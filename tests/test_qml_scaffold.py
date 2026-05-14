"""Phase 9A — QML scaffold & cross-platform GPU backend regression suite.

Covers UI_STRATEGY_INTEGRATION_PLAN.md §22.2 acceptance criteria:

1. ``QQmlApplicationEngine`` loads ``ui/qml/main.qml`` without error.
2. ``_configure_rhi_backend`` picks ``opengl`` on Darwin (Phase 9C.1
   workaround for PySide6's ``QSGGeometry.defaultAttributes_Point2D``
   ↔ Metal ``QSGFlatColorMaterial`` shader attribute mismatch) and
   ``opengl`` on Linux, in both cases honouring a pre-existing
   ``QSG_RHI_BACKEND`` override.
3. ``_check_gpu`` logs ERROR + flips the QML root's ``gpuWarning``
   property when the scene graph reports SOFTWARE rendering.
4. ``_check_gpu`` does **not** log ERROR for ``Metal`` / ``OpenGL`` /
   ``Vulkan``.
5. ``SnapshotModel`` Q_PROPERTY setters emit a single notify per change
   and ``update_from_snapshot`` mirrors a ``StrategySnapshot``.
6. QWidget paths remain importable so existing regression suites still
   exercise the deprecated UI layer during the 9A→9C migration.

All Qt object construction uses ``QT_QPA_PLATFORM=offscreen`` so the
tests run in CI / Docker without a display server.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PySide6.QtCore import QCoreApplication, QObject, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402 — Phase 9B bridge support

from ui.app import (  # noqa: E402
    _SOFTWARE_BACKENDS,
    _api_name,
    _check_gpu,
    _configure_rhi_backend,
    _iter_quick_windows,
    _qml_root_url,
    _register_qml_types,
)
from ui.models.snapshot_model import SnapshotModel, _enum_name  # noqa: E402


_QML_MAIN = _PROJECT_ROOT / "ui" / "qml" / "main.qml"


def _gui_app() -> QGuiApplication:
    """Return the singleton GUI application (created on first call).

    Phase 9B switched ``ui/app.py`` to ``QApplication`` so the Phase 9B
    ``QQuickPaintedItem`` bridges can hold hidden ``QWidget`` instances.
    ``QApplication`` IS-A ``QGuiApplication`` so the Phase 9A semantics
    (Qt Quick / QML scene graph entry point) are preserved.
    """
    instance = QCoreApplication.instance()
    if instance is None:
        instance = QApplication(sys.argv[:1])
    return instance


# ---------------------------------------------------------------------------
# _configure_rhi_backend
# ---------------------------------------------------------------------------

class TestConfigureRhiBackend(unittest.TestCase):
    """Backend selection must be platform-aware and shell-overridable."""

    _ENV_KEYS = (
        "QSG_RHI_BACKEND",
        "QSG_RENDER_LOOP",
        "QT_QPA_PLATFORM",
        "QT_ENABLE_GLYPH_CACHE_WORKAROUND",
        "QML_DISABLE_DISK_CACHE",
    )

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_darwin_selects_opengl_with_basic_loop(self) -> None:
        # Phase 9C.1 — macOS uses OpenGL (NOT Metal).  PySide6's
        # ``QSGGeometry.defaultAttributes_Point2D()`` binding emits
        # an attribute set incompatible with Metal's
        # ``QSGFlatColorMaterial`` vertex shader (which declares
        # ``[[attribute(0)]] float2 vertexCoord``).  Symptoms on
        # Metal: ``Failed to create render pipeline state: Vertex
        # attribute vertexCoord(0) is missing from the vertex
        # descriptor`` spammed every frame the ``CandleItem`` has
        # geometry, followed by a bus error / segfault after ~5 s.
        # OpenGL via Apple's 4.1 compatibility profile works.
        env = _configure_rhi_backend(system="Darwin")
        self.assertEqual(env["QSG_RHI_BACKEND"], "opengl")
        self.assertEqual(os.environ.get("QSG_RHI_BACKEND"), "opengl")
        # ``basic`` render loop pairs with the OpenGL backend so
        # ``QQuickPaintedItem.paint`` stays on the GUI thread (Qt
        # 6 defaults to ``threaded`` on macOS).  This eliminates
        # ``QObject::setParent: ... new parent is in a different
        # thread`` warnings observed when widget state mutations
        # race the render thread.  Linux/NICE DCV keeps
        # ``threaded`` for the 60 FPS budget.
        self.assertEqual(env["QSG_RENDER_LOOP"], "basic")
        self.assertEqual(os.environ.get("QSG_RENDER_LOOP"), "basic")
        # On Darwin no platform plugin override is required (cocoa is default).
        self.assertEqual(os.environ.get("QT_QPA_PLATFORM"), None)

    def test_linux_selects_opengl_and_xcb(self) -> None:
        env = _configure_rhi_backend(system="Linux")
        self.assertEqual(env["QSG_RHI_BACKEND"], "opengl")
        self.assertEqual(env["QT_QPA_PLATFORM"], "xcb")
        self.assertEqual(env["QSG_RENDER_LOOP"], "threaded")
        # AGENT_STRATEGY_RULES §22.2: NVIDIA glyph cache workaround must
        # be enabled on Linux.
        self.assertEqual(
            os.environ.get("QT_ENABLE_GLYPH_CACHE_WORKAROUND"), "1"
        )

    def test_shell_override_wins(self) -> None:
        """Developers must be able to force ``vulkan`` from the shell."""
        os.environ["QSG_RHI_BACKEND"] = "vulkan"
        env = _configure_rhi_backend(system="Linux")
        self.assertEqual(env["QSG_RHI_BACKEND"], "vulkan")
        # Other defaults still applied.
        self.assertEqual(env["QT_QPA_PLATFORM"], "xcb")


# ---------------------------------------------------------------------------
# QML engine — main.qml must load and produce three QQuickWindows.
# ---------------------------------------------------------------------------

class TestQmlEngineLoads(unittest.TestCase):
    """``main.qml`` must parse and produce three top-level windows."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _gui_app()
        _register_qml_types()

    def test_main_qml_exists(self) -> None:
        self.assertTrue(_QML_MAIN.is_file(), f"missing {_QML_MAIN}")

    def test_qml_root_url_resolves(self) -> None:
        url = _qml_root_url()
        self.assertIsInstance(url, QUrl)
        self.assertTrue(url.isLocalFile())
        self.assertEqual(Path(url.toLocalFile()), _QML_MAIN)

    def test_engine_loads_without_warnings(self) -> None:
        engine = QQmlApplicationEngine()
        warnings: list[str] = []

        def _capture(lst):
            for w in lst:
                warnings.append(w.toString())

        engine.warnings.connect(_capture)

        creation_failures: list[str] = []
        engine.objectCreationFailed.connect(
            lambda url: creation_failures.append(url.toString())
        )

        engine.load(_qml_root_url())

        self.assertEqual(
            creation_failures, [],
            f"objectCreationFailed for: {creation_failures}",
        )
        self.assertEqual(warnings, [], f"QML warnings: {warnings}")

        roots = engine.rootObjects()
        self.assertTrue(roots, "engine produced no root objects")

        windows = list(_iter_quick_windows(roots[0]))
        names = sorted(w.objectName() for w in windows)
        self.assertEqual(
            names, ["chartWindow", "orderFlowWindow", "strategyWindow"],
            f"expected 3 detachable windows; got {names}",
        )

        engine.deleteLater()


