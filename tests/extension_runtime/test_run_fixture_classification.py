"""Deterministic status classification tests for the local runtime fixture."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


RUNNER_PATH = Path(__file__).with_name("run_fixture.py")
SPEC = importlib.util.spec_from_file_location("biomem_extension_runtime_fixture", RUNNER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import precondition
    raise RuntimeError(f"cannot load fixture runner: {RUNNER_PATH}")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class _ExitedProcess:
    returncode = 17
    stdout = None

    def poll(self):
        return self.returncode


class FixtureClassificationTests(unittest.TestCase):
    def test_daemon_nonzero_after_launch_is_fail(self):
        with tempfile.TemporaryDirectory() as temp_root:
            stdout_path = Path(temp_root) / "daemon-stdout.log"
            stdout_path.write_text("synthetic daemon crash\n", encoding="utf-8")
            process = _ExitedProcess()
            process._biomem_stdout_path = stdout_path

            with self.assertRaises(runner.FixtureFailure) as raised:
                runner._wait_for_health(1, process, timeout=0.01)

        self.assertNotIsInstance(raised.exception, runner.BlockedEnvironment)
        self.assertEqual(runner._classification(raised.exception), ("FAIL", 1))

    def test_unexpected_runtime_exception_is_fail(self):
        self.assertEqual(runner._classification(RuntimeError("synthetic runtime exception")), ("FAIL", 1))

    def test_missing_explicit_chrome_is_blocked_environment(self):
        missing = "/definitely/missing/biomem-fixture-chrome"
        with mock.patch.dict(os.environ, {"BIOMEM_FIXTURE_CHROME": missing}, clear=False):
            with self.assertRaises(runner.BlockedEnvironment) as raised:
                runner._chrome_path()
        self.assertEqual(runner._classification(raised.exception), ("BLOCKED_ENVIRONMENT", 2))

    def test_missing_project_interpreter_is_blocked_environment(self):
        missing = Path("/definitely/missing/biomem-fixture-python")
        with mock.patch.object(runner, "PYTHON", missing):
            with self.assertRaises(runner.BlockedEnvironment) as raised:
                runner._start_daemon(Path(tempfile.gettempdir()) / "unused-biomem-data", 23001)
        self.assertEqual(runner._classification(raised.exception), ("BLOCKED_ENVIRONMENT", 2))

    def test_permission_precondition_is_blocked_environment(self):
        self.assertEqual(runner._classification(PermissionError("denied")), ("BLOCKED_ENVIRONMENT", 2))


if __name__ == "__main__":
    unittest.main()
