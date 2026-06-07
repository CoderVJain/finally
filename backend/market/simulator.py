from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass

import numpy as np

from .interface import MarketDataProvider, PriceCache, PriceRecord


@dataclass
class TickerParams:
    seed_price: float
    mu: float = 0.05     # annualized drift
    sigma: float = 0.20  # annualized volatility
    beta: float = 1.0    # market-factor correlation weight


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

# Time step: 500 ms expressed as a fraction of a trading year
# 252 trading days × 6.5 trading hours × 3600 seconds = 5_896_800 seconds/year
TICK_INTERVAL = 0.5
DT = TICK_INTERVAL / (252 * 6.5 * 3600)  # ≈ 8.49e-8 years

EVENT_PROB = 0.002         # probability per tick per ticker of a news event
EVENT_SIZE = (0.02, 0.05)  # event magnitude: 2–5% move


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
    return rho * market_shock + math.sqrt(1.0 - rho**2) * idio


def apply_event(price: float) -> float:
    """Randomly inject a 2–5% sudden move to simulate news/earnings."""
    if random.random() < EVENT_PROB:
        pct = random.uniform(*EVENT_SIZE)
        sign = random.choice([-1, 1])
        return price * (1.0 + sign * pct)
    return price


def gbm_step(price: float, params: TickerParams, z: float) -> float:
    """Advance price one GBM step.

    S(t+dt) = S(t) * exp((mu - sigma^2/2)*dt + sigma*sqrt(dt)*Z)
    """
    drift = (params.mu - 0.5 * params.sigma**2) * DT
    diffusion = params.sigma * math.sqrt(DT) * z
    return price * math.exp(drift + diffusion)


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
            self._cache.update(
                PriceRecord(
                    ticker=ticker,
                    price=price,
                    prev_price=price,
                    prev_close=self._params[ticker].seed_price,
                    timestamp=time.time(),
                )
            )
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

        market_shock = np.random.normal()  # shared factor for this tick

        for ticker, params in list(self._params.items()):
            prev = self._prices[ticker]
            z = correlated_shock(params.beta, market_shock)
            new_price = gbm_step(prev, params, z)
            new_price = apply_event(new_price)
            new_price = max(new_price, 0.01)  # hard floor

            self._cache.update(
                PriceRecord(
                    ticker=ticker,
                    price=round(new_price, 2),
                    prev_price=round(prev, 2),
                    prev_close=params.seed_price,  # fixed baseline for % change
                    timestamp=time.time(),
                )
            )
            self._prices[ticker] = new_price
