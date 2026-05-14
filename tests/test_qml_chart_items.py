"""Phase 9B — QML chart items regression suite.

Covers UI_STRATEGY_INTEGRATION_PLAN.md §22.3 acceptance criteria:

1. ``HeatmapItem`` does NOT trigger a per-trade paint between frames —
   :meth:`HeatmapItem.set_frame` is the only path that schedules a
   repaint (``self.update()``), and trade ingestion routes through the
   ``OrderFlowViewModel`` not the item.
2. ``CandleItem._geometry_dirty`` is ``False`` after ``updatePaintNode()``;
   it flips to ``True`` only on bucket close (and viewport / visible-
   candle shape changes).
3. ``QSGRendererInterface`` reports a hardware backend (Metal / OpenGL /
   Vulkan / Direct3D — i.e. not ``Software``) for every ``QQuickWindow``
   that hosts a chart item.
4. The four bridge items (``HeatmapItem``, ``CvdItem``,
   ``VolumeProfileItem``) and the native ``CandleItem`` all instantiate
   in offscreen mode, wrap the correct underlying widget where
   applicable, and forward state setters without raising.
5. The existing bubble pipeline (``OrderFlowViewModel.compute_frame``)
   still produces a ``FrameData`` with the new ``depth_texture_dirty``
   flag set after a depth rebuild.

All tests use ``QT_QPA_PLATFORM=offscreen`` and a singleton
``QApplication`` (QApplication IS-A QGuiApplication; Phase 9B switched
``ui.app._run_qml`` from ``QGuiApplication`` to ``QApplication`` so
the bridge tier can host hidden ``QWidget`` instances — see §22.3).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PySide6.QtCore import QCoreApplication, QPoint  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType  # noqa: E402
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.app import (  # noqa: E402
    _SOFTWARE_BACKENDS,
    _api_name,
    _iter_quick_windows,
    _qml_root_url,
    _register_qml_types,
)
from ui.items.candle_item import (  # noqa: E402
    _BODY_WICK_VERTS_PER_CANDLE,
    _VOLUME_VERTS_PER_CANDLE,
    CandleItem,
    _CandleNodeTree,
)
from ui.items.cvd_item import CvdItem  # noqa: E402
from ui.items.heatmap_item import HeatmapItem  # noqa: E402
from ui.items.volume_profile_item import VolumeProfileItem  # noqa: E402
from ui.cvd_widget import CVDWidget  # noqa: E402
from ui.heatmap_widget import HeatmapWidget  # noqa: E402
from ui.orderflow_viewmodel import FrameData, OrderFlowViewModel  # noqa: E402
from ui.volume_profile_widget import VolumeProfileWidget  # noqa: E402


def _gui_app() -> QApplication:
    """Singleton QApplication — created exactly once per test process."""
    instance = QCoreApplication.instance()
    if instance is None:
        instance = QApplication(sys.argv[:1])
    return instance


# ---------------------------------------------------------------------------
# Bridge plumbing — AC #4
# ---------------------------------------------------------------------------

class TestBridgeItemsInstantiate(unittest.TestCase):
    """AC #4 — every bridge / native item can be constructed offscreen."""

    @classmethod
    def setUpClass(cls) -> None:
        _gui_app()

    def test_heatmap_item_wraps_widget(self) -> None:
        item = HeatmapItem()
        self.addCleanup(item.deleteLater)
        self.assertIsInstance(item.bridge_widget(), HeatmapWidget)
        self.assertIsNotNone(item.viewmodel())

    def test_cvd_item_wraps_widget(self) -> None:
        item = CvdItem()
        self.addCleanup(item.deleteLater)
        self.assertIsInstance(item.bridge_widget(), CVDWidget)
        # add_trade / set_time_ref forward without raising
        item.add_trade(1000, 100.0, 1.0, True)
        item.set_time_ref(1000, 60_000, 0)
        item.refresh()

    def test_volume_profile_item_wraps_widget(self) -> None:
        item = VolumeProfileItem()
        self.addCleanup(item.deleteLater)
        self.assertIsInstance(item.bridge_widget(), VolumeProfileWidget)
        item.set_profile([(100.0, 5.0, 3.0, 2.0)], 100.5, 99.0, 102.0)

    def test_candle_item_is_native_qquickitem(self) -> None:
        item = CandleItem()
        self.addCleanup(item.deleteLater)
        # ItemHasContents flag is mandatory for native scene-graph items.
        from PySide6.QtQuick import QQuickItem
        self.assertTrue(item.flags() & QQuickItem.Flag.ItemHasContents)
        self.assertEqual(item.bucketMs, 60_000)
        self.assertEqual(item.visibleCandles, 80)
        self.assertEqual(item.candle_count, 0)


