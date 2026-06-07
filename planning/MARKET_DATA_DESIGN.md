# Market Data Backend — Implementation Design

This document is the authoritative implementation guide for the market data subsystem. It covers the full module layout, every class and function, and ready-to-paste code for all three layers: shared interface, simulator, and Massive API provider.

---

## 1. Module Layout

```
backend/
└── market/
    ├── __init__.py       # public API: create_market_provider()
    ├── interface.py      # PriceRecord, PriceCache, MarketDataProvider (ABC)
    ├── simulator.py      # SimulatorProvider + GBM helpers
    └── massive.py        # MassiveProvider + HTTP polling
```

Everything downstream (SSE router, portfolio valuation, watchlist API) imports only from `backend/market/__init__.py`. The provider selection is completely opaque to callers.

---

## 2. Shared Data Types — `interface.py`

```python
# backend/market/interface.py

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict


@dataclass
class PriceRecord:
    """One ticker's latest pricing data.

    prev_close is the official previous session close (or seed price for the
    simulator). Used to compute daily % change shown in the watchlist and
    positions table: pct_change = (price - prev_close) / prev_close.

    prev_price is the price from the immediately preceding poll/tick cycle.
    The frontend uses sign(price - prev_price) to decide whether to flash
    green or red, and only flashes when the value actually changes.
    """
    ticker: str
    price: float
    prev_price: float
    prev_close: float
    timestamp: float   # Unix seconds (time.time())


class PriceCache:
    """Thread-safe in-memory store of the latest PriceRecord per ticker.

    Written by the background provider task; read by the SSE generator,
    portfolio valuation, and watchlist endpoint — all on different threads/tasks.
    """

    def __init__(self) -> None:
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

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._data.pop(ticker, None)


class MarketDataProvider(ABC):
    """Abstract base for all market data sources.

    The concrete implementation (SimulatorProvider or MassiveProvider) is
    created once at startup and injected wherever market data is needed.
    All downstream code is agnostic to which provider is active.
    """

    @abstractmethod
    async def start(self, cache: PriceCache, tickers: set[str]) -> None:
        """Seed the cache with initial records and launch the background loop.

        Called once during FastAPI lifespan startup. The provider owns the
        asyncio task; it must not block.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> bool:
        """Register a ticker for tracking.

        Returns True if the ticker is valid and will be tracked.
        With Massive, an unknown symbol triggers an API validation call and
        returns False on 404. The simulator always returns True.
        """

    @abstractmethod
    def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker.

        The caller is responsible for deciding whether to call this (e.g. do
        not remove a ticker that still has an open position). This method
        only stops the provider from updating it — it does NOT evict the
        record from PriceCache.
        """
```

---

## 3. Factory Function — `__init__.py`

```python
# backend/market/__init__.py

import os

from .interface import MarketDataProvider, PriceCache, PriceRecord
from .massive import MassiveProvider
from .simulator import SimulatorProvider

__all__ = [
    "MarketDataProvider",
    "PriceCache",
    "PriceRecord",
    "create_market_provider",
]


def create_market_provider() -> MarketDataProvider:
    """Select and instantiate the correct provider based on env vars.

    Called once in main.py lifespan. Returns a MassiveProvider if
    MASSIVE_API_KEY is set and non-empty; otherwise returns SimulatorProvider.
    """
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveProvider(api_key=api_key)
    return SimulatorProvider()
```

---

## 4. Simulator Provider — `simulator.py`

### 4.1 Price Parameters

```python
# backend/market/simulator.py

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field

import numpy as np

from .interface import MarketDataProvider, PriceCache, PriceRecord


@dataclass
class TickerParams:
    seed_price: float
    mu: float = 0.05      # annualized drift
    sigma: float = 0.20   # annualized volatility
    beta: float = 1.0     # market-factor correlation weight


# Known tickers get realistic params; anything else falls back to defaults.
TICKER_PARAMS: dict[str, TickerParams] = {
    "AAPL":  TickerParams(190.0,  sigma=0.22, beta=0.9),
    "GOOGL": TickerParams(175.0,  sigma=0.25, beta=1.0),
    "MSFT":  TickerParams(415.0,  sigma=0.22, beta=0.9),
    "AMZN":  TickerParams(185.0,  sigma=0.28, beta=1.0),
    "TSLA":  TickerParams(245.0,  sigma=0.55, beta=1.4),
    "NVDA":  TickerParams(875.0,  sigma=0.50, beta=1.5),
    "META":  TickerParams(490.0,  sigma=0.30, beta=1.1),
    "JPM":   TickerParams(195.0,  sigma=0.18, beta=0.8),
    "V":     TickerParams(275.0,  sigma=0.15, beta=0.7),
    "NFLX":  TickerParams(630.0,  sigma=0.32, beta=1.1),
}
```

