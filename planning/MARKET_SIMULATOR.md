# Market Simulator — Design

## Overview

The simulator generates synthetic price streams using **Geometric Brownian Motion (GBM)** with correlated shocks and occasional random events. It runs as an asyncio background task, updating the `PriceCache` every ~500 ms.

It is the default data source when `MASSIVE_API_KEY` is not set. Any ticker is accepted; unknown symbols get a random realistic seed price.

---

## Price Model: Geometric Brownian Motion

GBM is the standard model for equity prices. Each tick:

```
S(t+dt) = S(t) * exp((μ - σ²/2)*dt + σ*√dt*Z)
```

Where:
- `S(t)` — current price
- `μ` — drift (annualized, e.g. 0.05 for 5% expected annual return)
- `σ` — volatility (annualized, e.g. 0.20 for 20% annual vol)
- `dt` — time step in years (500 ms ≈ `500/1000/252/6.5/3600` trading-year fraction)
- `Z` — standard normal random variable

In practice `(μ - σ²/2)*dt` is tiny relative to noise at 500 ms, so drift barely matters intraday — it's included for correctness.

---

## Seed Prices and Previous Close

Each ticker has a **seed price** that doubles as its **previous close** baseline for % change calculations. This matches how the Massive API works (`prevDay.c`).

```python
SEED_PRICES: dict[str, float] = {
    "AAPL":  190.0,
    "GOOGL": 175.0,
    "MSFT":  415.0,
    "AMZN":  185.0,
    "TSLA":  245.0,
    "NVDA":  875.0,
    "META":  490.0,
    "JPM":   195.0,
    "V":     275.0,
    "NFLX":  630.0,
}

def seed_price_for(ticker: str) -> float:
    """Return known seed or a random realistic price for unknown tickers."""
    if ticker in SEED_PRICES:
        return SEED_PRICES[ticker]
    return round(random.uniform(50.0, 500.0), 2)
```

---

## Correlated Shocks

A market-wide common factor creates realistic co-movement across tickers (e.g. tech stocks move together on macro news).

```python
MARKET_BETA: dict[str, float] = {
    "AAPL": 0.9, "GOOGL": 1.0, "MSFT": 0.9, "AMZN": 1.0,
    "TSLA": 1.4, "NVDA": 1.5, "META": 1.1, "JPM": 0.8,
    "V": 0.7, "NFLX": 1.1,
}
DEFAULT_BETA = 1.0
```

Each tick, one market shock `Z_market` is drawn, then each ticker gets an idiosyncratic shock `Z_idio`:

```python
import numpy as np

def correlated_shock(beta: float, market_shock: float) -> float:
    idio = np.random.normal()
    rho  = min(beta * 0.3, 0.9)   # correlation scales with beta, capped at 0.9
    return rho * market_shock + np.sqrt(1 - rho**2) * idio
```

---

## Random Events

Every tick, each ticker has a small independent chance of a sudden 2–5% move to simulate earnings, news, or macro shocks.

```python
EVENT_PROB   = 0.002          # ~0.2% chance per tick per ticker
EVENT_SIZE   = (0.02, 0.05)   # 2–5% move
```

```python
def apply_event(price: float) -> float:
    if random.random() < EVENT_PROB:
        pct   = random.uniform(*EVENT_SIZE)
        sign  = random.choice([-1, 1])
        return price * (1 + sign * pct)
    return price
```

---

## Per-Ticker Parameters

```python
from dataclasses import dataclass, field

@dataclass
class TickerParams:
    seed_price: float
    mu: float    = 0.05    # annualized drift
    sigma: float = 0.20    # annualized volatility
    beta: float  = 1.0     # market correlation factor
```

Default `sigma = 0.20` is roughly median S&P 500 historical vol. Higher-beta names (NVDA, TSLA) use higher sigma:

