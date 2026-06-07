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