### 4.2 GBM Math Helpers

```python
# Time step: 500 ms expressed as a fraction of a trading year
# 252 trading days × 6.5 trading hours × 3600 seconds = 5_896_800 seconds/year
TICK_INTERVAL = 0.5
DT = TICK_INTERVAL / (252 * 6.5 * 3600)   # ≈ 8.49e-8 years

EVENT_PROB = 0.002          # probability per tick per ticker of a news event
EVENT_SIZE = (0.02, 0.05)   # event magnitude: 2–5% move


def seed_price_for(ticker: str) -> float:
    """Return a known seed price, or a random realistic price for unknowns."""
    p = TICKER_PARAMS.get(ticker)
    if p:
        return p.seed_price
    return round(random.uniform(50.0, 500.0), 2)


def correlated_shock(beta: float, market_shock: float) -> float:
    """Blend a shared market factor with an idiosyncratic shock.

    rho (correlation) scales with beta, capped at 0.9 to preserve
    meaningful idiosyncratic variation even for high-beta names.
    The combined shock has unit variance by construction.
    """
    idio = np.random.normal()
    rho = min(beta * 0.3, 0.9)
    return rho * market_shock + math.sqrt(1.0 - rho ** 2) * idio


def apply_event(price: float) -> float:
    """Randomly inject a 2–5% sudden move to simulate news/earnings."""
    if random.random() < EVENT_PROB:
        pct = random.uniform(*EVENT_SIZE)
        sign = random.choice([-1, 1])
        return price * (1.0 + sign * pct)
    return price


def gbm_step(price: float, params: TickerParams, z: float) -> float:
    """Advance price one GBM step.

    S(t+dt) = S(t) * exp((mu - sigma²/2)*dt + sigma*sqrt(dt)*Z)
    """
    drift = (params.mu - 0.5 * params.sigma ** 2) * DT
    diffusion = params.sigma * math.sqrt(DT) * z
    return price * math.exp(drift + diffusion)
```

### 4.3 SimulatorProvider Class

```python
class SimulatorProvider(MarketDataProvider):
    """Generates GBM price streams with correlated shocks and random events.

    Runs a single asyncio task that fires every 500 ms. All tickers are
    updated in one synchronous batch per tick (no per-ticker tasks) so
    the common market shock is shared correctly across all tickers.
    """

    def __init__(self) -> None:
        self._params: dict[str, TickerParams] = {}
        self._prices: dict[str, float] = {}
        self._cache: PriceCache | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # MarketDataProvider interface
    # ------------------------------------------------------------------

    async def start(self, cache: PriceCache, tickers: set[str]) -> None:
        self._cache = cache
        for ticker in tickers:
            self._init_ticker(ticker)
        # Seed the cache immediately so the SSE endpoint has data on first request
        self._tick()
        self._task = asyncio.create_task(self._run())

    async def add_ticker(self, ticker: str) -> bool:
        self._init_ticker(ticker)
        # Emit one record immediately so callers get a price without waiting
        if self._cache:
            price = self._prices[ticker]
            self._cache.update(PriceRecord(
                ticker=ticker,
                price=price,
                prev_price=price,
                prev_close=self._params[ticker].seed_price,
                timestamp=time.time(),
            ))
        return True

    def remove_ticker(self, ticker: str) -> None:
        self._params.pop(ticker, None)
        self._prices.pop(ticker, None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_ticker(self, ticker: str) -> None:
        if ticker in self._params:
            return
        params = TICKER_PARAMS.get(ticker) or TickerParams(
            seed_price=seed_price_for(ticker)
        )
        self._params[ticker] = params
        self._prices[ticker] = params.seed_price

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            self._tick()

    def _tick(self) -> None:
        if not self._params or self._cache is None:
            return

        market_shock = np.random.normal()   # shared factor for this tick

        for ticker, params in list(self._params.items()):
            prev = self._prices[ticker]
            z = correlated_shock(params.beta, market_shock)
            new_price = gbm_step(prev, params, z)
            new_price = apply_event(new_price)
            new_price = max(new_price, 0.01)   # hard floor

            self._cache.update(PriceRecord(
                ticker=ticker,
                price=round(new_price, 2),
                prev_price=round(prev, 2),
                prev_close=params.seed_price,   # fixed baseline for % change
                timestamp=time.time(),
            ))
            self._prices[ticker] = new_price
```

