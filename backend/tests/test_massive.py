"""Unit tests for MassiveProvider using respx to mock httpx."""

import pytest
import httpx
import respx

from market.interface import PriceCache
from market.massive import BASE_URL, MassiveProvider


SNAPSHOT_URL = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"

MOCK_SNAPSHOT_RESPONSE = {
    "status": "OK",
    "count": 1,
    "tickers": [
        {
            "ticker": "AAPL",
            "lastTrade": {"p": 191.50},
            "prevDay": {"c": 190.00},
            "todaysChangePerc": 0.79,
        }
    ],
}


def make_provider(poll_interval: float = 999.0) -> MassiveProvider:
    """Provider with a long poll interval so the background loop never fires during tests."""
    return MassiveProvider(api_key="test-key", poll_interval=poll_interval)


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"Authorization": "Bearer test-key"},
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# start() / _fetch_and_update()
# ---------------------------------------------------------------------------


class TestMassiveProviderStart:
    async def test_seeds_cache_on_start(self):
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(200, json=MOCK_SNAPSHOT_RESPONSE)
            )
            provider = make_provider()
            await provider.start(cache, {"AAPL"})

            record = cache.get("AAPL")
            assert record is not None
            assert record.price == 191.50
            assert record.prev_close == 190.00

            if provider._task:
                provider._task.cancel()
            await provider._client.aclose()

    async def test_prev_price_equals_price_on_first_fetch(self):
        """On first fetch prev_price should equal price (no prior data)."""
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(200, json=MOCK_SNAPSHOT_RESPONSE)
            )
            provider = make_provider()
            await provider.start(cache, {"AAPL"})

            record = cache.get("AAPL")
            assert record.prev_price == record.price

            if provider._task:
                provider._task.cancel()
            await provider._client.aclose()

    async def test_multiple_tickers_seeded(self):
        multi_response = {
            "status": "OK",
            "count": 2,
            "tickers": [
                {"ticker": "AAPL", "lastTrade": {"p": 191.50}, "prevDay": {"c": 190.00}},
                {"ticker": "TSLA", "lastTrade": {"p": 245.00}, "prevDay": {"c": 240.00}},
            ],
        }
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(200, json=multi_response)
            )
            provider = make_provider()
            await provider.start(cache, {"AAPL", "TSLA"})

            assert cache.get("AAPL").price == 191.50
            assert cache.get("TSLA").price == 245.00

            if provider._task:
                provider._task.cancel()
            await provider._client.aclose()

    async def test_prev_price_tracks_between_polls(self):
        """On the second fetch, prev_price should reflect the first fetch's price."""
        cache = PriceCache()
        second_response = {
            "status": "OK",
            "count": 1,
            "tickers": [
                {"ticker": "AAPL", "lastTrade": {"p": 193.00}, "prevDay": {"c": 190.00}}
            ],
        }
        # Use side_effect iterator to return different responses per call
        call_iter = iter([
            httpx.Response(200, json=MOCK_SNAPSHOT_RESPONSE),
            httpx.Response(200, json=second_response),
        ])

        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(side_effect=lambda req: next(call_iter))
            provider = make_provider()
            provider._cache = cache
            provider._tickers = {"AAPL"}
            provider._client = make_client()

            await provider._fetch_and_update()
            price_after_first = cache.get("AAPL").price  # 191.50

            await provider._fetch_and_update()
            record = cache.get("AAPL")
            assert record.price == 193.00
            assert record.prev_price == price_after_first

            await provider._client.aclose()

    async def test_skips_malformed_entries_with_zero_price(self):
        """Entries where lastTrade.p == 0 should be silently skipped."""
        bad_response = {
            "status": "OK",
            "count": 1,
            "tickers": [
                {"ticker": "AAPL", "lastTrade": {"p": 0}, "prevDay": {"c": 190.00}}
            ],
        }
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(200, json=bad_response)
            )
            provider = make_provider()
            provider._cache = cache
            provider._tickers = {"AAPL"}
            provider._client = make_client()

            await provider._fetch_and_update()
            assert cache.get("AAPL") is None

            await provider._client.aclose()

    async def test_skips_entries_missing_ticker_field(self):
        bad_response = {
            "status": "OK",
            "count": 1,
            "tickers": [{"lastTrade": {"p": 191.50}, "prevDay": {"c": 190.00}}],
        }
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(200, json=bad_response)
            )
            provider = make_provider()
            provider._cache = cache
            provider._tickers = {"AAPL"}
            provider._client = make_client()

            await provider._fetch_and_update()
            assert len(cache.all()) == 0

            await provider._client.aclose()

    async def test_prev_close_falls_back_to_price_when_missing(self):
        """When prevDay is absent, prev_close should fall back to the trade price."""
        no_prev_day = {
            "status": "OK",
            "count": 1,
            "tickers": [{"ticker": "AAPL", "lastTrade": {"p": 191.50}}],
        }
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(200, json=no_prev_day)
            )
            provider = make_provider()
            provider._cache = cache
            provider._tickers = {"AAPL"}
            provider._client = make_client()

            await provider._fetch_and_update()
            record = cache.get("AAPL")
            assert record is not None
            assert record.prev_close == record.price

            await provider._client.aclose()


