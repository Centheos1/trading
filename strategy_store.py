"""
Phase 4: HDF5 persistence for strategy signals, snapshots, and session events.

Uses a separate .h5 file per symbol to avoid contention with the C++ TickStore.
File path: data/{symbol}_strategy.h5

Schema version 2 layout:
    /{symbol}/
        strategy_signals/     (compound dataset)
        strategy_snapshots/   (compound dataset)
        session_events/       (compound dataset)
    attrs['schema_version'] = 2
"""
import json
import logging
import os
from typing import List, Optional

import h5py
import numpy as np

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

_SIGNAL_DTYPE = np.dtype([
    ("timestamp", "i8"),
    ("signal_type", "S64"),
    ("category", "S64"),
    ("side", "S8"),
    ("price", "f8"),
    ("quantity", "f8"),
    ("archetype", "S32"),
    ("lifecycle", "S32"),
    ("wave_regime", "S32"),
    ("tide_bias", "S16"),
    ("risk_pct", "f8"),
    ("description", "S256"),
])

_SNAPSHOT_DTYPE = np.dtype([
    ("timestamp", "i8"),
    ("wave_regime", "S32"),
    ("tide_bias", "S16"),
    ("liquidity_state", "S32"),
    ("lifecycle_state", "S32"),
    ("microprice", "f8"),
    ("imbalance", "f8"),
    ("unrealized_pnl", "f8"),
    ("consumed_es", "f8"),
    ("es_budget", "f8"),
    ("cumulative_pnl", "f8"),
])

_EVENT_DTYPE = np.dtype([
    ("timestamp", "i8"),
    ("event_type", "S32"),
    ("details", "S512"),
])