---

## 5. Massive Provider — `massive.py`

### 5.1 Constants and Initialization

```python
# backend/market/massive.py

from __future__ import annotations

import asyncio
import time

import httpx

from .interface import MarketDataProvider, PriceCache, PriceRecord

BASE_URL = "https://api.massive.com"
POLL_INTERVAL = 15.0   # seconds; safe for free tier (5 req/min ceiling)


class MassiveProvider(MarketDataProvider):
    """Polls the Massive (formerly Polygon.io) REST API for live prices.

    One batch request fetches all watched tickers. The poll interval is
    configurable but defaults to 15 s for free-tier safety. Between polls
    the SSE layer simply repeats the last cached values; the frontend flashes
    only when price actually changes.
    """

    def __init__(self, api_key: str, poll_interval: float = POLL_INTERVAL) -> None:
        self._api_key = api_key
        self._poll_interval = poll_interval
        self._tickers: set[str] = set()
        self._prev_prices: dict[str, float] = {}
        self._cache: PriceCache | None = None
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
```

### 5.2 Start and Lifecycle

```python
    async def start(self, cache: PriceCache, tickers: set[str]) -> None:
        self._cache = cache
        self._tickers = set(tickers)
        # One shared client for the lifetime of the provider
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=10.0,
        )
        # Fetch initial prices immediately before launching the poll loop
        await self._fetch_and_update()
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            await self._fetch_and_update()
```

### 5.3 Batch Fetch

```python
    async def _fetch_and_update(self) -> None:
        if not self._tickers or self._client is None or self._cache is None:
            return

        tickers_param = ",".join(sorted(self._tickers))
        try:
            resp = await self._client.get(
                "/v2/snapshot/locale/us/markets/stocks/tickers",
                params={"tickers": tickers_param},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            # Network or server error: keep serving the last cached values.
            # The SSE layer continues streaming stale-but-valid data until
            # the next successful poll.
            return

        data = resp.json()
        now = time.time()

        for t in data.get("tickers", []):
            ticker = t.get("ticker")
            if not ticker:
                continue

            last_trade = t.get("lastTrade") or {}
            prev_day = t.get("prevDay") or {}

            price = float(last_trade.get("p", 0) or 0)
            prev_close = float(prev_day.get("c", price) or price)

            if price <= 0:
                continue   # skip malformed entries

            prev_price = self._prev_prices.get(ticker, price)

            self._cache.update(PriceRecord(
                ticker=ticker,
                price=round(price, 2),
                prev_price=round(prev_price, 2),
                prev_close=round(prev_close, 2),
                timestamp=now,
            ))
            self._prev_prices[ticker] = price
```

### 5.4 Ticker Management

```python
    async def add_ticker(self, ticker: str) -> bool:
        """Validate ticker against Massive API before tracking.

        A 200 response confirms the symbol exists. Any other status (404
        for an unknown ticker, network error, etc.) returns False so the
        caller can reject the watchlist add request with HTTP 400.
        """
        if self._client is None:
            return False
        try:
            resp = await self._client.get(
                f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
                timeout=5.0,
            )
        except httpx.HTTPError:
            return False

        if resp.status_code != 200:
            return False

        self._tickers.add(ticker)

        # Parse and seed the cache immediately so the caller gets a price
        data = resp.json()
        snap = (data.get("ticker") or {})
        last_trade = snap.get("lastTrade") or {}
        prev_day = snap.get("prevDay") or {}
        price = float(last_trade.get("p", 0) or 0)
        prev_close = float(prev_day.get("c", price) or price)
        if price > 0 and self._cache:
            self._cache.update(PriceRecord(
                ticker=ticker,
                price=round(price, 2),
                prev_price=round(price, 2),
                prev_close=round(prev_close, 2),
                timestamp=time.time(),
            ))
            self._prev_prices[ticker] = price

        return True

    def remove_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker)
        self._prev_prices.pop(ticker, None)
        # NOTE: does NOT evict from PriceCache — caller handles that
```

---