```python
TICKER_PARAMS: dict[str, TickerParams] = {
    "AAPL":  TickerParams(190.0, sigma=0.22, beta=0.9),
    "GOOGL": TickerParams(175.0, sigma=0.25, beta=1.0),
    "MSFT":  TickerParams(415.0, sigma=0.22, beta=0.9),
    "AMZN":  TickerParams(185.0, sigma=0.28, beta=1.0),
    "TSLA":  TickerParams(245.0, sigma=0.55, beta=1.4),
    "NVDA":  TickerParams(875.0, sigma=0.50, beta=1.5),
    "META":  TickerParams(490.0, sigma=0.30, beta=1.1),
    "JPM":   TickerParams(195.0, sigma=0.18, beta=0.8),
    "V":     TickerParams(275.0, sigma=0.15, beta=0.7),
    "NFLX":  TickerParams(630.0, sigma=0.32, beta=1.1),
}
```

---

## `SimulatorProvider` — Full Implementation

```python
import asyncio
import math
import random
import time
import numpy as np

from .interface import MarketDataProvider, PriceCache, PriceRecord

TICK_INTERVAL = 0.5          # seconds between updates
DT = TICK_INTERVAL / (252 * 6.5 * 3600)   # fraction of a trading year


class SimulatorProvider(MarketDataProvider):
    """Generates GBM price streams for all tracked tickers."""

    def __init__(self):
        self._params: dict[str, TickerParams] = {}
        self._prices: dict[str, float] = {}
        self._prev_prices: dict[str, float] = {}
        self._cache: PriceCache | None = None

    async def start(self, cache: PriceCache, tickers: set[str]) -> None:
        self._cache = cache
        for ticker in tickers:
            self._init_ticker(ticker)
        asyncio.create_task(self._run())

    async def add_ticker(self, ticker: str) -> bool:
        self._init_ticker(ticker)
        return True

    def remove_ticker(self, ticker: str) -> None:
        self._params.pop(ticker, None)
        self._prices.pop(ticker, None)
        self._prev_prices.pop(ticker, None)

    def _init_ticker(self, ticker: str) -> None:
        if ticker in self._params:
            return
        params = TICKER_PARAMS.get(ticker) or TickerParams(
            seed_price=seed_price_for(ticker)
        )
        self._params[ticker] = params
        self._prices[ticker] = params.seed_price
        self._prev_prices[ticker] = params.seed_price

    async def _run(self) -> None:
        while True:
            self._tick()
            await asyncio.sleep(TICK_INTERVAL)

    def _tick(self) -> None:
        if not self._params:
            return

        market_shock = np.random.normal()   # one shared factor per tick

        for ticker, params in self._params.items():
            z     = correlated_shock(params.beta, market_shock)
            drift = (params.mu - 0.5 * params.sigma ** 2) * DT
            diffusion = params.sigma * math.sqrt(DT) * z

            new_price = self._prices[ticker] * math.exp(drift + diffusion)
            new_price = apply_event(new_price)
            new_price = max(new_price, 0.01)   # floor at $0.01

            record = PriceRecord(
                ticker=ticker,
                price=round(new_price, 2),
                prev_price=round(self._prices[ticker], 2),
                prev_close=self._params[ticker].seed_price,
                timestamp=time.time(),
            )
            self._cache.update(record)
            self._prev_prices[ticker] = self._prices[ticker]
            self._prices[ticker] = new_price
```

---

## Module Layout

```
backend/
└── market/
    ├── __init__.py       # exports create_market_provider()
    ├── interface.py      # PriceRecord, PriceCache, MarketDataProvider
    ├── simulator.py      # SimulatorProvider, TICKER_PARAMS, GBM helpers
    └── massive.py        # MassiveProvider, HTTP polling
```

---

## Behavior Summary

| Property | Value |
|----------|-------|
| Tick interval | 500 ms |
| Price model | GBM with drift + volatility |
| Correlation | Common market factor scaled by beta |
| Random events | 0.2% chance per tick, 2–5% move |
| Previous close | Seed price (fixed; resets to seed on restart) |
| Unknown tickers | Accepted; assigned random $50–$500 seed |
| Floor | $0.01 (prevents zero/negative price) |

---

## Why GBM

GBM is the industry-standard model for equity price simulation (basis of Black-Scholes). Its key properties match what we want:
- Prices are always positive (log-normal)
- Returns are normally distributed around drift
- Volatility scales with price level (proportional shocks)
- Simple single-parameter (sigma) control per ticker
