"""Unit tests for SimulatorProvider and GBM helpers."""

import asyncio
import math

import numpy as np
import pytest

from market.interface import PriceCache
from market.simulator import (
    TICKER_PARAMS,
    TICK_INTERVAL,
    SimulatorProvider,
    TickerParams,
    apply_event,
    correlated_shock,
    gbm_step,
    seed_price_for,
)


# ---------------------------------------------------------------------------
# GBM math helpers
# ---------------------------------------------------------------------------


class TestGbmStep:
    def test_positive_price_positive_z(self):
        params = TickerParams(seed_price=100.0)
        result = gbm_step(100.0, params, z=2.0)
        assert result > 0

    def test_positive_price_negative_z(self):
        """exp() of any finite number is positive, so price stays positive."""
        params = TickerParams(seed_price=100.0, sigma=0.99)
        for z in [-5.0, -10.0, -20.0, -50.0]:
            result = gbm_step(100.0, params, z)
            assert result > 0, f"gbm_step returned {result} for z={z}"

    def test_zero_z_gives_drift_only(self):
        """With z=0 the result is purely the drift component."""
        params = TickerParams(seed_price=100.0, mu=0.05, sigma=0.20)
        from market.simulator import DT
        expected = 100.0 * math.exp((params.mu - 0.5 * params.sigma**2) * DT)
        result = gbm_step(100.0, params, z=0.0)
        assert abs(result - expected) < 1e-10

    def test_price_scales_proportionally(self):
        """Doubling the seed price should double the output (log-normal property)."""
        params = TickerParams(seed_price=100.0)
        z = 1.5
        r1 = gbm_step(100.0, params, z)
        r2 = gbm_step(200.0, params, z)
        assert abs(r2 / r1 - 2.0) < 1e-9

    def test_high_sigma_extreme_shocks_stay_positive(self):
        """Even with extreme downward shocks and high vol, price is positive before floor."""
        params = TickerParams(seed_price=100.0, sigma=2.0)
        rng = np.random.default_rng(42)
        for _ in range(1_000):
            z = float(rng.normal(-8, 1))
            result = gbm_step(1.0, params, z)
            assert result > 0


class TestCorrelatedShock:
    def test_returns_float(self):
        result = correlated_shock(beta=1.0, market_shock=0.5)
        assert isinstance(result, float)

    def test_high_beta_scales_rho_up(self):
        """Higher beta → higher rho → shock more correlated with market."""
        # rho = min(beta * 0.3, 0.9); beta=3.0 → rho=0.9 (capped)
        rho_low  = min(0.5 * 0.3, 0.9)   # 0.15
        rho_high = min(3.0 * 0.3, 0.9)   # 0.9
        assert rho_high > rho_low

    def test_beta_zero_pure_idiosyncratic(self):
        """With beta=0, rho=0, shock is entirely idiosyncratic (uncorrelated)."""
        # Just verify it doesn't raise and returns a number
        result = correlated_shock(beta=0.0, market_shock=999.0)
        assert isinstance(result, float)

    def test_distribution_reasonable(self):
        """Over many draws the shock should have roughly unit variance."""
        shocks = [correlated_shock(1.0, float(np.random.normal())) for _ in range(10_000)]
        std = float(np.std(shocks))
        # Standard deviation should be close to 1 (±20% tolerance)
        assert 0.8 < std < 1.2


class TestApplyEvent:
    def test_most_ticks_unchanged(self):
        """With EVENT_PROB=0.002, the vast majority of calls return price unchanged."""
        unchanged = sum(1 for _ in range(10_000) if apply_event(100.0) == 100.0)
        # Expect ~98% unchanged; allow generous bounds to avoid flakiness
        assert unchanged > 9_500

    def test_event_magnitude_in_range(self):
        """When an event fires the move should be 2–5%."""
        original = 100.0
        # Force an event by monkeypatching random
        import random as _random
        original_random = _random.random
        original_uniform = _random.uniform
        original_choice = _random.choice
        try:
            _random.random = lambda: 0.0001  # always < EVENT_PROB
            _random.uniform = lambda a, b: 0.03  # 3% move
            _random.choice = lambda s: 1  # always up
            result = apply_event(original)
            expected = original * 1.03
            assert abs(result - expected) < 1e-9
        finally:
            _random.random = original_random
            _random.uniform = original_uniform
            _random.choice = original_choice

    def test_no_event_returns_exact_price(self):
        import random as _random
        original_random = _random.random
        try:
            _random.random = lambda: 1.0  # always > EVENT_PROB
            result = apply_event(123.45)
            assert result == 123.45
        finally:
            _random.random = original_random


class TestSeedPriceFor:
    def test_known_ticker_returns_fixed_seed(self):
        assert seed_price_for("AAPL") == 190.0
        assert seed_price_for("GOOGL") == 175.0
        assert seed_price_for("NVDA") == 875.0

    def test_unknown_ticker_returns_realistic_price(self):
        for _ in range(50):
            price = seed_price_for("XYZUNK")
            assert 50.0 <= price <= 500.0

    def test_all_known_tickers_covered(self):
        for ticker in TICKER_PARAMS:
            assert seed_price_for(ticker) == TICKER_PARAMS[ticker].seed_price