# ---------------------------------------------------------------------------
# Network error handling
# ---------------------------------------------------------------------------


class TestMassiveProviderErrorHandling:
    async def test_connect_error_does_not_raise(self):
        """Network error is swallowed; cache retains previous values."""
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(side_effect=httpx.ConnectError("timeout"))
            provider = make_provider()
            provider._cache = cache
            provider._tickers = {"AAPL"}
            provider._client = make_client()

            # Should not raise; cache remains empty (was never seeded)
            await provider._fetch_and_update()
            assert cache.get("AAPL") is None

            await provider._client.aclose()

    async def test_http_error_does_not_raise(self):
        """5xx response is swallowed; cache retains previous values."""
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(503, json={"error": "Service Unavailable"})
            )
            provider = make_provider()
            provider._cache = cache
            provider._tickers = {"AAPL"}
            provider._client = make_client()

            await provider._fetch_and_update()
            assert cache.get("AAPL") is None

            await provider._client.aclose()

    async def test_empty_tickers_skips_fetch(self):
        """With no tracked tickers, _fetch_and_update is a no-op (no HTTP request)."""
        cache = PriceCache()
        with respx.mock:
            # No mock registered — any request would raise AssertionError
            provider = make_provider()
            provider._cache = cache
            provider._tickers = set()
            provider._client = make_client()

            await provider._fetch_and_update()  # should complete without a request

            await provider._client.aclose()

    async def test_none_client_skips_fetch(self):
        """If client is None (not yet started), _fetch_and_update is a no-op."""
        provider = make_provider()
        provider._cache = PriceCache()
        provider._tickers = {"AAPL"}
        provider._client = None
        await provider._fetch_and_update()  # should not raise

    async def test_stale_cache_served_after_error(self):
        """After a successful fetch, a subsequent error should leave the cache intact."""
        cache = PriceCache()
        call_iter = iter([
            httpx.Response(200, json=MOCK_SNAPSHOT_RESPONSE),
            httpx.ConnectError("timeout"),
        ])

        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(side_effect=lambda req: next(call_iter))
            provider = make_provider()
            provider._cache = cache
            provider._tickers = {"AAPL"}
            provider._client = make_client()

            # First fetch succeeds
            await provider._fetch_and_update()
            assert cache.get("AAPL").price == 191.50

            # Second fetch errors — cache should remain unchanged
            await provider._fetch_and_update()
            assert cache.get("AAPL").price == 191.50

            await provider._client.aclose()


# ---------------------------------------------------------------------------
# add_ticker()
# ---------------------------------------------------------------------------


