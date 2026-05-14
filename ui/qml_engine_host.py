"""Phase 9C.1 — Live-engine host for the QML scaffold.

Phase 9A/9B/9C produced a QML application that loads three windows
(OrderFlow / Chart / Strategy) and a :class:`MainWindowBridge` that knows
how to push snapshots, signal entries, and trade overlays into the QML
models / native ``CandleItem``.  What was missing was the actual *plumbing*
that drives those bridge methods from the live trading session — the
WebSocket trade feed, depth book, strategy engine, and execution manager
all live inside :class:`ui.main_window.MainWindow` today.

``QmlEngineHost`` closes that gap with the smallest amount of surgery the
production data path can stomach:

* Construct a :class:`MainWindow` **without ever showing it**.  All of its
  widgets (heatmap, CVD, volume profile, candle view), the
  :class:`OrderFlowViewModel`, the C++ orderflow engine, the
  :class:`LiveTradingSession`, and the strategy / execution managers
  stay exactly the same — the QML scene re-uses them.
* Re-attach the hidden MainWindow's widgets onto the QML scene's
  ``HeatmapItem`` / ``CvdItem`` / ``VolumeProfileItem`` bridge items via
  :meth:`WidgetBridgeItem.attach_external_widget`.  ``LiveTradingSession``
  keeps calling ``mw._heatmap.add_trade`` / ``mw._cvd.add_trade`` /
  ``mw._volume_profile.set_profile`` exactly as it did under the legacy
  QWidget shell; the bridge's ``widget.update()`` hook invalidates the
  QML scene-graph texture so the QML window actually repaints.
* Forward every :class:`SignalEntry` that ``MainWindow._broadcast_entry``
  fans out into :meth:`MainWindowBridge.on_signal_entry` so the QML
  ``TradeBlotter`` and chart ENTRY/EXIT markers stay in sync.
* Tee ``mw._candle_view.process_trade`` into the QML ``CandleItem`` so
  the native QML candle chart sees every trade alongside the hidden
  QWidget candle chart.
* Append a second slot to ``mw._update_timer`` so ``MainWindowBridge``
  receives the latest snapshot + execution state on every tick *after*
  MainWindow's own tick handler runs (which is the one that refreshes
  ``mw._last_strategy_snap``).

The host is intentionally **additive**: nothing about MainWindow's
existing wiring changes, which keeps the Phase 8 regression suites
unchanged and lets the legacy QWidget UI continue to work behind
``--legacy-widgets``.

# TODO Phase 9D: lift the WebSocket + strategy engine out of MainWindow
# into a headless ``LiveEngine`` class and have ``MainWindowBridge``
# drive it directly, so we can drop the hidden ``MainWindow`` instance.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from PySide6.QtCore import QObject

from execution.models import SignalEntry
from ui.items._widget_bridge import WidgetBridgeItem
from ui.items.candle_item import CandleItem
from ui.items.cvd_item import CvdItem
from ui.items.heatmap_item import HeatmapItem
from ui.items.volume_profile_item import VolumeProfileItem
from ui.main_window_bridge import MainWindowBridge

logger = logging.getLogger(__name__)


class QmlEngineHost(QObject):
    """Glue between a hidden :class:`MainWindow` and the QML scene.

    Parameters
    ----------
    engine:
        The :class:`QQmlApplicationEngine` that loaded ``main.qml``.
        Used to walk ``rootObjects()`` and find the bridge items.
    bridge:
        The :class:`MainWindowBridge` already installed on
        ``engine.rootContext()`` (see ``ui.app._install_bridge_context``).
        The host fans engine events into this bridge.
    parent:
        Optional QObject parent for lifetime management.
    """

    def __init__(
        self,
        engine: Any,
        bridge: MainWindowBridge,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._bridge = bridge
        self._main_window: Optional[Any] = None
        # Captured originals so we can restore in tests / teardown.
        self._original_broadcast_entry: Optional[Any] = None
        self._original_process_trade: Optional[Any] = None
        # The QML scene items resolved by ``_resolve_items``.
        self._heatmap_item: Optional[HeatmapItem] = None
        self._cvd_item: Optional[CvdItem] = None
        self._vp_item: Optional[VolumeProfileItem] = None
        self._candle_item: Optional[CandleItem] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        auto_connect: bool = True,
        symbol: Optional[str] = None,
    ) -> bool:
        """Build the hidden MainWindow and wire it into the QML scene.

        Parameters
        ----------
        auto_connect:
            When ``True`` (the default), invokes ``mw._on_connect()`` so
            the WebSocket feed starts immediately.  Tests pass ``False``
            to avoid touching the network.
        symbol:
            Optional override for the symbol field on the (hidden)
            toolbar before auto-connect fires.  When ``None``, MainWindow's
            default (``BTCUSDT``) is used.

        Returns ``True`` when the QML scene was successfully wired.  When
        the QML scene is missing the required items (i.e. ``main.qml``
        failed to load or was modified), the host logs an error and
        returns ``False`` without raising — the QML app then keeps
        running, just without live data.
        """
        from ui.main_window import MainWindow

        roots = self._engine.rootObjects()
        if not roots:
            logger.error(
                "QmlEngineHost.start: engine has no root objects; "
                "main.qml must be loaded before start() is called."
            )
            return False

        self._resolve_items(roots)
        if self._heatmap_item is None:
            logger.error(
                "QmlEngineHost.start: heatmapItem not found in QML scene; "
                "aborting live-engine wiring."
            )
            return False

        try:
            mw = MainWindow()
        except Exception:
            logger.exception("QmlEngineHost.start: MainWindow init failed")
            return False
        self._main_window = mw
        # Phase 9D — register the hidden MainWindow on the bridge so
        # toolbar slots (setCandleBucketMs / setOverlayEnabled) and the
        # SuppressionModel refresh inside on_timer_tick can resolve.
        try:
            self._bridge.set_main_window(mw)
        except Exception:
            logger.exception(
                "QmlEngineHost.start: bridge.set_main_window failed")

        self._reuse_widgets(mw)
        self._wire_candle_overlay(mw)
        self._tap_broadcast_entry(mw)
        self._tap_candle_process_trade(mw)
        self._tap_timer_tick(mw)

        if symbol is not None and hasattr(mw, "_symbol_input"):
            try:
                mw._symbol_input.setText(str(symbol))
            except Exception:
                logger.exception(
                    "QmlEngineHost.start: failed to set symbol input")

        if auto_connect:
            try:
                mw._on_connect()
                logger.info(
                    "QmlEngineHost.start: live session connected — "
                    "symbol=%s",
                    mw._symbol_input.text() if hasattr(mw, "_symbol_input")
                    else "?",
                )
            except Exception:
                logger.exception(
                    "QmlEngineHost.start: mw._on_connect() raised")
                return False

        logger.info("QmlEngineHost.start: QML scene wired to live MainWindow")
        return True

    @property
    def main_window(self) -> Optional[Any]:
        return self._main_window

    @property
    def bridge(self) -> MainWindowBridge:
        return self._bridge

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_items(self, roots: Sequence[Any]) -> None:
        """Find bridge items + the native ``CandleItem`` in the QML tree."""
        for obj in roots:
            if self._heatmap_item is None:
                for it in obj.findChildren(HeatmapItem):
                    self._heatmap_item = it
                    break
            if self._cvd_item is None:
                for it in obj.findChildren(CvdItem):
                    self._cvd_item = it
                    break
            if self._vp_item is None:
                for it in obj.findChildren(VolumeProfileItem):
                    self._vp_item = it
                    break
            if self._candle_item is None:
                for it in obj.findChildren(CandleItem):
                    self._candle_item = it
                    break

    def _reuse_widgets(self, mw: Any) -> None:
        """Have the QML bridges paint MainWindow's widgets directly."""
        if self._heatmap_item is not None and getattr(mw, "_heatmap", None):
            self._heatmap_item.attach_external_widget(mw._heatmap)
        if self._cvd_item is not None and getattr(mw, "_cvd", None):
            self._cvd_item.attach_external_widget(mw._cvd)
        if self._vp_item is not None and getattr(mw, "_volume_profile", None):
            self._vp_item.attach_external_widget(mw._volume_profile)

    def _wire_candle_overlay(self, mw: Any) -> None:
        """Hand the native ``CandleItem`` to the bridge so trade overlays
        fire from ``on_timer_tick`` onto the live scene-graph item."""
        if self._candle_item is None:
            return
        # Match the bucket / visible-candles configuration of the hidden
        # candle view so the two stay in lock-step.  Bucket changes
        # forced via the legacy toolbar will still propagate (Phase 9D
        # adds a QML control surface).
        try:
            cv = getattr(mw, "_candle_view", None)
            if cv is not None and hasattr(cv, "bucket_ms"):
                self._candle_item.set_bucket_ms(int(cv.bucket_ms))
        except Exception:
            logger.exception(
                "QmlEngineHost: failed to align CandleItem bucket_ms")
        self._bridge.set_candle_item(self._candle_item)

    def _tap_broadcast_entry(self, mw: Any) -> None:
        """Wrap ``MainWindow._broadcast_entry`` to also fan into the bridge.

        The legacy code path (``mw._strategy_dashboard.blotter.add_entry``
        + ``mw._strategy_store.write_signal``) is preserved verbatim so
        the QWidget regression suites and on-disk persistence keep
        working.  We just append the QML bridge call to the same
        funnel.
        """
        original = mw._broadcast_entry
        self._original_broadcast_entry = original
        bridge = self._bridge

        def wrapped(entry: SignalEntry) -> None:
            try:
                original(entry)
            finally:
                try:
                    bridge.on_signal_entry(entry)
                except Exception:
                    logger.exception(
                        "QmlEngineHost: bridge.on_signal_entry raised")

        mw._broadcast_entry = wrapped  # type: ignore[method-assign]

    def _tap_candle_process_trade(self, mw: Any) -> None:
        """Forward every trade into the QML ``CandleItem``.

        The wrap is installed on ``mw._candle_view.process_trade`` (the
        legacy QWidget candle view that ``LiveTradingSession`` already
        feeds), so we get one tap point for both Live and Replay sources.
        """
        if self._candle_item is None:
            return
        cv = getattr(mw, "_candle_view", None)
        if cv is None or not hasattr(cv, "process_trade"):
            return

        original = cv.process_trade
        self._original_process_trade = original
        qml_candle = self._candle_item

        def wrapped(ts: int, price: float, qty: float, is_buy: bool):
            result = original(ts, price, qty, is_buy)
            try:
                qml_candle.process_trade(
                    int(ts), float(price), float(qty), bool(is_buy))
            except Exception:
                logger.exception(
                    "QmlEngineHost: qml CandleItem.process_trade raised")
            return result

        cv.process_trade = wrapped  # type: ignore[method-assign]

    def _tap_timer_tick(self, mw: Any) -> None:
        """Connect a second slot to ``mw._update_timer`` that refreshes
        the bridge's QML models after MainWindow's own slot runs.

        Qt invokes ``timeout`` slots in connection order, and MainWindow's
        ``_on_timer_tick`` is connected in ``MainWindow.__init__`` — this
        connect therefore runs *after* it, which is exactly what we
        want so ``mw._last_strategy_snap`` is fresh before we forward
        it to ``MainWindowBridge.on_timer_tick``.
        """
        timer = getattr(mw, "_update_timer", None)
        if timer is None:
            return
        bridge = self._bridge

        def push_to_bridge() -> None:
            try:
                snap = getattr(mw, "_last_strategy_snap", None)
                exec_state = (
                    getattr(mw, "_exec_manager", None)
                    or getattr(mw, "_paper_engine", None)
                )
                bridge.on_timer_tick(snap, exec_state)
            except Exception:
                logger.exception(
                    "QmlEngineHost: bridge.on_timer_tick raised")

        timer.timeout.connect(push_to_bridge)
        # Hold a reference so the lambda isn't GC'd while the timer is alive.
        self._bridge_tick_callable = push_to_bridge

    # ------------------------------------------------------------------
    # Teardown / test helpers
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Disconnect from the live feed (test / shutdown helper)."""
        mw = self._main_window
        if mw is None:
            return
        try:
            session = getattr(mw, "_session", None)
            if session is not None:
                session.stop()
        except Exception:
            logger.exception("QmlEngineHost.stop: session.stop() raised")
        try:
            timer = getattr(mw, "_update_timer", None)
            if timer is not None:
                timer.stop()
        except Exception:
            logger.exception(
                "QmlEngineHost.stop: update_timer.stop() raised")

    def items(self) -> dict[str, Any]:
        """Test helper: expose the resolved QML items."""
        return {
            "heatmap": self._heatmap_item,
            "cvd": self._cvd_item,
            "volume_profile": self._vp_item,
            "candle": self._candle_item,
        }