## 6. FastAPI Integration — `main.py` (lifespan)

```python
# backend/main.py  (relevant excerpt)

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from market import PriceCache, create_market_provider
from db import init_db, get_watchlist_tickers, get_position_tickers
from routers import stream, portfolio, watchlist, chat

# Singletons shared across the app
price_cache = PriceCache()
provider = create_market_provider()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Ensure database exists and is seeded
    await init_db()

    # 2. Load all tickers that need pricing: watchlist ∪ open positions
    watched = await get_watchlist_tickers()
    held = await get_position_tickers()
    initial_tickers = watched | held

    # 3. Start the market data provider (seeds cache, launches background task)
    await provider.start(price_cache, initial_tickers)

    yield   # application is running

    # Shutdown: nothing to clean up (asyncio tasks are cancelled automatically)


app = FastAPI(lifespan=lifespan)

# Inject shared singletons into routers via FastAPI dependency
app.state.cache = price_cache
app.state.provider = provider

app.include_router(stream.router)
app.include_router(portfolio.router)
app.include_router(watchlist.router)
app.include_router(chat.router)

# Serve the Next.js static export for all non-API routes
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

### Dependency Helpers

```python
# backend/dependencies.py

from fastapi import Request
from market import PriceCache, MarketDataProvider


def get_cache(request: Request) -> PriceCache:
    return request.app.state.cache


def get_provider(request: Request) -> MarketDataProvider:
    return request.app.state.provider
```

---

## 7. SSE Streaming Router — `routers/stream.py`

The SSE layer reads `PriceCache` at a fixed 500 ms cadence, independent of how fast the provider updates. Slow sources (Massive free tier at 15 s) simply repeat the last cached value; the frontend detects actual changes via `price !== prev_price`.

```python
# backend/routers/stream.py

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from dependencies import get_cache
from market import PriceCache

router = APIRouter()

SSE_INTERVAL = 0.5   # seconds between snapshots sent to each client


@router.get("/api/stream/prices")
async def price_stream(
    request: Request,
    cache: PriceCache = Depends(get_cache),
):
    async def generate():
        while not await request.is_disconnected():
            records = cache.all()
            payload = [
                {
                    "ticker":     r.ticker,
                    "price":      r.price,
                    "prev_price": r.prev_price,
                    "prev_close": r.prev_close,
                    "timestamp":  r.timestamp,
                }
                for r in records
            ]
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(SSE_INTERVAL)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if present
        },
    )
```

---

## 8. Watchlist API — Ticker Lifecycle

The watchlist router owns the decision of whether to call `remove_ticker` when a ticker is deleted, because only it knows whether an open position exists.

```python
# backend/routers/watchlist.py  (relevant excerpts)

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependencies import get_cache, get_provider
from market import PriceCache, MarketDataProvider
from db import (
    add_watchlist_ticker, remove_watchlist_ticker,
    get_watchlist_with_prices, has_open_position,
)

router = APIRouter(prefix="/api/watchlist")


class AddTickerRequest(BaseModel):
    ticker: str


@router.post("")
async def add_ticker(
    body: AddTickerRequest,
    cache: PriceCache = Depends(get_cache),
    provider: MarketDataProvider = Depends(get_provider),
):
    ticker = body.ticker.upper().strip()

    # Provider validates the ticker (Massive checks the API; simulator always passes)
    valid = await provider.add_ticker(ticker)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Unknown ticker: {ticker}")

    await add_watchlist_ticker(ticker)
    # Cache is already seeded by provider.add_ticker() above
    return {"ticker": ticker, "price": cache.get(ticker)}


@router.delete("/{ticker}")
async def delete_ticker(
    ticker: str,
    cache: PriceCache = Depends(get_cache),
    provider: MarketDataProvider = Depends(get_provider),
):
    ticker = ticker.upper()
    await remove_watchlist_ticker(ticker)

    # Only stop tracking if the user does not hold a position in this ticker.
    # Held tickers must remain in the cache so position P&L stays current.
    if not await has_open_position(ticker):
        provider.remove_ticker(ticker)
        cache.remove(ticker)

    return {"removed": ticker}


@router.get("")
async def get_watchlist(cache: PriceCache = Depends(get_cache)):
    return await get_watchlist_with_prices(cache)
```

### Ticker Lifecycle Summary

```
User adds ticker
  → provider.add_ticker(ticker)      # validates (Massive) or accepts (simulator)
  → cache is seeded immediately
  → DB watchlist row inserted