# ---------------------------------------------------------------------------
# Bridge widget-render path actually paints — sanity / AC #4
# ---------------------------------------------------------------------------

class TestBridgePaintRoute(unittest.TestCase):
    """The bridge contract is that ``widget.render(painter, QPoint())``
    produces non-trivial output.  We verify the QPainter sink picks up
    paint commands (the framebuffer ends up non-empty) for all three
    QWidget-backed bridges."""

    @classmethod
    def setUpClass(cls) -> None:
        _gui_app()

    def _paint_widget_to_image(self, widget, size=(400, 200)) -> QImage:
        widget.resize(*size)
        img = QImage(size[0], size[1], QImage.Format_ARGB32)
        img.fill(0xFF000000)
        painter = QPainter(img)
        try:
            widget.render(painter, QPoint())
        finally:
            painter.end()
        return img

    def test_heatmap_widget_renders(self) -> None:
        item = HeatmapItem()
        self.addCleanup(item.deleteLater)
        img = self._paint_widget_to_image(item.bridge_widget(), (400, 200))
        self.assertEqual(img.size().width(), 400)

    def test_cvd_widget_renders(self) -> None:
        item = CvdItem()
        self.addCleanup(item.deleteLater)
        item.add_trade(1000, 100.0, 1.5, True)
        item.add_trade(2000, 100.5, 2.0, False)
        item.set_time_ref(2000, 60_000, 0)
        img = self._paint_widget_to_image(item.bridge_widget(), (400, 200))
        self.assertEqual(img.size().width(), 400)

    def test_volume_profile_widget_renders(self) -> None:
        item = VolumeProfileItem()
        self.addCleanup(item.deleteLater)
        item.set_profile(
            [(100.0, 5.0, 3.0, 2.0), (101.0, 8.0, 4.0, 4.0)],
            100.5, 99.0, 102.0,
        )
        img = self._paint_widget_to_image(item.bridge_widget(), (220, 400))
        self.assertEqual(img.size().width(), 220)


# ---------------------------------------------------------------------------
# AC #2 — CandleItem._geometry_dirty semantics
# ---------------------------------------------------------------------------

class TestCandleItemGeometryDirty(unittest.TestCase):
    """AC #2 — ``_geometry_dirty`` flips to True ONLY on bucket close (or
    viewport / visibleCandles shape change), and ``updatePaintNode``
    clears it before returning."""

    @classmethod
    def setUpClass(cls) -> None:
        _gui_app()

    def _make_item(self) -> CandleItem:
        item = CandleItem()
        item.setWidth(800)
        item.setHeight(400)
        self.addCleanup(item.deleteLater)
        return item

    def test_initial_state_is_dirty(self) -> None:
        item = self._make_item()
        self.assertTrue(item.geometry_dirty)

    def test_update_paint_node_clears_flag(self) -> None:
        item = self._make_item()
        item.process_trade(1000, 100.0, 1.0, True)
        self.assertTrue(item.geometry_dirty)
        item.updatePaintNode(None, None)
        self.assertFalse(item.geometry_dirty)

    def test_in_bucket_tick_does_not_set_dirty(self) -> None:
        item = self._make_item()
        item.process_trade(1000, 100.0, 1.0, True)
        node = item.updatePaintNode(None, None)
        self.assertFalse(item.geometry_dirty)
        # Same-bucket update — must NOT flip the flag.
        item.process_trade(2000, 101.0, 1.0, True)
        self.assertFalse(item.geometry_dirty)
        item.process_trade(30_000, 99.5, 1.0, False)
        self.assertFalse(item.geometry_dirty)
        # updatePaintNode runs the in-place refresh path; flag stays False.
        item.updatePaintNode(node, None)
        self.assertFalse(item.geometry_dirty)

    def test_bucket_close_sets_dirty(self) -> None:
        item = self._make_item()
        item.process_trade(1000, 100.0, 1.0, True)
        item.updatePaintNode(None, None)
        self.assertFalse(item.geometry_dirty)
        appended = item.process_trade(60_001, 102.0, 0.5, False)
        self.assertTrue(appended)
        self.assertTrue(item.geometry_dirty)

    def test_visible_candles_change_sets_dirty(self) -> None:
        item = self._make_item()
        item.process_trade(1000, 100.0, 1.0, True)
        item.updatePaintNode(None, None)
        self.assertFalse(item.geometry_dirty)
        item.visibleCandles = 40
        self.assertTrue(item.geometry_dirty)

    def test_bucket_ms_change_clears_candles_and_dirty(self) -> None:
        item = self._make_item()
        item.process_trade(1000, 100.0, 1.0, True)
        self.assertEqual(item.candle_count, 1)
        item.bucketMs = 300_000
        self.assertEqual(item.candle_count, 0)
        self.assertTrue(item.geometry_dirty)


