# Market Data Interface — Design

## Overview

The backend has two market data sources: the built-in **simulator** and the **Massive REST API**. All downstream code (SSE streaming, price cache, portfolio valuation) uses the shared `PriceCache` object and never knows which source is active.

Selection is driven solely by the environment variable:

```
MASSIVE_API_KEY=<key>   →  Massive REST API (real data)
MASSIVE_API_KEY=        →  Simulator (default)
```

---

## `PriceRecord` — Shared Data Type

```python
from dataclasses import dataclass

@dataclass
class PriceRecord:
    ticker: str
    price: float          # latest trade price
    prev_price: float     # price from previous poll cycle (for flash direction)
    prev_close: float     # official previous session close (for % change)
    timestamp: float      # Unix seconds (time.time())
```

`% change = (price - prev_close) / prev_close`

---

## `PriceCache` — In-Memory Store

```python
import threading
from typing import Dict

class PriceCache:
    """Thread-safe cache of the latest PriceRecord per ticker."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[str, PriceRecord] = {}

    def update(self, record: PriceRecord) -> None:
        with self._lock:
            self._data[record.ticker] = record

    def get(self, ticker: str) -> PriceRecord | None:
        with self._lock:
            return self._data.get(ticker)

    def all(self) -> list[PriceRecord]:
        with self._lock:
            return list(self._data.values())

    def tickers(self) -> set[str]:
        with self._lock:
            return set(self._data.keys())
```

The cache is a singleton created at startup and passed into whichever provider is active.

---

## `MarketDataProvider` — Abstract Interface

```python
from abc import ABC, abstractmethod

class MarketDataProvider(ABC):
    """Base class for all market data sources."""

    @abstractmethod
    async def start(self, cache: PriceCache, tickers: set[str]) -> None:
        """Start the background polling/simulation loop."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> bool:
        """
        Register a ticker for tracking.
        Returns True if valid, False if the ticker is unknown (Massive rejects it).
        Simulator always returns True.
        """

    @abstractmethod
    def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker (but keep it in cache while a position is held)."""
```

---

## Factory Function

```python
import os
from .simulator import SimulatorProvider
from .massive import MassiveProvider

def create_market_provider() -> MarketDataProvider:
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveProvider(api_key=api_key)
    return SimulatorProvider()
```

Called once at application startup in `main.py` / `lifespan`.

---

## `MassiveProvider` — Sketch

```python
import asyncio
import httpx
import time

POLL_INTERVAL = 15.0   # seconds; safe for free tier (5 req/min)

class MassiveProvider(MarketDataProvider):
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._tickers: set[str] = set()
        self._cache: PriceCache | None = None
        self._prev_prices: dict[str, float] = {}

    async def start(self, cache: PriceCache, tickers: set[str]) -> None:
        self._cache = cache
        self._tickers = set(tickers)
        asyncio.create_task(self._poll_loop())

    async def add_ticker(self, ticker: str) -> bool:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=5,
            )
        if resp.status_code != 200:
            return False
        self._tickers.add(ticker)
        return True

    def remove_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker)

    async def _poll_loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                await self._fetch_and_update(client)
                await asyncio.sleep(POLL_INTERVAL)

    async def _fetch_and_update(self, client: httpx.AsyncClient) -> None:
        if not self._tickers:
            return
        tickers_param = ",".join(self._tickers)
        try:
            resp = await client.get(
                "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers",
                params={"tickers": tickers_param},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=10,
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return   # keep serving cached values; retry next cycle

        for t in resp.json().get("tickers", []):
            ticker = t["ticker"]
            price  = t["lastTrade"]["p"]
            record = PriceRecord(
                ticker=ticker,
                price=price,
                prev_price=self._prev_prices.get(ticker, price),
                prev_close=t["prevDay"]["c"],
                timestamp=time.time(),
            )
            self._cache.update(record)
            self._prev_prices[ticker] = price
```

---

## `SimulatorProvider` — Sketch

See `MARKET_SIMULATOR.md` for the full design. The simulator conforms to the same interface:

```python
class SimulatorProvider(MarketDataProvider):
    async def start(self, cache: PriceCache, tickers: set[str]) -> None: ...
    async def add_ticker(self, ticker: str) -> bool: ...   # always True
    def remove_ticker(self, ticker: str) -> None: ...
```

It runs an internal asyncio loop that updates the cache every ~500 ms.

---

## SSE Streaming Layer

The SSE endpoint reads from `PriceCache` on a fixed 500 ms cadence, independent of how fast the provider updates.

```python
# backend/routers/stream.py  (sketch)
import asyncio
from fastapi import Request
from fastapi.responses import StreamingResponse
import json
import time

async def price_stream(request: Request, cache: PriceCache):
    async def generate():
        while not await request.is_disconnected():
            records = cache.all()
            payload = [
                {
                    "ticker": r.ticker,
                    "price": r.price,
                    "prev_price": r.prev_price,
                    "prev_close": r.prev_close,
                    "timestamp": r.timestamp,
                }
                for r in records
            ]
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(generate(), media_type="text/event-stream")
```

Clients receive a snapshot of all tracked tickers every 500 ms regardless of whether the underlying source has new data. When the Massive free tier updates every 15 s, the same cached values simply repeat — the frontend only flashes on an actual price change (`price != prev_price`).

---

## Ticker Lifecycle

```
Watchlist add   →  provider.add_ticker(ticker)  →  cache starts tracking
Watchlist remove →  provider.remove_ticker(ticker) if no open position
                    else keep in cache (position remains priced)
Position closed  →  if ticker not in watchlist: provider.remove_ticker(ticker)
```

The backend checks both the watchlist table and the positions table before calling `remove_ticker`, ensuring held tickers stay priced even after watchlist removal.
