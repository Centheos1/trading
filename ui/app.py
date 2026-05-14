#!/usr/bin/env python3
"""Order Flow Trading UI — Qt Quick / QML application entry point.

Phase 9A migrated the application root from ``QApplication`` + the legacy
``MainWindow`` (QWidget stack) to ``QGuiApplication`` +
``QQmlApplicationEngine``.  The QML scene graph runs on a platform-appropriate
hardware backend (Metal on macOS, OpenGL on Linux/NICE DCV) selected by
``_configure_rhi_backend`` before ``QGuiApplication`` is constructed.

Chart, dashboard, and trade-blotter content is migrated in Phase 9B/9C; for
now ``ui/qml/components/*.qml`` are stub items that render a placeholder.

The legacy QWidget UI remains importable so ``tests/test_strategy_ui.py`` and
related regression suites continue to exercise the older code paths during
the migration.  Pass ``--legacy-widgets`` on the CLI to launch the old
QMainWindow shell explicitly (useful while 9B/9C land).
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
from pathlib import Path

# Make the project root importable when ``python -m ui.app`` is used from a
# checkout that has not been installed as a package.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RHI / scene-graph backend selection — MUST run before QGuiApplication().
# ---------------------------------------------------------------------------

# Hardware-accelerated graphics APIs accepted by ``_check_gpu``.  Software /
# Unknown trip the FATAL log; everything else is permitted.  Vulkan is
# allowed because a developer may force it for testing, and Direct3D is
# included for completeness even though the production targets are macOS and
# Linux.
_SOFTWARE_BACKENDS: tuple[str, ...] = ("Software", "Unknown")


def _configure_rhi_backend(system: str | None = None) -> dict[str, str]:
    """Set Qt RHI environment variables before ``QGuiApplication`` is created.

    macOS  → OpenGL (4.1 compatibility profile) + ``basic`` render loop.
             Metal is *not* used because PySide6's
             ``QSGGeometry.defaultAttributes_Point2D`` binding emits an
             attribute set incompatible with Metal's
             ``QSGFlatColorMaterial`` shader, which spams
             ``Failed to create render pipeline state: Vertex attribute
             vertexCoord(0) is missing`` and bus-errors after ~5 s.
             See the inline comment below + Phase 9C.1 evidence block.
    Linux  → OpenGL + ``xcb`` + ``threaded`` render loop (required for
             NICE DCV remote rendering and the 60 FPS budget).

    All variables use ``setdefault`` so developers can override from the
    shell without touching source (e.g. ``QSG_RHI_BACKEND=metal`` to
    experiment with Metal after a Phase 9B-final shader rewrite, or
    ``QSG_RHI_BACKEND=vulkan`` for Vulkan experiments).  Returns the
    resulting Qt env-var snapshot for test introspection.
    """
    sys_name = system or platform.system()

    if sys_name == "Darwin":
        # Phase 9C.1 — macOS uses OpenGL, NOT Metal.  PySide6's
        # ``QSGGeometry.defaultAttributes_Point2D()`` binding produces
        # an attribute set that Metal's ``QSGFlatColorMaterial``
        # shader (which declares ``[[attribute(0)]] float2
        # vertexCoord``) rejects with the error:
        #
        #   Failed to create render pipeline state: Vertex attribute
        #   vertexCoord(0) is missing from the vertex descriptor
        #
        # spammed on every frame the ``CandleItem`` has geometry to
        # render.  After a few seconds Metal's internal state
        # corrupts and the process bus-errors.  The same code path
        # works fine on OpenGL (macOS still supports OpenGL 4.1 via
        # Apple's compatibility profile) and on Linux's OpenGL
        # backend (the production NICE DCV target).
        #
        # Force OpenGL on Darwin until Phase 9B-final replaces the
        # ``QSGFlatColorMaterial`` flow with a custom shader that
        # explicitly names its vertex attribute (or PySide6 ships a
        # fix for the binding).  ``setdefault`` keeps the shell
        # override path open for developers experimenting with
        # Metal-specific code.
        os.environ.setdefault("QSG_RHI_BACKEND", "opengl")
        # Pair with ``basic`` render loop so ``QQuickPaintedItem.paint``
        # runs on the GUI thread.  Qt 6 defaults to ``threaded`` on
        # macOS, which races widget state mutation against the render
        # thread and produces ``QObject::setParent: Cannot set parent,
        # new parent is in a different thread`` warnings every tick.
        os.environ.setdefault("QSG_RENDER_LOOP", "basic")
    else:
        os.environ.setdefault("QSG_RHI_BACKEND", "opengl")
        # NICE DCV streams over X11; force xcb to avoid Wayland fallback.
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
        # NVIDIA glyph cache workaround (see AGENT_STRATEGY_RULES §22.2).
        os.environ.setdefault("QT_ENABLE_GLYPH_CACHE_WORKAROUND", "1")
        # Linux / NICE DCV needs the threaded render loop to clear
        # the 60 FPS budget on a g5.xlarge; the bridge widgets there
        # use a Qt build with proper paint-thread affinity.
        os.environ.setdefault("QSG_RENDER_LOOP", "threaded")

    os.environ.setdefault("QML_DISABLE_DISK_CACHE", "0")

    return {
        "QSG_RHI_BACKEND": os.environ["QSG_RHI_BACKEND"],
        "QSG_RENDER_LOOP": os.environ.get("QSG_RENDER_LOOP", ""),
        "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", ""),
    }


# ---------------------------------------------------------------------------
# GPU verification check — run for every top-level QQuickWindow.
# ---------------------------------------------------------------------------

def _api_name(api) -> str:
    """Return a stable string for a ``QSGRendererInterface.GraphicsApi``."""
    name = getattr(api, "name", None)
    if isinstance(name, str):
        return name
    return str(api).rsplit(".", 1)[-1]


def _check_gpu(window) -> bool:
    """Verify hardware acceleration is active for ``window``.

    Logs ERROR (per AGENT_STRATEGY_RULES §22.2) when the scene graph falls
    back to software.  Metal, OpenGL, Vulkan, and Direct3D are all accepted
    as valid hardware backends; only Software / Unknown trigger the FATAL
    warning.  When a software backend is detected the QML root object is
    marked with ``gpuWarning = true`` so the UI can surface a dismissible
    banner.

    Returns ``True`` when hardware acceleration is active.
    """
    iface = window.rendererInterface() if window is not None else None
    if iface is None:
        logger.error(
            "QQuickWindow has no renderer interface — scene graph not "
            "initialised before _check_gpu(); cannot verify GPU backend."
        )
        return False

    api = iface.graphicsApi()
    name = _api_name(api)

    if name in _SOFTWARE_BACKENDS:
        logger.error(
            "FATAL: Qt scene graph is using SOFTWARE rendering (%s). "
            "GPU acceleration unavailable — frame budget will be exceeded. "
            "Check QSG_RHI_BACKEND and graphics drivers.",
            name,
        )
        root = window.rootObject() if hasattr(window, "rootObject") else None
        if root is not None:
            try:
                root.setProperty("gpuWarning", True)
            except Exception:  # pragma: no cover — defensive
                logger.debug("Unable to set gpuWarning on QML root", exc_info=True)
        return False

    logger.info("Qt scene graph backend: %s (hardware) — window=%s",
                name, window.title() if hasattr(window, "title") else "?")
    return True


# ---------------------------------------------------------------------------
# Legacy QWidget entry point (kept available during 9A → 9B/9C migration).
# ---------------------------------------------------------------------------

def _run_legacy_widgets(argv: list[str]) -> int:
    """Launch the legacy QMainWindow shell.

    Retained as an explicit opt-in so the QWidget paths exercised by
    ``tests/test_strategy_ui.py`` (and the live trading session that still
    drives them) remain usable while Phase 9B/9C migrate the chart and
    dashboard surfaces into QML.
    """
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont

    from ui.main_window import MainWindow

    logger.warning(
        "Launching legacy QWidget UI (--legacy-widgets); QML scaffold "
        "remains the default entry point per Phase 9A."
    )

    app = QApplication(argv)
    app.setApplicationName("OrderFlow Trading (legacy widgets)")
    app.setFont(QFont("Menlo", 10))

    window = MainWindow()
    window.show()
    return app.exec()


# ---------------------------------------------------------------------------
# QML entry point.
# ---------------------------------------------------------------------------

def _qml_root_url() -> "QUrl":  # noqa: F821 — forward ref to QtCore.QUrl
    from PySide6.QtCore import QUrl

    qml_path = Path(_PROJECT_ROOT) / "ui" / "qml" / "main.qml"
    return QUrl.fromLocalFile(str(qml_path))


def _register_qml_types() -> None:
    """Expose Phase 9A/9B/9C Python types to the QML engine."""
    from PySide6.QtQml import qmlRegisterType

    from ui.models.snapshot_model import SnapshotModel

    qmlRegisterType(SnapshotModel, "Trading.Models", 1, 0, "SnapshotModel")

    # Phase 9C — Strategy dashboard models.
    from ui.models.position_model import PositionModel
    from ui.models.trade_blotter_model import TradeBlotterModel

    qmlRegisterType(PositionModel, "Trading.Models", 1, 0, "PositionModel")
    qmlRegisterType(
        TradeBlotterModel, "Trading.Models", 1, 0, "TradeBlotterModel")

    # Phase 9D — ripple-suppression metrics for the diagnostics tooltip.
    from ui.models.suppression_model import SuppressionModel

    qmlRegisterType(
        SuppressionModel, "Trading.Models", 1, 0, "SuppressionModel")

    # Phase 9B — chart items (QQuickPaintedItem bridges + native CandleItem).
    from ui.items.heatmap_item import HeatmapItem
    from ui.items.cvd_item import CvdItem
    from ui.items.volume_profile_item import VolumeProfileItem
    from ui.items.candle_item import CandleItem

    qmlRegisterType(HeatmapItem, "Trading.Items", 1, 0, "HeatmapItem")
    qmlRegisterType(CvdItem, "Trading.Items", 1, 0, "CvdItem")
    qmlRegisterType(VolumeProfileItem, "Trading.Items", 1, 0, "VolumeProfileItem")
    qmlRegisterType(CandleItem, "Trading.Items", 1, 0, "CandleItem")


def _run_qml(argv: list[str], no_live: bool = False) -> int:
    """Boot the Qt Quick / QML application.

    Phase 9B uses ``QApplication`` (a ``QGuiApplication`` subclass) so the
    bridge-tier ``QQuickPaintedItem`` wrappers in ``ui/items/`` can keep a
    hidden ``QWidget`` instance and delegate ``paint()`` to
    ``QWidget.render(painter, QPoint())``.  ``QApplication`` IS-A
    ``QGuiApplication``, so the Phase 9A scaffold semantics are preserved.

    Phase 9C registers a :class:`~ui.main_window_bridge.MainWindowBridge`
    + the three QML-facing models (``snapshotModel``, ``positionModel``,
    ``tradeBlotterModel``) as root-context properties so the
    ``PositionCard``, ``TradeBlotter``, ``StrategyDiagnostics`` QML
    components have something concrete to bind against even before the
    main strategy engine starts pumping data.  The bridge handle is
    returned via :func:`_qml_app_components` so callers wiring a live
    ``MainWindow`` can replay snapshots / signal entries through it.
    """
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setApplicationName("OrderFlow Trading")
    QCoreApplication.setOrganizationName("OrderFlow")
    QCoreApplication.setOrganizationDomain("orderflow.local")

    app = QApplication(argv)

    _register_qml_types()

    engine = QQmlApplicationEngine()
    engine.objectCreationFailed.connect(
        lambda url, _engine=engine: logger.error(
            "QML object creation failed for %s", url.toString()
        )
    )

    bridge = _install_bridge_context(engine)

    engine.load(_qml_root_url())

    roots = engine.rootObjects()
    if not roots:
        logger.error(
            "QML engine produced no root objects — main.qml failed to load. "
            "Check the QML log output above for parse / import errors."
        )
        return 2

    # Resolve the CandleItem and hand it to the bridge so the chart
    # overlay setters land on the right item instance.  The host (next
    # step) re-runs this if --no-live is off so the same item is shared
    # with QmlEngineHost.set_candle_item.
    _wire_candle_item(roots, bridge)

    # Run _check_gpu for every top-level QQuickWindow (main + detached
    # Chart + Strategy windows).  The signal may fire more than once
    # per window (Qt re-emits on backend / context invalidation) and
    # the same window can be reached via multiple roots; dedupe so
    # each window logs exactly once.
    _connect_gpu_checks(roots)

    # Phase 9C.1 — wire the QML scene to a hidden MainWindow that owns
    # the live WebSocket feed, OrderFlowEngine, strategy and execution
    # managers.  Without this the QML windows render the empty-state
    # placeholders for every component because nothing pumps trades /
    # depth / snapshots into the bridge models or chart items.
    host = None
    if not no_live:
        from ui.qml_engine_host import QmlEngineHost

        host = QmlEngineHost(engine, bridge)
        started = host.start(auto_connect=True)
        if not started:
            logger.warning(
                "QML scene is up but live-engine host failed to start; "
                "windows will keep showing 'Waiting for data' placeholders."
            )

    app._qml_engine = engine  # type: ignore[attr-defined]
    app._qml_bridge = bridge  # type: ignore[attr-defined]
    if host is not None:
        app._qml_host = host  # type: ignore[attr-defined]

    return app.exec()


def _install_bridge_context(engine) -> "MainWindowBridge":
    """Create idle QML models + a :class:`MainWindowBridge` and expose
    them on ``engine.rootContext()`` so QML bindings resolve.

    The bridge is wired up but receives no live data until a
    ``MainWindow`` (or test harness) calls
    ``bridge.on_timer_tick`` / ``bridge.on_signal_entry``.
    """
    from ui.main_window_bridge import MainWindowBridge
    from ui.models.position_model import PositionModel
    from ui.models.snapshot_model import SnapshotModel
    from ui.models.suppression_model import SuppressionModel
    from ui.models.trade_blotter_model import TradeBlotterModel

    snapshot_model = SnapshotModel()
    position_model = PositionModel()
    blotter_model = TradeBlotterModel()
    suppression_model = SuppressionModel()

    bridge = MainWindowBridge(
        snapshot_model, position_model, blotter_model,
        suppression_model=suppression_model,
    )

    ctx = engine.rootContext()
    ctx.setContextProperty("snapshotModel", snapshot_model)
    ctx.setContextProperty("positionModel", position_model)
    ctx.setContextProperty("tradeBlotterModel", blotter_model)
    ctx.setContextProperty("suppressionModel", suppression_model)
    ctx.setContextProperty("mainWindowBridge", bridge)

    return bridge


def _wire_candle_item(roots, bridge) -> None:
    """Find the ``CandleItem`` in the loaded QML and attach it to the
    bridge so chart overlay calls reach the live scene graph."""
    from ui.items.candle_item import CandleItem

    for obj in roots:
        for item in obj.findChildren(CandleItem):
            bridge.set_candle_item(item)
            return


def _connect_gpu_checks(roots) -> None:
    """Connect a one-shot GPU-check slot to every reachable
    ``QQuickWindow``.

    ``sceneGraphInitialized`` may fire multiple times per window (on
    backend swap, context invalidation, or window-resize on some
    platforms); we want exactly one log line per window, so guard
    the slot with a ``set`` of already-checked window ids.
    """
    checked: set[int] = set()

    def _run_check(window) -> None:
        if id(window) in checked:
            return
        checked.add(id(window))
        _check_gpu(window)

    seen: set[int] = set()
    for obj in roots:
        for window in _iter_quick_windows(obj):
            if id(window) in seen:
                continue
            seen.add(id(window))
            window.sceneGraphInitialized.connect(
                lambda w=window: _run_check(w)
            )


def _iter_quick_windows(obj):
    """Yield every ``QQuickWindow`` reachable from a QML root object.

    Top-level ``Window`` items declared inside an ``ApplicationWindow``
    surface both as the root's QML children and (depending on Qt
    version) as separate entries in ``engine.rootObjects()``.  The
    iterator de-duplicates by ``id`` to guarantee one yield per
    underlying ``QQuickWindow``.
    """
    from PySide6.QtQuick import QQuickWindow

    seen: set[int] = set()

    def _emit(window):
        if id(window) in seen:
            return False
        seen.add(id(window))
        return True

    if isinstance(obj, QQuickWindow) and _emit(obj):
        yield obj
    for child in obj.findChildren(QQuickWindow):
        if _emit(child):
            yield child


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="ui.app",
        description="OrderFlow Trading UI (Qt Quick / QML).",
    )
    parser.add_argument(
        "--legacy-widgets",
        action="store_true",
        help="Launch the deprecated QMainWindow shell instead of the QML "
             "scaffold (Phase 9A transition aid; removed in Phase 9C).",
    )
    parser.add_argument(
        "--no-live",
        action="store_true",
        help="Skip launching the live MainWindow data feed.  QML windows "
             "show the empty-state placeholders.  Useful for QML "
             "previewing / development without touching the network "
             "(Phase 9C.1 — live wiring is opt-out via this flag).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Root logger level (default: INFO).",
    )
    return parser.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args, remaining = _parse_args(argv[1:])

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    )

    if args.legacy_widgets:
        return _run_legacy_widgets([argv[0]] + remaining)

    _configure_rhi_backend()
    return _run_qml([argv[0]] + remaining, no_live=args.no_live)


if __name__ == "__main__":
    sys.exit(main())