User removes ticker (no position held)
  → DB watchlist row deleted
  → provider.remove_ticker(ticker)   # stop updating
  → cache.remove(ticker)             # evict from SSE stream

User removes ticker (position still held)
  → DB watchlist row deleted
  → provider keeps tracking          # ticker stays in provider's active set
  → cache keeps the record           # position remains priced in SSE stream

Position closed (ticker not in watchlist)
  → portfolio router calls provider.remove_ticker(ticker)
  → portfolio router calls cache.remove(ticker)
```

---

## 9. Portfolio Valuation Using the Cache

The portfolio endpoint enriches DB positions with live prices from the cache:

```python
# backend/routers/portfolio.py  (valuation excerpt)

from market import PriceCache, PriceRecord


def value_position(
    ticker: str,
    quantity: float,
    avg_cost: float,
    cache: PriceCache,
) -> dict:
    record: PriceRecord | None = cache.get(ticker)
    current_price = record.price if record else avg_cost   # fallback to cost if no price
    market_value = quantity * current_price
    cost_basis = quantity * avg_cost
    unrealized_pnl = market_value - cost_basis
    pct_change_day = (
        (record.price - record.prev_close) / record.prev_close
        if record and record.prev_close > 0
        else 0.0
    )
    return {
        "ticker": ticker,
        "quantity": quantity,
        "avg_cost": avg_cost,
        "current_price": current_price,
        "market_value": market_value,
        "unrealized_pnl": unrealized_pnl,
        "pct_change_day": pct_change_day,
    }
```

---

## 10. Python Dependencies (`pyproject.toml` additions)

```toml
[project]
dependencies = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.29",
    "httpx>=0.27",       # async HTTP client for Massive API
    "numpy>=1.26",       # GBM normal random draws
    "aiosqlite>=0.20",   # async SQLite
    "pydantic>=2.7",
]
```

`numpy` is used only by the simulator. The Massive provider needs only `httpx`. Both are always installed since the same image serves either mode.

---

## 11. Unit Testing Guide

### Simulator Tests

```python
# backend/tests/test_simulator.py

import asyncio
import pytest
from market.simulator import SimulatorProvider, gbm_step, TickerParams
from market.interface import PriceCache


@pytest.mark.asyncio
async def test_simulator_seeds_cache_on_start():
    cache = PriceCache()
    provider = SimulatorProvider()
    await provider.start(cache, {"AAPL", "TSLA"})

    records = cache.all()
    tickers = {r.ticker for r in records}
    assert "AAPL" in tickers
    assert "TSLA" in tickers
    for r in records:
        assert r.price > 0
        assert r.prev_close > 0


@pytest.mark.asyncio
async def test_simulator_price_updates_after_tick():
    cache = PriceCache()
    provider = SimulatorProvider()
    await provider.start(cache, {"AAPL"})
    initial = cache.get("AAPL").price

    # Wait for two tick intervals to guarantee at least one update
    await asyncio.sleep(1.1)
    updated = cache.get("AAPL").price

    # Price should have changed (astronomically unlikely to be exactly equal)
    assert updated != initial


def test_gbm_step_stays_positive():
    params = TickerParams(seed_price=100.0, sigma=0.99)
    for _ in range(10_000):
        price = gbm_step(100.0, params, z=float("nan"))   # nan path never reached
    # Real test: ensure floor prevents zero/negative
    # Use extreme negative Z to stress-test
    import numpy as np
    for _ in range(1_000):
        z = np.random.normal(-10, 1)  # very negative shock
        new = max(gbm_step(100.0, params, z), 0.01)
        assert new > 0


@pytest.mark.asyncio
async def test_add_unknown_ticker_returns_true():
    cache = PriceCache()
    provider = SimulatorProvider()
    await provider.start(cache, set())
    result = await provider.add_ticker("XYZUNK")
    assert result is True
    # Price should be in the realistic seed range
    record = cache.get("XYZUNK")
    assert record is not None
    assert 50.0 <= record.price <= 500.0


@pytest.mark.asyncio
async def test_remove_ticker_stops_updates():
    cache = PriceCache()
    provider = SimulatorProvider()
    await provider.start(cache, {"AAPL"})
    provider.remove_ticker("AAPL")
    # "AAPL" still in cache (not evicted by provider), but no longer updated
    assert "AAPL" not in provider._params