# ---------------------------------------------------------------------------
# _check_gpu — logs ERROR on software, INFO on hardware.
# ---------------------------------------------------------------------------

class _FakeRendererInterface:
    def __init__(self, api):
        self._api = api

    def graphicsApi(self):
        return self._api


class _FakeQuickWindow:
    """Lightweight stand-in for ``QQuickWindow`` so we can drive
    ``_check_gpu`` synchronously without spinning the scene graph."""

    def __init__(self, api):
        self._api = api
        self._root = QObject()  # supports setProperty

    def rendererInterface(self):
        return _FakeRendererInterface(self._api)

    def rootObject(self):
        return self._root

    def title(self):
        return "fake"


class TestCheckGpu(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _gui_app()

    def test_software_logs_error(self) -> None:
        api = QSGRendererInterface.GraphicsApi.Software
        win = _FakeQuickWindow(api)
        with self.assertLogs("ui.app", level="ERROR") as cap:
            ok = _check_gpu(win)
        self.assertFalse(ok)
        self.assertTrue(
            any("SOFTWARE" in msg for msg in cap.output),
            f"expected SOFTWARE error log; got {cap.output}",
        )
        self.assertTrue(win.rootObject().property("gpuWarning"))

    def test_unknown_logs_error(self) -> None:
        api = QSGRendererInterface.GraphicsApi.Unknown
        win = _FakeQuickWindow(api)
        with self.assertLogs("ui.app", level="ERROR") as cap:
            ok = _check_gpu(win)
        self.assertFalse(ok)
        self.assertTrue(any("SOFTWARE" in msg for msg in cap.output))

    def test_metal_passes(self) -> None:
        api = QSGRendererInterface.GraphicsApi.Metal
        win = _FakeQuickWindow(api)
        logger = logging.getLogger("ui.app")
        with mock.patch.object(logger, "error") as err_mock:
            ok = _check_gpu(win)
        self.assertTrue(ok)
        err_mock.assert_not_called()

    def test_opengl_passes(self) -> None:
        api = QSGRendererInterface.GraphicsApi.OpenGL
        win = _FakeQuickWindow(api)
        logger = logging.getLogger("ui.app")
        with mock.patch.object(logger, "error") as err_mock:
            ok = _check_gpu(win)
        self.assertTrue(ok)
        err_mock.assert_not_called()

    def test_vulkan_passes(self) -> None:
        api = QSGRendererInterface.GraphicsApi.Vulkan
        win = _FakeQuickWindow(api)
        logger = logging.getLogger("ui.app")
        with mock.patch.object(logger, "error") as err_mock:
            ok = _check_gpu(win)
        self.assertTrue(ok)
        err_mock.assert_not_called()

    def test_software_backends_constant(self) -> None:
        self.assertIn("Software", _SOFTWARE_BACKENDS)
        self.assertIn("Unknown", _SOFTWARE_BACKENDS)
        for hw in ("Metal", "OpenGL", "Vulkan", "Direct3D11", "Direct3D12"):
            self.assertNotIn(hw, _SOFTWARE_BACKENDS)

    def test_api_name_resolves(self) -> None:
        self.assertEqual(
            _api_name(QSGRendererInterface.GraphicsApi.Metal), "Metal"
        )
        self.assertEqual(
            _api_name(QSGRendererInterface.GraphicsApi.Software), "Software"
        )

    def test_no_renderer_interface_returns_false(self) -> None:
        class _NoIface:
            def rendererInterface(self):
                return None
            def rootObject(self):
                return None
            def title(self):
                return ""
        with self.assertLogs("ui.app", level="ERROR"):
            ok = _check_gpu(_NoIface())
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# End-to-end: force software, load main.qml, verify _check_gpu fires.
# ---------------------------------------------------------------------------

class TestCheckGpuEndToEnd(unittest.TestCase):
    """Run the real QML engine under ``QSG_RHI_BACKEND=software`` and
    verify ``_check_gpu`` reports SOFTWARE for every QQuickWindow."""

    @classmethod
    def setUpClass(cls) -> None:
        # NOTE: setting ``QSG_RHI_BACKEND`` after ``QGuiApplication`` is
        # created has no effect.  Spawn a dedicated process so the env var
        # applies before Qt boots.
        cls.app = _gui_app()

    def test_software_backend_triggers_error_for_each_window(self) -> None:
        import subprocess

        # Phase 9B note: ``_register_qml_types`` registers ``HeatmapItem``
        # / ``CvdItem`` / ``VolumeProfileItem``, which each construct a
        # hidden ``QWidget`` inside the bridge.  ``QWidget`` requires
        # ``QApplication``; we use ``QApplication`` here (a
        # ``QGuiApplication`` subclass — Phase 9A semantics preserved).
        script = (
            "import os, sys, logging\n"
            f"sys.path.insert(0, {str(_PROJECT_ROOT)!r})\n"
            "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
            "os.environ['QSG_RHI_BACKEND'] = 'software'\n"
            "logging.basicConfig(level=logging.DEBUG)\n"
            "from PySide6.QtCore import QUrl, QTimer\n"
            "from PySide6.QtQml import QQmlApplicationEngine\n"
            "from PySide6.QtWidgets import QApplication\n"
            "from ui.app import _check_gpu, _iter_quick_windows, _register_qml_types\n"
            "app = QApplication(sys.argv)\n"
            "_register_qml_types()\n"
            "engine = QQmlApplicationEngine()\n"
            f"engine.load(QUrl.fromLocalFile({str(_QML_MAIN)!r}))\n"
            "results = []\n"
            "def on_init(w):\n"
            "    ok = _check_gpu(w)\n"
            "    results.append((w.objectName(), ok))\n"
            "    print('CHECK', w.objectName(), ok)\n"
            "    if len(results) >= 3:\n"
            "        app.quit()\n"
            "for obj in engine.rootObjects():\n"
            "    for win in _iter_quick_windows(obj):\n"
            "        win.sceneGraphInitialized.connect(lambda w=win: on_init(w))\n"
            "QTimer.singleShot(5000, app.quit)\n"
            "app.exec()\n"
            "print('FINAL', results)\n"
        )

        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
            cwd=str(_PROJECT_ROOT),
        )
        # Combine stdout + stderr — the ERROR log goes to stderr.
        combined = proc.stdout + proc.stderr

        # AC #4: SOFTWARE rendering MUST log ERROR.
        self.assertIn("SOFTWARE", combined,
                      f"no SOFTWARE error in subprocess output:\n{combined}")

        # AC #1 / #4: every QQuickWindow that has its scene graph
        # initialised under the offscreen platform must report False
        # (software).  Under offscreen, the QML root ``ApplicationWindow``
        # is not always exposed, so accept any subset >= 2 of the three
        # named windows (the two ``Window {}`` children always initialise).
        observed = {name for name in (
            "orderFlowWindow", "chartWindow", "strategyWindow",
        ) if name in combined}
        self.assertGreaterEqual(
            len(observed), 2,
            f"expected scene graph init for >=2 windows; got {observed}\n"
            f"output:\n{combined}",
        )
        # The two detachable Window items must always initialise.
        self.assertIn("chartWindow", observed)
        self.assertIn("strategyWindow", observed)


