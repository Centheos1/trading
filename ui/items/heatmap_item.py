"""Phase 9B bridge — ``HeatmapItem``.

Wraps :class:`ui.heatmap_widget.HeatmapWidget` so the existing order-flow
heatmap render (depth grid + buy/sell bubbles + axes + strategy overlay)
runs inside the QML scene graph.  Trade and depth ingestion still happens
on ``OrderFlowViewModel`` — the bridge simply forwards ``set_frame()`` and
exposes ``set_viewmodel()`` so QML / Python wiring can stay declarative.

The expensive depth ``QImage`` is built once per computed frame in
``OrderFlowViewModel.compute_frame()``; Phase 9B-final will upload it as a
``QSGTexture`` directly (see ``OrderFlowViewModel.depth_texture_dirty`` —
the dirty flag is plumbed today so the upgrade is a drop-in replacement).

# TODO Phase 9B-final: migrate to QSGNode (drop the wrapped HeatmapWidget
# and upload ``frame.depth_image`` as a ``QSGTexture`` per the spec).
"""

from __future__ import annotations

from typing import Optional

from ui.heatmap_widget import HeatmapWidget
from ui.items._widget_bridge import WidgetBridgeItem


class HeatmapItem(WidgetBridgeItem):
    """QML scene-graph bridge over :class:`HeatmapWidget`."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        widget = HeatmapWidget()
        self.set_bridge_widget(widget)

    # ------------------------------------------------------------------
    # Forwarding API — keeps the bridge a thin shim over the widget so
    # callers don't need to reach through ``bridge_widget()`` in normal
    # usage.
    # ------------------------------------------------------------------

    def set_viewmodel(self, viewmodel) -> None:
        widget = self.bridge_widget()
        if widget is not None:
            widget.set_viewmodel(viewmodel)

    def set_frame(self, frame) -> None:
        """Store the pre-computed ``FrameData`` for the next paint.

        Calls ``self.update()`` so the scene graph schedules a repaint;
        without this the QML compositor keeps the previous texture.
        """
        widget = self.bridge_widget()
        if widget is None:
            return
        widget.set_frame(frame)
        # The bridge has its own dirty flag (the Quick item) — we must
        # call update() to invalidate the cached texture.  Without this
        # ``QQuickPaintedItem`` will keep compositing the previous frame
        # on the GPU and the heatmap will look frozen.
        self.update()

    def viewmodel(self):
        widget = self.bridge_widget()
        if widget is None:
            return None
        return widget._vm  # noqa: SLF001 — widget exposes _vm as its API
