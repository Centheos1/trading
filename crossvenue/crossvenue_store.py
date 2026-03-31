"""
Cross-venue L1 price storage and replay for deterministic backtesting.

Stores (timestamp_ms, venue, price) observations in a simple CSV file.
During replay, events are loaded and fed to CrossVenueEngine in timestamp order.
"""

from __future__ import annotations

import csv
import os
from typing import List, Tuple


CrossVenueRecord = Tuple[int, str, float]  # (timestamp_ms, venue, price)


def save_crossvenue_prices(
    records: List[CrossVenueRecord],
    path: str,
) -> None:
    """Write cross-venue L1 prices to CSV."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_ms", "venue", "price"])
        for ts, venue, price in records:
            w.writerow([ts, venue, f"{price:.8f}"])


def load_crossvenue_prices(path: str) -> List[CrossVenueRecord]:
    """Load cross-venue L1 prices from CSV, sorted by timestamp."""
    if not os.path.exists(path):
        return []
    records = []
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["timestamp_ms"])
            venue = row["venue"]
            price = float(row["price"])
            records.append((ts, venue, price))
    records.sort(key=lambda r: r[0])
    return records


def replay_crossvenue(
    records: List[CrossVenueRecord],
    engine: "CrossVenueEngine",
) -> List["CrossVenueSnapshot"]:
    """Feed stored records into a CrossVenueEngine and collect snapshots.

    Returns a snapshot after each record that triggers a recomputation.
    """
    snapshots = []
    for ts, venue, price in records:
        engine.on_price(venue, price, ts)
        snap = engine.update(ts)
        if snap is not None:
            snapshots.append(snap)
    return snapshots