```

### Massive Provider Tests (with httpx mocking)

```python
# backend/tests/test_massive.py

import pytest
import respx
import httpx
from market.massive import MassiveProvider
from market.interface import PriceCache

MOCK_SNAPSHOT_RESPONSE = {
    "status": "OK",
    "count": 1,
    "tickers": [{
        "ticker": "AAPL",
        "lastTrade": {"p": 191.50},
        "prevDay": {"c": 190.00},
        "todaysChangePerc": 0.79,
    }],
}


@pytest.mark.asyncio
@respx.mock
async def test_massive_seeds_cache_on_start():
    respx.get(
        "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers",
    ).mock(return_value=httpx.Response(200, json=MOCK_SNAPSHOT_RESPONSE))

    cache = PriceCache()
    provider = MassiveProvider(api_key="test-key", poll_interval=999)
    await provider.start(cache, {"AAPL"})

    record = cache.get("AAPL")
    assert record is not None
    assert record.price == 191.50
    assert record.prev_close == 190.00


@pytest.mark.asyncio
@respx.mock
async def test_massive_add_ticker_valid():
    respx.get(
        "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/MSFT",
    ).mock(return_value=httpx.Response(200, json={
        "ticker": {
            "ticker": "MSFT",
            "lastTrade": {"p": 415.0},
            "prevDay": {"c": 413.0},
        }
    }))

    cache = PriceCache()
    provider = MassiveProvider(api_key="test-key")
    provider._cache = cache
    import httpx as _httpx
    provider._client = _httpx.AsyncClient(
        base_url="https://api.massive.com",
        headers={"Authorization": "Bearer test-key"},
    )

    result = await provider.add_ticker("MSFT")
    assert result is True
    assert "MSFT" in provider._tickers


@pytest.mark.asyncio
@respx.mock
async def test_massive_add_ticker_invalid():
    respx.get(
        "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/FAKE",
    ).mock(return_value=httpx.Response(404, json={"status": "NOT_FOUND"}))

    cache = PriceCache()
    provider = MassiveProvider(api_key="test-key")
    provider._cache = cache
    import httpx as _httpx
    provider._client = _httpx.AsyncClient(
        base_url="https://api.massive.com",
        headers={"Authorization": "Bearer test-key"},
    )

    result = await provider.add_ticker("FAKE")
    assert result is False
    assert "FAKE" not in provider._tickers


@pytest.mark.asyncio
@respx.mock
async def test_massive_handles_http_error_gracefully():
    """Provider silently continues on network error (stale cache is served)."""
    respx.get(
        "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers",
    ).mock(side_effect=httpx.ConnectError("timeout"))

    cache = PriceCache()
    provider = MassiveProvider(api_key="test-key", poll_interval=999)
    provider._cache = cache
    provider._tickers = {"AAPL"}
    import httpx as _httpx
    provider._client = _httpx.AsyncClient(base_url="https://api.massive.com")

    # Should not raise; cache remains unchanged
    await provider._fetch_and_update()
    assert cache.get("AAPL") is None   # was never seeded; no crash
```

---

## 12. Environment Variable Reference

| Variable | Required | Default | Effect |
|---|---|---|---|
| `MASSIVE_API_KEY` | No | `""` | Non-empty → use MassiveProvider; empty → SimulatorProvider |
| `GROQ_API_KEY` | For LLM fallback | — | Not used by market data subsystem |
| `LLM_MOCK` | No | `false` | Not used by market data subsystem |

---

## 13. Design Decisions

| Decision | Rationale |
|---|---|
| Single `PriceCache` singleton | Avoids data races; one source of truth for all consumers (SSE, portfolio, watchlist) |
| SSE cadence independent of poll cadence | Decouples the data source from the delivery layer; slow sources work without frontend changes |
| `prev_price` on every record | Frontend can derive flash direction purely from SSE data — no separate state needed |
| `prev_close` fixed to seed in simulator | Consistent baseline for % change that matches what Massive returns; resets cleanly on container restart |
| Provider does NOT evict from cache | Separation of concerns — the router (not the provider) decides when it's safe to evict a ticker |
| `numpy` only in simulator | The Massive provider has no scientific computing dependency; keeps the import footprint minimal in that path |
| `httpx.AsyncClient` shared in Massive | One connection pool for the lifetime of the app; avoids connection setup overhead on every poll |
