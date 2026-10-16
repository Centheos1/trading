"""Redis live-bus cap must stay inside the collector's ElastiCache node."""

from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from market_data import EventKind, MarketEvent
import redis_md_bus
from redis_md_bus import RedisMDBus

# Measured on trading-md (cache.t4g.small) 2026-10-01: the depth stream used
# about 6 KB of Redis memory per entry, and the node refused writes at
# 1_103_269_725 bytes.
_DEPTH_BYTES_PER_ENTRY = 6 * 1024
_NODE_MAXMEMORY_BYTES = 1_103_269_725


class _FakeRedis:
    def __init__(self, *, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple] = []

    def xadd(self, key, fields, maxlen=None, approximate=None):
        if self.fail is not None:
            raise self.fail
        self.calls.append((key, maxlen, approximate, fields))
        return b"1-0"


def _event() -> MarketEvent:
    return MarketEvent(
        venue="binance",
        symbol="BTCUSDT",
        kind=EventKind.DEPTH_UPDATE,
        ts_exchange_ms=1,
        ts_recv_ms=2,
        payload={"bids": [[100.0, 1.0]], "asks": [[101.0, 1.0]]},
    )


class TestRedisMDBusCap(unittest.TestCase):
    def test_default_cap_fits_the_cache_node(self):
        self.assertEqual(redis_md_bus._DEFAULT_MAXLEN, 20_000)
        depth_budget = redis_md_bus._DEFAULT_MAXLEN * _DEPTH_BYTES_PER_ENTRY
        # Leave the other half for the trades stream and allocator overhead.
        self.assertLess(depth_budget, _NODE_MAXMEMORY_BYTES // 2)

    def test_publish_trims_with_the_default_cap(self):
        fake = _FakeRedis()
        bus = RedisMDBus(redis_url=None)
        bus._client = fake
        bus._maxlen = redis_md_bus._DEFAULT_MAXLEN
        bus.publish(_event())
        self.assertEqual(len(fake.calls), 1)
        key, maxlen, approximate, _fields = fake.calls[0]
        self.assertEqual(key, "md:binance:depth")
        self.assertEqual(maxlen, 20_000)
        self.assertTrue(approximate)

    def test_publish_failure_does_not_raise(self):
        fake = _FakeRedis(fail=RuntimeError("used memory > maxmemory"))
        bus = RedisMDBus(redis_url=None)
        bus._client = fake
        bus._maxlen = redis_md_bus._DEFAULT_MAXLEN
        bus.publish(_event())
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