class TestMassiveProviderAddTicker:
    async def test_add_valid_ticker(self):
        single_url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers/MSFT"
        cache = PriceCache()
        with respx.mock:
            respx.get(single_url).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "ticker": {
                            "ticker": "MSFT",
                            "lastTrade": {"p": 415.0},
                            "prevDay": {"c": 413.0},
                        }
                    },
                )
            )
            provider = make_provider()
            provider._cache = cache
            provider._client = make_client()

            result = await provider.add_ticker("MSFT")

            assert result is True
            assert "MSFT" in provider._tickers
            record = cache.get("MSFT")
            assert record is not None
            assert record.price == 415.0
            assert record.prev_close == 413.0

            await provider._client.aclose()

    async def test_add_invalid_ticker_returns_false(self):
        single_url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers/FAKE"
        cache = PriceCache()
        with respx.mock:
            respx.get(single_url).mock(
                return_value=httpx.Response(404, json={"status": "NOT_FOUND"})
            )
            provider = make_provider()
            provider._cache = cache
            provider._client = make_client()

            result = await provider.add_ticker("FAKE")

            assert result is False
            assert "FAKE" not in provider._tickers
            assert cache.get("FAKE") is None

            await provider._client.aclose()

    async def test_add_ticker_network_error_returns_false(self):
        single_url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers/AAPL"
        cache = PriceCache()
        with respx.mock:
            respx.get(single_url).mock(side_effect=httpx.ConnectError("timeout"))
            provider = make_provider()
            provider._cache = cache
            provider._client = make_client()

            result = await provider.add_ticker("AAPL")

            assert result is False
            assert "AAPL" not in provider._tickers

            await provider._client.aclose()

    async def test_add_ticker_no_client_returns_false(self):
        provider = make_provider()
        provider._client = None
        result = await provider.add_ticker("AAPL")
        assert result is False

    async def test_add_ticker_prev_price_seeded_for_next_poll(self):
        """After add_ticker, the next poll uses the add_ticker price as prev_price."""
        single_url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers/AAPL"
        cache = PriceCache()
        with respx.mock:
            respx.get(single_url).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "ticker": {
                            "ticker": "AAPL",
                            "lastTrade": {"p": 191.50},
                            "prevDay": {"c": 190.00},
                        }
                    },
                )
            )
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status": "OK",
                        "tickers": [
                            {
                                "ticker": "AAPL",
                                "lastTrade": {"p": 193.00},
                                "prevDay": {"c": 190.00},
                            }
                        ],
                    },
                )
            )
            provider = make_provider()
            provider._cache = cache
            provider._client = make_client()

            await provider.add_ticker("AAPL")
            await provider._fetch_and_update()

            record = cache.get("AAPL")
            assert record.price == 193.00
            assert record.prev_price == 191.50  # price from add_ticker

            await provider._client.aclose()

    async def test_add_ticker_no_price_in_response(self):
        """add_ticker returns True even if the response has no price data."""
        single_url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers/AAPL"
        cache = PriceCache()
        with respx.mock:
            respx.get(single_url).mock(
                return_value=httpx.Response(200, json={"ticker": {}})
            )
            provider = make_provider()
            provider._cache = cache
            provider._client = make_client()

            result = await provider.add_ticker("AAPL")
            assert result is True
            assert "AAPL" in provider._tickers
            # Cache not seeded because price == 0
            assert cache.get("AAPL") is None

            await provider._client.aclose()


# ---------------------------------------------------------------------------
# remove_ticker()
# ---------------------------------------------------------------------------


class TestMassiveProviderRemoveTicker:
    async def test_remove_ticker_stops_tracking(self):
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(200, json=MOCK_SNAPSHOT_RESPONSE)
            )
            provider = make_provider()
            await provider.start(cache, {"AAPL"})
            provider.remove_ticker("AAPL")

            assert "AAPL" not in provider._tickers

            if provider._task:
                provider._task.cancel()
            await provider._client.aclose()

    async def test_remove_ticker_does_not_evict_cache(self):
        """Provider.remove_ticker does NOT touch PriceCache."""
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(200, json=MOCK_SNAPSHOT_RESPONSE)
            )
            provider = make_provider()
            await provider.start(cache, {"AAPL"})
            provider.remove_ticker("AAPL")

            assert cache.get("AAPL") is not None

            if provider._task:
                provider._task.cancel()
            await provider._client.aclose()

    async def test_remove_nonexistent_ticker_is_noop(self):
        provider = make_provider()
        provider.remove_ticker("NONEXISTENT")  # should not raise

    async def test_remove_clears_prev_price_state(self):
        cache = PriceCache()
        with respx.mock:
            respx.get(SNAPSHOT_URL).mock(
                return_value=httpx.Response(200, json=MOCK_SNAPSHOT_RESPONSE)
            )
            provider = make_provider()
            await provider.start(cache, {"AAPL"})

            assert "AAPL" in provider._prev_prices
            provider.remove_ticker("AAPL")
            assert "AAPL" not in provider._prev_prices

            if provider._task:
                provider._task.cancel()
            await provider._client.aclose()


# ---------------------------------------------------------------------------
# create_market_provider factory
# ---------------------------------------------------------------------------


class TestCreateMarketProvider:
    def test_returns_simulator_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
        from market import create_market_provider
        from market.simulator import SimulatorProvider

        provider = create_market_provider()
        assert isinstance(provider, SimulatorProvider)

    def test_returns_massive_when_api_key_set(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "some-real-key")
        from market import create_market_provider

        provider = create_market_provider()
        assert isinstance(provider, MassiveProvider)

    def test_returns_simulator_when_api_key_empty_string(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "")
        from market import create_market_provider
        from market.simulator import SimulatorProvider

        provider = create_market_provider()
        assert isinstance(provider, SimulatorProvider)

    def test_returns_simulator_when_api_key_whitespace(self, monkeypatch):
        monkeypatch.setenv("MASSIVE_API_KEY", "   ")
        from market import create_market_provider
        from market.simulator import SimulatorProvider

        provider = create_market_provider()
        assert isinstance(provider, SimulatorProvider)
