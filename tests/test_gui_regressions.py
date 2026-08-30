"""Offline regressions for the desktop dashboard startup contract."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _DashboardStub:
    """Implements the same two-argument message boundary as BDBMDashboard."""

    def __init__(self, *, cancel_when_ready: bool = False):
        self.messages: list[tuple[str, dict]] = []
        self.loop = None
        self.server_task = None
        self.cancel_when_ready = cancel_when_ready

    def set_async_loop(self, loop):
        self.loop = loop

    def set_server_task(self, task):
        self.server_task = task

    def set_command_handler(self, handler):
        self.command_handler = handler

    def post_message(self, msg_type, data=None):
        self.messages.append((msg_type, data or {}))
        if msg_type == "server_ready" and self.cancel_when_ready:
            assert self.server_task is not None
            assert self.server_task.get_loop().is_running()
            self.server_task.cancel()


class _SettingsStub:
    def get_session_hash(self):
        return "offline-test"


def _background_stubs(server_type):
    dashboard_module = types.ModuleType("memory_module.dashboard")
    for name, value in {
        "MSG_STATUS_UPDATE": "status_update",
        "MSG_CONV_HANDLER_READY": "conv_handler_ready",
        "MSG_MEMORY_STATS": "memory_stats",
        "MSG_SERVER_READY": "server_ready",
        "fetch_news_async": lambda *_args, **_kwargs: None,
    }.items():
        setattr(dashboard_module, name, value)

    ws_module = types.ModuleType("memory_module.ws_server")
    ws_module.BDBMServer = server_type

    update_module = types.ModuleType("memory_module.update_checker")
    update_module.check_for_update_async = lambda *_args, **_kwargs: None

    telemetry_module = types.ModuleType("memory_module.telemetry")

    class TelemetryClient:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            await asyncio.Future()

    telemetry_module.TelemetryClient = TelemetryClient

    return {
        "memory_module.dashboard": dashboard_module,
        "memory_module.ws_server": ws_module,
        "memory_module.update_checker": update_module,
        "memory_module.telemetry": telemetry_module,
    }


def _background_args():
    return Namespace(host="127.0.0.1", port=8765)


class GuiRegressionTests(unittest.TestCase):

    def test_background_failure_uses_structured_dashboard_payload(self):
        from memory_module.config import MemoryConfig
        from memory_module.main import _run_background_server

        class FailingServer:
            def __init__(self, **_kwargs):
                raise RuntimeError("model failed")

        dashboard = _DashboardStub()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, _background_stubs(FailingServer)
        ):
            tmp_path = Path(tmp)
            _run_background_server(
                MemoryConfig(),
                _background_args(),
                str(tmp_path / "memory.bdbm"),
                tmp_path,
                _SettingsStub(),
                dashboard=dashboard,
            )

        self.assertEqual(dashboard.messages[0][0], "status_update")
        self.assertTrue(dashboard.messages[0][1]["detail"])
        kind, payload = dashboard.messages[-1]
        self.assertEqual(kind, "status_update")
        self.assertTrue(payload["text"])
        self.assertEqual(payload["detail"], "model failed")
        self.assertEqual(payload["color"], "#ef476f")

    def test_server_ready_is_emitted_after_running_and_shutdown_is_clean(self):
        from memory_module.config import MemoryConfig
        from memory_module.main import _run_background_server

        instances = []

        class RunningServer:
            def __init__(self, **_kwargs):
                self.is_running = False
                self.memory = SimpleNamespace(
                    embedder=SimpleNamespace(model=object()),
                    backup=lambda: None,
                    get_stats=lambda: {},
                )
                self.handler = object()
                instances.append(self)

            async def start(self):
                self.is_running = True
                try:
                    await asyncio.Future()
                finally:
                    self.is_running = False

        dashboard = _DashboardStub(cancel_when_ready=True)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, _background_stubs(RunningServer)
        ):
            tmp_path = Path(tmp)
            _run_background_server(
                MemoryConfig(),
                _background_args(),
                str(tmp_path / "memory.bdbm"),
                tmp_path,
                _SettingsStub(),
                dashboard=dashboard,
            )

        self.assertTrue(instances)
        self.assertFalse(instances[0].is_running)
        self.assertEqual(
            [kind for kind, _ in dashboard.messages].count("server_ready"), 1
        )
        self.assertFalse(any(
            kind == "status_update" and payload.get("color") == "#ef476f"
            for kind, payload in dashboard.messages
        ))

    def test_dashboard_local_imports_are_package_relative(self):
        tree = ast.parse(
            (SRC / "memory_module" / "dashboard.py").read_text(encoding="utf-8")
        )
        invalid = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module in {"localization", "net"}
            and node.level == 0
        ]
        self.assertEqual(invalid, [])

    def test_main_post_message_calls_use_one_payload_argument(self):
        tree = ast.parse(
            (SRC / "memory_module" / "main.py").read_text(encoding="utf-8")
        )
        invalid = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "post_message"
            and (node.keywords or len(node.args) != 2)
        ]
        self.assertEqual(invalid, [])

    def test_missing_tray_dependency_is_non_fatal(self):
        from memory_module import tray_icon

        tray = tray_icon.BDBMTrayIcon()
        with patch.object(tray_icon, "_ensure_imports", side_effect=ImportError("missing")):
            self.assertIsNone(tray.start())
        self.assertIsNone(tray._thread)

    @unittest.skipUnless(importlib.util.find_spec("PyQt6"), "PyQt6 is optional")
    def test_dashboard_constructs_and_processes_message_offscreen(self):
        from PyQt6.QtWidgets import QApplication
        from memory_module.dashboard import BDBMDashboard, MSG_STATUS_UPDATE
        from memory_module.settings_manager import SettingsManager

        with tempfile.TemporaryDirectory() as tmp:
            dashboard = BDBMDashboard(SettingsManager(Path(tmp)))
            dashboard.post_message(MSG_STATUS_UPDATE, {
                "text": "READY",
                "detail": "offline smoke",
                "color": "#10b981",
            })
            QApplication.processEvents()
            self.assertEqual(dashboard.status_label.text(), "READY")
            self.assertEqual(dashboard.status_detail.text(), "offline smoke")
            dashboard.hide()
            dashboard.deleteLater()
            QApplication.processEvents()


if __name__ == "__main__":
    unittest.main()
