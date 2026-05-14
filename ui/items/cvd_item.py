"""Phase 9B bridge — ``CvdItem``.

Wraps :class:`ui.cvd_widget.CVDWidget` so the cumulative-volume-delta
chart (delta histogram + cumulative line + rolling mean) renders inside
the QML scene graph.  Inputs (``add_raw``, ``set_time_reference``, etc.)
are forwarded to the wrapped widget unchanged.

# TODO Phase 9B-final: migrate to QSGGeometryNode bars + line strip; the
# rolling-mean and CVD line are natural ``QSGGeometry.DrawLineStrip`` nodes.
"""

from __future__ import annotations

from ui.cvd_widget import CVDWidget
from ui.items._widget_bridge import WidgetBridgeItem


class CvdItem(WidgetBridgeItem):
    """QML scene-graph bridge over :class:`CVDWidget`."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        widget = CVDWidget()
        self.set_bridge_widget(widget)

    # ------------------------------------------------------------------
    # Forwarding API — only the methods MainWindow already calls are
    # surfaced.  Anything beyond this should reach through
    # ``bridge_widget()`` to keep the bridge surface tight.
    # ------------------------------------------------------------------

    def add_trade(self, ts, price, qty, is_buy) -> None:
        widget = self.bridge_widget()
        if widget is not None:
            widget.add_trade(ts, price, qty, is_buy)

    def set_time_ref(self, now_ts, visible_window_ms, right_inset_px=0) -> None:
        widget = self.bridge_widget()
        if widget is not None:
            widget.set_time_ref(now_ts, visible_window_ms, right_inset_px)

    def refresh(self) -> None:
        """Drain the per-frame batch and repaint.  Matches
        :meth:`CVDWidget.refresh` so the existing render-timer wiring
        translates verbatim."""
        widget = self.bridge_widget()
        if widget is not None:
            widget.refresh()
            self.update()
