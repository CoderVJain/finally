"""Unit tests for PriceRecord and PriceCache."""

import threading
import time

import pytest

from market.interface import PriceCache, PriceRecord


def make_record(ticker: str, price: float = 100.0) -> PriceRecord:
    return PriceRecord(
        ticker=ticker,
        price=price,
        prev_price=price - 1.0,
        prev_close=95.0,
        timestamp=time.time(),
    )


class TestPriceRecord:
    def test_fields_accessible(self):
        r = make_record("AAPL", 190.0)
        assert r.ticker == "AAPL"
        assert r.price == 190.0
        assert r.prev_close == 95.0

    def test_pct_change_formula(self):
        r = PriceRecord(ticker="X", price=110.0, prev_price=100.0, prev_close=100.0, timestamp=0.0)
        pct = (r.price - r.prev_close) / r.prev_close
        assert abs(pct - 0.10) < 1e-9


class TestPriceCache:
    def test_update_and_get(self):
        cache = PriceCache()
        record = make_record("AAPL", 190.0)
        cache.update(record)
        result = cache.get("AAPL")
        assert result is not None
        assert result.price == 190.0

    def test_get_missing_ticker_returns_none(self):
        cache = PriceCache()
        assert cache.get("UNKNOWN") is None

    def test_update_overwrites_existing(self):
        cache = PriceCache()
        cache.update(make_record("AAPL", 190.0))
        cache.update(make_record("AAPL", 195.0))
        assert cache.get("AAPL").price == 195.0

    def test_all_returns_all_records(self):
        cache = PriceCache()
        cache.update(make_record("AAPL"))
        cache.update(make_record("TSLA"))
        cache.update(make_record("MSFT"))
        records = cache.all()
        assert len(records) == 3
        tickers = {r.ticker for r in records}
        assert tickers == {"AAPL", "TSLA", "MSFT"}

    def test_all_returns_snapshot_copy(self):
        cache = PriceCache()
        cache.update(make_record("AAPL"))
        snapshot = cache.all()
        # Mutating the snapshot doesn't affect the cache
        snapshot.clear()
        assert len(cache.all()) == 1

    def test_tickers_returns_set(self):
        cache = PriceCache()
        cache.update(make_record("AAPL"))
        cache.update(make_record("GOOGL"))
        assert cache.tickers() == {"AAPL", "GOOGL"}

    def test_remove_evicts_ticker(self):
        cache = PriceCache()
        cache.update(make_record("AAPL"))
        cache.remove("AAPL")
        assert cache.get("AAPL") is None
        assert "AAPL" not in cache.tickers()

    def test_remove_missing_ticker_is_noop(self):
        cache = PriceCache()
        cache.remove("NONEXISTENT")  # should not raise

    def test_empty_cache_all_returns_empty_list(self):
        cache = PriceCache()
        assert cache.all() == []

    def test_thread_safety_concurrent_updates(self):
        """Multiple threads writing to the cache should not corrupt data."""
        cache = PriceCache()
        errors: list[Exception] = []

        def writer(ticker: str, count: int) -> None:
            try:
                for i in range(count):
                    cache.update(make_record(ticker, float(i)))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(f"T{i}", 200))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All 10 tickers should be present
        assert len(cache.tickers()) == 10

    def test_thread_safety_concurrent_reads_and_writes(self):
        """Reads and writes from different threads should not deadlock or crash."""
        cache = PriceCache()
        cache.update(make_record("AAPL", 100.0))
        errors: list[Exception] = []
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    _ = cache.all()
                    _ = cache.get("AAPL")
            except Exception as exc:
                errors.append(exc)

        def writer() -> None:
            try:
                for i in range(500):
                    cache.update(make_record("AAPL", float(i)))
            except Exception as exc:
                errors.append(exc)

        reader_threads = [threading.Thread(target=reader) for _ in range(4)]
        writer_thread = threading.Thread(target=writer)

        for t in reader_threads:
            t.start()
        writer_thread.start()
        writer_thread.join()
        stop.set()
        for t in reader_threads:
            t.join()

        assert not errors