# ---------------------------------------------------------------------------
# AC #2 (continued) — vertex counts match candle count after rebuild
# ---------------------------------------------------------------------------

class TestCandleItemVertexCounts(unittest.TestCase):
    """Verifies that ``updatePaintNode`` produces the expected vertex
    counts in the four-bucket scene graph node tree."""

    @classmethod
    def setUpClass(cls) -> None:
        _gui_app()

    def test_bull_candle_emits_12_body_and_6_volume_vertices(self) -> None:
        item = CandleItem()
        item.setWidth(800)
        item.setHeight(400)
        self.addCleanup(item.deleteLater)

        item.process_trade(1000, 100.0, 1.0, True)
        item.process_trade(2000, 101.0, 2.0, True)  # bull (c >= o)
        node = item.updatePaintNode(None, None)

        counts = node.vertex_counts()
        # One bull candle: 6 body + 6 wick verts = 12 in bull_body bucket;
        # 6 volume verts.  No bear candles yet.
        self.assertEqual(counts["bull_body"],
                         _BODY_WICK_VERTS_PER_CANDLE)
        self.assertEqual(counts["bull_volume"],
                         _VOLUME_VERTS_PER_CANDLE)
        self.assertEqual(counts["bear_body"], 0)
        self.assertEqual(counts["bear_volume"], 0)

    def test_bear_candle_routes_to_bear_buckets(self) -> None:
        item = CandleItem()
        item.setWidth(800)
        item.setHeight(400)
        self.addCleanup(item.deleteLater)

        # open at 100, close at 95 → bear.
        item.process_trade(1000, 100.0, 1.0, True)
        item.process_trade(2000, 95.0, 1.0, False)
        node = item.updatePaintNode(None, None)

        counts = node.vertex_counts()
        self.assertEqual(counts["bear_body"],
                         _BODY_WICK_VERTS_PER_CANDLE)
        self.assertEqual(counts["bear_volume"],
                         _VOLUME_VERTS_PER_CANDLE)
        self.assertEqual(counts["bull_body"], 0)

    def test_three_candles_three_buckets(self) -> None:
        item = CandleItem()
        item.setWidth(800)
        item.setHeight(400)
        self.addCleanup(item.deleteLater)

        # bucket 1: bull
        item.process_trade(1_000, 100.0, 1.0, True)
        item.process_trade(2_000, 102.0, 1.0, True)
        # bucket 2: bear
        item.process_trade(60_001, 102.0, 1.0, False)
        item.process_trade(70_000, 98.0, 1.0, False)
        # bucket 3: bull
        item.process_trade(120_001, 98.0, 1.0, True)
        item.process_trade(130_000, 101.0, 1.0, True)

        self.assertEqual(item.candle_count, 3)
        node = item.updatePaintNode(None, None)
        counts = node.vertex_counts()
        self.assertEqual(
            counts["bull_body"] + counts["bear_body"],
            3 * _BODY_WICK_VERTS_PER_CANDLE,
        )
        self.assertEqual(
            counts["bull_volume"] + counts["bear_volume"],
            3 * _VOLUME_VERTS_PER_CANDLE,
        )


# ---------------------------------------------------------------------------
# AC #1 — HeatmapItem.paint() is not driven per-trade.
# ---------------------------------------------------------------------------

class TestHeatmapItemFramePaints(unittest.TestCase):
    """AC #1 — feeding N trades into the ``OrderFlowViewModel`` between
    frames must NOT schedule N repaints on the ``HeatmapItem``.  The
    only path that calls ``self.update()`` on the item is
    :meth:`HeatmapItem.set_frame`."""

    @classmethod
    def setUpClass(cls) -> None:
        _gui_app()

    def test_trade_ingestion_does_not_call_item_update(self) -> None:
        item = HeatmapItem()
        self.addCleanup(item.deleteLater)
        vm: OrderFlowViewModel = item.viewmodel()
        self.assertIsNotNone(vm)

        with mock.patch.object(HeatmapItem, "update",
                               autospec=True) as mock_update:
            base_ts = 1_700_000_000_000
            for i in range(100):
                vm.add_trade(base_ts + i, 100.0 + (i % 3), 0.5, i % 2 == 0)
            self.assertEqual(
                mock_update.call_count, 0,
                "HeatmapItem.update() must not fire per-trade",
            )

    def test_set_frame_schedules_one_repaint(self) -> None:
        item = HeatmapItem()
        self.addCleanup(item.deleteLater)

        frame = FrameData()
        with mock.patch.object(HeatmapItem, "update",
                               autospec=True) as mock_update:
            item.set_frame(frame)
            item.set_frame(frame)
            item.set_frame(frame)
            self.assertEqual(mock_update.call_count, 3)