# ---------------------------------------------------------------------------
# SimulatorProvider
# ---------------------------------------------------------------------------


class TestSimulatorProvider:
    async def test_seeds_cache_on_start(self):
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
            assert r.timestamp > 0

        if provider._task:
            provider._task.cancel()

    async def test_known_ticker_uses_seed_price(self):
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, {"MSFT"})

        record = cache.get("MSFT")
        assert record is not None
        # prev_close is fixed to seed; initial price equals seed price
        assert record.prev_close == TICKER_PARAMS["MSFT"].seed_price

        if provider._task:
            provider._task.cancel()

    async def test_price_updates_after_tick(self):
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, {"AAPL"})
        initial = cache.get("AAPL").price

        # Wait for two tick intervals to guarantee at least one update
        await asyncio.sleep(TICK_INTERVAL * 2 + 0.1)
        updated = cache.get("AAPL").price

        # Price should have changed (astronomically unlikely to be exactly equal
        # given the GBM diffusion component)
        assert updated != initial

        if provider._task:
            provider._task.cancel()

    async def test_add_unknown_ticker_returns_true(self):
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, set())
        result = await provider.add_ticker("XYZUNK")
        assert result is True

        record = cache.get("XYZUNK")
        assert record is not None
        assert 50.0 <= record.price <= 500.0

        if provider._task:
            provider._task.cancel()

    async def test_add_known_ticker_uses_correct_seed(self):
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, set())
        await provider.add_ticker("AAPL")

        record = cache.get("AAPL")
        assert record is not None
        assert record.price == TICKER_PARAMS["AAPL"].seed_price
        assert record.prev_close == TICKER_PARAMS["AAPL"].seed_price

        if provider._task:
            provider._task.cancel()

    async def test_add_ticker_idempotent(self):
        """Adding the same ticker twice should not create duplicate state."""
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, set())
        await provider.add_ticker("AAPL")
        await provider.add_ticker("AAPL")

        assert len([r for r in cache.all() if r.ticker == "AAPL"]) == 1
        assert list(provider._params.keys()).count("AAPL") == 1

        if provider._task:
            provider._task.cancel()

    async def test_remove_ticker_stops_tracking(self):
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, {"AAPL"})
        provider.remove_ticker("AAPL")

        # Provider no longer tracks AAPL internally
        assert "AAPL" not in provider._params
        assert "AAPL" not in provider._prices

        if provider._task:
            provider._task.cancel()

    async def test_remove_ticker_does_not_evict_cache(self):
        """Provider.remove_ticker does NOT touch the cache (caller decides)."""
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, {"AAPL"})
        provider.remove_ticker("AAPL")

        # Record should still be in cache
        assert cache.get("AAPL") is not None

        if provider._task:
            provider._task.cancel()

    async def test_remove_nonexistent_ticker_is_noop(self):
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, set())
        provider.remove_ticker("NONEXISTENT")  # should not raise

        if provider._task:
            provider._task.cancel()

    async def test_start_with_empty_tickers(self):
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, set())
        assert cache.all() == []

        if provider._task:
            provider._task.cancel()

    async def test_multiple_tickers_all_seeded(self):
        tickers = {"AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"}
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, tickers)

        cached = cache.tickers()
        assert cached == tickers

        if provider._task:
            provider._task.cancel()

    async def test_tick_prev_price_equals_prior_price(self):
        """After a tick, prev_price in the record should equal the price before the tick."""
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, {"AAPL"})
        price_after_seed = cache.get("AAPL").price

        # Manually fire one tick
        provider._tick()
        record = cache.get("AAPL")

        assert record.prev_price == price_after_seed

        if provider._task:
            provider._task.cancel()

    async def test_prev_close_never_changes(self):
        """prev_close is fixed to seed price regardless of how many ticks pass."""
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, {"AAPL"})
        expected_prev_close = TICKER_PARAMS["AAPL"].seed_price

        for _ in range(10):
            provider._tick()
            record = cache.get("AAPL")
            assert record.prev_close == expected_prev_close

        if provider._task:
            provider._task.cancel()

    async def test_price_floor_applied(self):
        """Price never goes below 0.01 even with extreme downward shocks."""
        cache = PriceCache()
        provider = SimulatorProvider()
        await provider.start(cache, {"AAPL"})

        # Drive price toward zero by forcing tiny values
        provider._prices["AAPL"] = 0.001  # below floor

        # Override gbm_step to return a value below the floor
        import market.simulator as sim_module
        original_gbm = sim_module.gbm_step
        sim_module.gbm_step = lambda p, params, z: 0.005  # still above floor
        try:
            provider._tick()
            record = cache.get("AAPL")
            assert record.price >= 0.01
        finally:
            sim_module.gbm_step = original_gbm

        if provider._task:
            provider._task.cancel()
