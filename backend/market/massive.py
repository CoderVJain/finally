from __future__ import annotations

import asyncio
import time

import httpx

from .interface import MarketDataProvider, PriceCache, PriceRecord

BASE_URL = "https://api.massive.com"
POLL_INTERVAL = 15.0  # seconds; safe for free tier (5 req/min ceiling)


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
                continue  # skip malformed entries

            prev_price = self._prev_prices.get(ticker, price)

            self._cache.update(
                PriceRecord(
                    ticker=ticker,
                    price=round(price, 2),
                    prev_price=round(prev_price, 2),
                    prev_close=round(prev_close, 2),
                    timestamp=now,
                )
            )
            self._prev_prices[ticker] = price

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
        snap = data.get("ticker") or {}
        last_trade = snap.get("lastTrade") or {}
        prev_day = snap.get("prevDay") or {}
        price = float(last_trade.get("p", 0) or 0)
        prev_close = float(prev_day.get("c", price) or price)
        if price > 0 and self._cache:
            self._cache.update(
                PriceRecord(
                    ticker=ticker,
                    price=round(price, 2),
                    prev_price=round(price, 2),
                    prev_close=round(prev_close, 2),
                    timestamp=time.time(),
                )
            )
            self._prev_prices[ticker] = price

        return True

    def remove_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker)
        self._prev_prices.pop(ticker, None)
        # NOTE: does NOT evict from PriceCache — caller handles that
