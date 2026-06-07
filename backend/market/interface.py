from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict


@dataclass
class PriceRecord:
    """One ticker's latest pricing data.

    prev_close is the official previous session close (or seed price for the
    simulator). Used to compute daily % change:
        pct_change = (price - prev_close) / prev_close

    prev_price is the price from the immediately preceding poll/tick cycle.
    The frontend uses sign(price - prev_price) to decide whether to flash
    green or red, and only flashes when the value actually changes.
    """

    ticker: str
    price: float
    prev_price: float
    prev_close: float
    timestamp: float  # Unix seconds (time.time())


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