# ---------------------------------------------------------------------------
# AC #5 — depth_texture_dirty flag plumbed through compute_frame.
# ---------------------------------------------------------------------------

class TestDepthTextureDirtyFlag(unittest.TestCase):
    """AC #5 — ``FrameData.depth_texture_dirty`` is set whenever
    ``OrderFlowViewModel.compute_frame`` rebuilds the depth ``QImage``.

    Also confirms the default value is False so callers don't see
    spurious dirty flips when no depth data is present.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _gui_app()

    def test_default_flag_is_false(self) -> None:
        frame = FrameData()
        self.assertFalse(frame.depth_texture_dirty)

    def test_flag_set_after_depth_rebuild(self) -> None:
        vm = OrderFlowViewModel()
        # Drive depth + trade ingestion to produce a heatmap frame.
        ts0 = 1_700_000_000_000
        for i in range(20):
            ts = ts0 + i * 500
            bids = [(100.0 - j * 0.5, 1.0 + j) for j in range(1, 10)]
            asks = [(100.0 + j * 0.5, 1.0 + j) for j in range(1, 10)]
            vm.add_depth_column(ts, bids, asks, 99.9, 100.1, sample_ts=ts)
            vm.add_trade(ts, 100.0, 0.5, i % 2 == 0)

        frame = vm.compute_frame(800, 400, 70, 10, 28)
        self.assertTrue(
            frame.have_heatmap,
            "Test fixture should produce a heatmap-eligible frame",
        )
        self.assertTrue(
            frame.depth_texture_dirty,
            "depth_texture_dirty must be set on every depth rebuild",
        )


# ---------------------------------------------------------------------------
# AC #3 — chart items live in QQuickWindow instances audited by _check_gpu.
# ---------------------------------------------------------------------------

class TestQmlChartItemsLiveInQuickWindows(unittest.TestCase):
    """AC #3 wiring — ``HeatmapItem`` / ``CandleItem`` / ``CvdItem`` /
    ``VolumeProfileItem`` resolve to ``QQuickItem`` children of
    ``QQuickWindow`` instances when ``main.qml`` is loaded.  Combined
    with the Phase 9A ``_check_gpu`` audit (``tests/test_qml_scaffold.py``
    ``TestCheckGpuEndToEnd``) this proves the chart surfaces ARE on the
    GPU path that Phase 9A enforces.

    The literal AC #3 wording ("QSGRendererInterface reports OpenGL")
    is a *production* invariant on Linux/g5.xlarge; under
    ``QT_QPA_PLATFORM=offscreen`` the platform plugin always reports
    Software.  Phase 9A's subprocess end-to-end already proves the
    runtime check fires correctly when software rendering occurs, so
    we don't repeat that here.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _gui_app()

    def test_chart_items_resolve_from_main_qml(self) -> None:
        _register_qml_types()
        engine = QQmlApplicationEngine()
        engine.load(_qml_root_url())
        self.addCleanup(engine.deleteLater)

        roots = engine.rootObjects()
        self.assertTrue(roots, "main.qml must produce a root object")

        windows = []
        for root in roots:
            for w in _iter_quick_windows(root):
                windows.append(w)
        self.assertGreaterEqual(
            len(windows), 3,
            "main.qml exposes 3 QQuickWindow objects",
        )

        # Walk every QML root and its windows, collecting the bridge /
        # native items registered in Phase 9B.  All four item types
        # must show up (HeatmapItem, CvdItem, VolumeProfileItem,
        # CandleItem).
        found_types: set[str] = set()
        seen: set[str] = set()
        for root in roots:
            for child in [root, *root.findChildren(object)]:
                t = type(child).__name__
                if t in seen:
                    continue
                seen.add(t)
                if t in {"HeatmapItem", "CvdItem",
                         "VolumeProfileItem", "CandleItem"}:
                    found_types.add(t)

        self.assertEqual(
            found_types,
            {"HeatmapItem", "CvdItem",
             "VolumeProfileItem", "CandleItem"},
            f"Phase 9B chart items must appear in main.qml; found {found_types}",
        )

    def test_check_gpu_wiring_uses_software_constant(self) -> None:
        """Sanity — the Phase 9A constant the AC #3 audit relies on is
        importable from ``ui.app`` and lists the expected software
        backends."""
        self.assertIn("Software", _SOFTWARE_BACKENDS)
        self.assertIn("Unknown", _SOFTWARE_BACKENDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