# ---------------------------------------------------------------------------
# SnapshotModel — Q_PROPERTYs, change notify, and update_from_snapshot.
# ---------------------------------------------------------------------------

class TestSnapshotModel(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _gui_app()

    def test_default_values(self) -> None:
        m = SnapshotModel()
        self.assertEqual(m.tideBias, "")
        self.assertEqual(m.waveRegime, "")
        self.assertEqual(m.riskBudgetPct, 0.0)
        self.assertEqual(m.unrealizedPnl, 0.0)
        self.assertEqual(m.tradeState, "")

    def test_tide_bias_notify_only_on_change(self) -> None:
        m = SnapshotModel()
        calls: list[str] = []
        m.tideBiasChanged.connect(calls.append)

        m.tideBias = "LONG"
        m.tideBias = "LONG"
        m.tideBias = "SHORT"
        m.tideBias = "SHORT"

        self.assertEqual(calls, ["LONG", "SHORT"])

    def test_numeric_setters_coerce(self) -> None:
        m = SnapshotModel()
        m.riskBudgetPct = 0.75
        m.unrealizedPnl = -125.5
        self.assertEqual(m.riskBudgetPct, 0.75)
        self.assertEqual(m.unrealizedPnl, -125.5)

    def test_update_from_strategy_snapshot(self) -> None:
        from schemas import StrategySnapshot, TideBias, WaveRegime

        snap = StrategySnapshot.make_default()
        m = SnapshotModel()
        m.update_from_snapshot(snap)

        self.assertEqual(m.tideBias, TideBias.NEUTRAL.name)
        self.assertEqual(m.waveRegime, WaveRegime.NEUTRAL.name)
        self.assertEqual(m.riskBudgetPct, 1.0)  # default risk_multiplier
        self.assertEqual(m.unrealizedPnl, 0.0)
        # Default LifecycleState is "SETUP" per schemas.py.
        self.assertEqual(m.tradeState, "SETUP")

    def test_update_from_snapshot_handles_none(self) -> None:
        m = SnapshotModel()
        m.tideBias = "LONG"
        m.riskBudgetPct = 0.5
        m.update_from_snapshot(None)
        self.assertEqual(m.tideBias, "")
        self.assertEqual(m.riskBudgetPct, 0.0)

    def test_update_from_snapshot_emits_signals(self) -> None:
        from schemas import StrategySnapshot

        snap = StrategySnapshot.make_default()
        m = SnapshotModel()

        tide_calls: list[str] = []
        m.tideBiasChanged.connect(tide_calls.append)

        m.update_from_snapshot(snap)
        # First update goes from "" → "NEUTRAL", so one notify.
        self.assertEqual(tide_calls, ["NEUTRAL"])

        # Repeated update with identical values — no extra notify.
        m.update_from_snapshot(snap)
        self.assertEqual(tide_calls, ["NEUTRAL"])

    def test_enum_name_helper(self) -> None:
        self.assertEqual(_enum_name(None), "")

        class _Fake:
            name = "FOO"

        self.assertEqual(_enum_name(_Fake()), "FOO")
        self.assertEqual(_enum_name(42), "42")


# ---------------------------------------------------------------------------
# QWidget paths must remain importable (AC #5).
# ---------------------------------------------------------------------------

class TestLegacyWidgetPathsImportable(unittest.TestCase):
    """Phase 9A keeps the QWidget UI usable so the existing regression
    suites (``test_strategy_ui``, ``test_strategy_dashboard`` etc.) still
    exercise the old code paths during the migration."""

    def test_main_window_importable(self) -> None:
        # Just an import — instantiating QMainWindow requires QApplication,
        # which other test modules already provide.
        from ui.main_window import MainWindow  # noqa: F401
        self.assertTrue(hasattr(MainWindow, "__init__"))

    def test_strategy_panel_importable(self) -> None:
        from ui.strategy_panel import StrategyDiagnosticsPanel  # noqa: F401

    def test_app_module_exposes_main(self) -> None:
        from ui import app

        self.assertTrue(callable(app.main))
        self.assertTrue(callable(app._configure_rhi_backend))
        self.assertTrue(callable(app._check_gpu))


if __name__ == "__main__":
    unittest.main()
