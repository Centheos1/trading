"""Phase 9B bridge-tier scaffold: ``WidgetBridgeItem``.

Wraps a hidden ``QWidget`` inside a ``QQuickPaintedItem`` so the existing
QPainter-based render logic for ``HeatmapWidget``, ``CVDWidget``, and
``VolumeProfileWidget`` can be re-used inside the QML scene graph without a
ground-up rewrite.  The scene graph composites the resulting texture on the
GPU; only the actual painter calls remain CPU-bound, which is the
established cost of the bridge tier (``UI_STRATEGY_INTEGRATION_PLAN.md
§22.3``).

# TODO Phase 9B-final: migrate to QSGNode — drop the wrapped QWidget and
# emit native scene-graph nodes from ``updatePaintNode``.

Threading: ``paint()`` runs on the GUI thread (Qt Quick guarantees this for
``QQuickPaintedItem``), which matches the original ``QWidget.paintEvent``
contract.  Worker threads must continue to marshal state mutations via
``QMetaObject.invokeMethod(..., Qt.QueuedConnection)`` exactly as before.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPainter
from PySide6.QtQuick import QQuickPaintedItem
from PySide6.QtWidgets import QWidget


class WidgetBridgeItem(QQuickPaintedItem):
    """Generic bridge that paints a hidden ``QWidget`` into a Quick item.

    Subclasses construct the concrete widget in ``__init__`` and pass it
    via ``set_bridge_widget()``.  Callers should never show the wrapped
    widget — it lives off-screen and is only rendered into the scene
    graph painter inside ``paint()``.
    """

    # All bridges paint into an internal QImage-backed framebuffer and the
    # scene graph composites that onto the GPU.  ``FastFBO`` would re-render
    # every frame regardless of dirty state; the default ``Image`` mode
    # already invalidates only when ``self.update()`` is called.  Setting
    # this constant here documents the choice; subclasses can override if
    # they have a reason (none do today).
    _RENDER_TARGET = QQuickPaintedItem.RenderTarget.Image

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._widget: Optional[QWidget] = None
        # Painters consumed by Qt's scene graph live entirely inside paint();
        # antialiasing must be enabled on the *item* (not the painter) so the
        # backing framebuffer is supersampled.
        self.setAntialiasing(True)
        self.setRenderTarget(self._RENDER_TARGET)
        # Resize the wrapped widget whenever the QML layout reassigns our
        # bounds.  Both signals fire from the GUI thread so direct
        # connection is safe.
        self.widthChanged.connect(self._sync_widget_size)
        self.heightChanged.connect(self._sync_widget_size)

    # ------------------------------------------------------------------
    # Subclass API
    # ------------------------------------------------------------------

    def set_bridge_widget(self, widget: QWidget) -> None:
        """Attach the QWidget whose ``render()`` produces this item's pixels.

        Called exactly once from each subclass constructor.  The widget is
        kept off-screen (no ``show()``) for the lifetime of the item.
        """
        self._widget = widget
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        widget.resize(max(1, int(self.width()) or 200),
                      max(1, int(self.height()) or 200))
        self.update()

    def attach_external_widget(self, widget: QWidget) -> None:
        """Replace the auto-created widget with an externally-owned one.

        Used by :class:`~ui.qml_engine_host.QmlEngineHost` (Phase 9C.1) to
        feed the QML scene from a hidden ``MainWindow``'s widgets — the
        same widget instances ``LiveTradingSession`` already mutates
        directly via ``mw._heatmap`` / ``mw._cvd`` / ``mw._volume_profile``.

        Side effects:

        * The previous (auto-created) widget is detached and queued for
          deletion via ``deleteLater`` if it was owned by this item.
        * ``widget.update`` is wrapped on the instance so any
          paint-invalidation call from production code (e.g.
          ``LiveTradingSession`` doing ``mw._heatmap.update()`` after a
          frame batch) also invalidates the QML bridge texture.  Without
          this hook the scene graph would composite the previous frame
          indefinitely while the underlying widget held the latest state.

        The wrap is idempotent: re-wrapping an already-wrapped
        ``widget.update`` skips installing a second layer.
        """
        if widget is None:
            return
        old = self._widget
        if old is not None and old is not widget:
            try:
                old.setParent(None)
            except RuntimeError:
                pass
            try:
                old.deleteLater()
            except RuntimeError:
                pass
        self._widget = widget
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        widget.resize(max(1, int(self.width()) or 200),
                      max(1, int(self.height()) or 200))
        self._install_update_hook(widget)
        self.update()

    def _install_update_hook(self, widget: QWidget) -> None:
        """Wrap ``widget.update`` so the bridge is invalidated alongside it.

        Bound on the *instance* (not the class) so swapping widgets via
        ``attach_external_widget`` is safe — each widget gets its own
        wrapper and the previous widget's wrapper is GC'd with it.
        ``_bridge_update_hook_installed`` guards against double-wrapping
        when the same widget is attached twice.
        """
        if getattr(widget, "_bridge_update_hook_installed", False):
            return
        original_update = widget.update
        bridge_self = self

        def _wrapped_update(*args, **kwargs):
            original_update(*args, **kwargs)
            try:
                bridge_self.update()
            except RuntimeError:
                pass

        widget.update = _wrapped_update  # type: ignore[method-assign]
        widget._bridge_update_hook_installed = True  # type: ignore[attr-defined]

    def bridge_widget(self) -> Optional[QWidget]:
        """Return the wrapped widget (None until ``set_bridge_widget``)."""
        return self._widget

    # ------------------------------------------------------------------
    # QQuickPaintedItem
    # ------------------------------------------------------------------

    def paint(self, painter: QPainter) -> None:
        widget = self._widget
        if widget is None:
            return
        # Belt-and-braces: ensure size matches the current item bounds.
        # ``_sync_widget_size`` already runs on geometry changes, but a
        # subclass that mutates the widget directly might desync.
        self._sync_widget_size()
        widget.render(painter, QPoint())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sync_widget_size(self) -> None:
        widget = self._widget
        if widget is None:
            return
        w = int(self.width())
        h = int(self.height())
        if w <= 0 or h <= 0:
            return
        if widget.size().width() != w or widget.size().height() != h:
            widget.resize(w, h)