class StrategyStore:
    """HDF5 store for strategy signals, snapshots, and session events.

    Opens (or creates) ``data/{symbol}_strategy.h5`` with schema_version=2.
    Buffers snapshot rows and flushes in batches to limit I/O.
    """

    _FLUSH_BATCH = 50

    def __init__(self, symbol: str, data_dir: str = "data"):
        os.makedirs(data_dir, exist_ok=True)
        self._symbol = symbol.upper()
        self._path = os.path.join(data_dir, f"{self._symbol}_strategy.h5")
        self._hf: Optional[h5py.File] = None
        self._snapshot_buffer: List[np.void] = []
        self._open()

    @property
    def path(self) -> str:
        return self._path

    @property
    def schema_version(self) -> int:
        if self._hf is None:
            return 0
        return int(self._hf.attrs.get("schema_version", 0))

    def _open(self):
        self._hf = h5py.File(self._path, "a")
        if "schema_version" not in self._hf.attrs:
            self._hf.attrs["schema_version"] = SCHEMA_VERSION

        grp = self._hf.require_group(self._symbol)
        if "strategy_signals" not in grp:
            grp.create_dataset(
                "strategy_signals", shape=(0,), maxshape=(None,),
                dtype=_SIGNAL_DTYPE)
        if "strategy_snapshots" not in grp:
            grp.create_dataset(
                "strategy_snapshots", shape=(0,), maxshape=(None,),
                dtype=_SNAPSHOT_DTYPE)
        if "session_events" not in grp:
            grp.create_dataset(
                "session_events", shape=(0,), maxshape=(None,),
                dtype=_EVENT_DTYPE)
        self._hf.flush()

    # ------------------------------------------------------------------
    # Write: strategy signals
    # ------------------------------------------------------------------

    def write_signal(self, entry) -> None:
        """Write a single SignalEntry to the strategy_signals dataset."""
        if self._hf is None:
            return
        row = np.array([(
            int(getattr(entry, "timestamp", 0)),
            _s(getattr(entry, "signal_type", "")),
            _s(str(getattr(getattr(entry, "category", ""), "value", ""))),
            _s(getattr(entry, "side", "")),
            float(getattr(entry, "price", 0.0)),
            float(getattr(entry, "quantity", 0.0)),
            _s(getattr(entry, "archetype", "")),
            _s(getattr(entry, "lifecycle_state", "")),
            _s(getattr(entry, "wave_regime", "")),
            _s(getattr(entry, "tide_bias", "")),
            float(getattr(entry, "risk_budget_pct", 0.0)),
            _s(getattr(entry, "description", "")),
        )], dtype=_SIGNAL_DTYPE)
        ds = self._hf[self._symbol]["strategy_signals"]
        ds.resize(ds.shape[0] + 1, axis=0)
        ds[-1] = row[0]
        self._hf.flush()

    # ------------------------------------------------------------------
    # Write: strategy snapshots (buffered)
    # ------------------------------------------------------------------

    def buffer_snapshot(self, timestamp_ms: int, snap) -> None:
        """Buffer a strategy snapshot row. Call flush_snapshots() periodically."""
        row = np.void(np.array([(
            int(timestamp_ms),
            _s(_nested(snap, "wave.regime", "")),
            _s(_nested(snap, "tide.bias", "")),
            _s(_nested(snap, "ripple.liquidity_state", "")),
            _s(_nested(snap, "trade.state", "")),
            float(_nested(snap, "ripple.microprice", 0.0)),
            float(_nested(snap, "ripple.imbalance", 0.0)),
            float(_nested(snap, "trade.unrealized_pnl", 0.0)),
            float(_nested(snap, "risk.consumed_es", 0.0)),
            float(_nested(snap, "risk.es_budget", 0.0)),
            float(_nested(snap, "trade.cumulative_pnl", 0.0)),
        )], dtype=_SNAPSHOT_DTYPE)[0])
        self._snapshot_buffer.append(row)
        if len(self._snapshot_buffer) >= self._FLUSH_BATCH:
            self.flush_snapshots()

    def flush_snapshots(self) -> None:
        """Flush buffered snapshots to disk."""
        if not self._snapshot_buffer or self._hf is None:
            return
        ds = self._hf[self._symbol]["strategy_snapshots"]
        n = len(self._snapshot_buffer)
        old_len = ds.shape[0]
        ds.resize(old_len + n, axis=0)
        arr = np.array(self._snapshot_buffer, dtype=_SNAPSHOT_DTYPE)
        ds[old_len:old_len + n] = arr
        self._snapshot_buffer.clear()
        self._hf.flush()

    # ------------------------------------------------------------------
    # Write: session events
    # ------------------------------------------------------------------

    def write_event(self, timestamp_ms: int, event_type: str,
                    details: Optional[dict] = None) -> None:
        """Write a session event (ARM, DISARM, CONNECT, DISCONNECT, ERROR)."""
        if self._hf is None:
            return
        details_str = json.dumps(details or {})
        row = np.array([(
            int(timestamp_ms),
            _s(event_type),
            _s(details_str),
        )], dtype=_EVENT_DTYPE)
        ds = self._hf[self._symbol]["session_events"]
        ds.resize(ds.shape[0] + 1, axis=0)
        ds[-1] = row[0]
        self._hf.flush()

    # ------------------------------------------------------------------
    # Read paths (for replay-with-overlay)
    # ------------------------------------------------------------------

    def read_signals(self, from_ms: int = 0, to_ms: int = 0) -> List[dict]:
        """Read strategy signals, optionally filtered by time range."""
        if self._hf is None:
            return []
        grp = self._hf.get(self._symbol)
        if grp is None or "strategy_signals" not in grp:
            return []
        ds = grp["strategy_signals"]
        if ds.shape[0] == 0:
            return []
        data = ds[:]
        rows = []
        for r in data:
            ts = int(r["timestamp"])
            if from_ms and ts < from_ms:
                continue
            if to_ms and ts > to_ms:
                continue
            rows.append(_signal_row_to_dict(r))
        return rows

    def read_snapshots(self, from_ms: int = 0, to_ms: int = 0) -> List[dict]:
        """Read strategy snapshots, optionally filtered by time range."""
        if self._hf is None:
            return []
        grp = self._hf.get(self._symbol)
        if grp is None or "strategy_snapshots" not in grp:
            return []
        ds = grp["strategy_snapshots"]
        if ds.shape[0] == 0:
            return []
        data = ds[:]
        rows = []
        for r in data:
            ts = int(r["timestamp"])
            if from_ms and ts < from_ms:
                continue
            if to_ms and ts > to_ms:
                continue
            rows.append(_snapshot_row_to_dict(r))
        return rows

    def read_events(self, from_ms: int = 0, to_ms: int = 0) -> List[dict]:
        """Read session events, optionally filtered by time range."""
        if self._hf is None:
            return []
        grp = self._hf.get(self._symbol)
        if grp is None or "session_events" not in grp:
            return []
        ds = grp["session_events"]
        if ds.shape[0] == 0:
            return []
        data = ds[:]
        rows = []
        for r in data:
            ts = int(r["timestamp"])
            if from_ms and ts < from_ms:
                continue
            if to_ms and ts > to_ms:
                continue
            rows.append(_event_row_to_dict(r))
        return rows

    def signal_count(self) -> int:
        if self._hf is None:
            return 0
        grp = self._hf.get(self._symbol)
        if grp is None or "strategy_signals" not in grp:
            return 0
        return grp["strategy_signals"].shape[0]

    def snapshot_count(self) -> int:
        if self._hf is None:
            return 0
        grp = self._hf.get(self._symbol)
        if grp is None or "strategy_snapshots" not in grp:
            return 0
        return grp["strategy_snapshots"].shape[0]

    def event_count(self) -> int:
        if self._hf is None:
            return 0
        grp = self._hf.get(self._symbol)
        if grp is None or "session_events" not in grp:
            return 0
        return grp["session_events"].shape[0]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Flush snapshot buffer and HDF5 file."""
        self.flush_snapshots()
        if self._hf:
            self._hf.flush()

    def close(self) -> None:
        """Flush and close the HDF5 file."""
        self.flush()
        if self._hf:
            self._hf.close()
            self._hf = None

    # ------------------------------------------------------------------
    # Static helpers for backward-compatible reads
    # ------------------------------------------------------------------

    @staticmethod
    def is_legacy(path: str) -> bool:
        """Check if a file is legacy (schema_version < 2 or missing)."""
        if not os.path.exists(path):
            return True
        with h5py.File(path, "r") as f:
            return int(f.attrs.get("schema_version", 0)) < SCHEMA_VERSION

    @staticmethod
    def open_readonly(path: str, symbol: str) -> Optional["StrategyStore"]:
        """Open an existing store read-only. Returns None if file missing."""
        if not os.path.exists(path):
            return None
        store = object.__new__(StrategyStore)
        store._symbol = symbol.upper()
        store._path = path
        store._snapshot_buffer = []
        store._hf = h5py.File(path, "r")
        grp = store._hf.get(store._symbol)
        if grp is None:
            store._hf.close()
            return None
        return store


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _s(val) -> bytes:
    """Convert a value to bytes for HDF5 fixed-length string fields."""
    s = str(val) if val is not None else ""
    if s.startswith("SignalCategory."):
        s = s.split(".")[-1]
    return s.encode("utf-8", errors="replace")


def _nested(obj, dotpath: str, default=""):
    """Safely access nested attributes like 'wave.regime'."""
    parts = dotpath.split(".")
    cur = obj
    for p in parts:
        cur = getattr(cur, p, None)
        if cur is None:
            return default
    if isinstance(cur, (int, float)):
        return cur
    return str(cur).split(".")[-1] if cur is not None else default


def _signal_row_to_dict(r) -> dict:
    return {
        "timestamp": int(r["timestamp"]),
        "signal_type": r["signal_type"].decode("utf-8", errors="replace").rstrip("\x00"),
        "category": r["category"].decode("utf-8", errors="replace").rstrip("\x00"),
        "side": r["side"].decode("utf-8", errors="replace").rstrip("\x00"),
        "price": float(r["price"]),
        "quantity": float(r["quantity"]),
        "archetype": r["archetype"].decode("utf-8", errors="replace").rstrip("\x00"),
        "lifecycle": r["lifecycle"].decode("utf-8", errors="replace").rstrip("\x00"),
        "wave_regime": r["wave_regime"].decode("utf-8", errors="replace").rstrip("\x00"),
        "tide_bias": r["tide_bias"].decode("utf-8", errors="replace").rstrip("\x00"),
        "risk_pct": float(r["risk_pct"]),
        "description": r["description"].decode("utf-8", errors="replace").rstrip("\x00"),
    }


def _snapshot_row_to_dict(r) -> dict:
    return {
        "timestamp": int(r["timestamp"]),
        "wave_regime": r["wave_regime"].decode("utf-8", errors="replace").rstrip("\x00"),
        "tide_bias": r["tide_bias"].decode("utf-8", errors="replace").rstrip("\x00"),
        "liquidity_state": r["liquidity_state"].decode("utf-8", errors="replace").rstrip("\x00"),
        "lifecycle_state": r["lifecycle_state"].decode("utf-8", errors="replace").rstrip("\x00"),
        "microprice": float(r["microprice"]),
        "imbalance": float(r["imbalance"]),
        "unrealized_pnl": float(r["unrealized_pnl"]),
        "consumed_es": float(r["consumed_es"]),
        "es_budget": float(r["es_budget"]),
        "cumulative_pnl": float(r["cumulative_pnl"]),
    }


def _event_row_to_dict(r) -> dict:
    return {
        "timestamp": int(r["timestamp"]),
        "event_type": r["event_type"].decode("utf-8", errors="replace").rstrip("\x00"),
        "details": r["details"].decode("utf-8", errors="replace").rstrip("\x00"),
    }
