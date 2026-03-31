"""Cross-venue confirmation layer (strategy.md §8.3, implementation_plan Phase 8).

Computes lead/lag, divergence, and correlation features from L1 prices
across multiple venues (Binance + Oanda). Feeds into Wave regime classification.
"""

from .crossvenue_engine import CrossVenueEngine, CrossVenueSnapshot, CrossVenueConfig

__all__ = ["CrossVenueEngine", "CrossVenueSnapshot", "CrossVenueConfig"]
