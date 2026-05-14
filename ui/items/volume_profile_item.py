"""Phase 9B bridge — ``VolumeProfileItem``.

Wraps :class:`ui.volume_profile_widget.VolumeProfileWidget` so the
side-by-side volume profile renders inside the QML scene graph.

# TODO Phase 9B-final: migrate to QSGGeometryNode histogram.
"""

from __future__ import annotations

from ui.volume_profile_widget import VolumeProfileWidget
from ui.items._widget_bridge import WidgetBridgeItem


class VolumeProfileItem(WidgetBridgeItem):
    """QML scene-graph bridge over :class:`VolumeProfileWidget`."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        widget = VolumeProfileWidget()
        self.set_bridge_widget(widget)

    def set_profile(self, profile, poc_price, price_min, price_max) -> None:
        widget = self.bridge_widget()
        if widget is not None:
            widget.set_profile(profile, poc_price, price_min, price_max)
            self.update()
