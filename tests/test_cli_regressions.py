"""Focused regressions for the runtime and CLI defects from the macOS E2E run."""

from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import ast
from importlib.metadata import version as distribution_version
import json
import os
import subprocess
import sys
import tempfile
import unittest
from uuid import UUID
from unittest.mock import patch

import memory_module
from memory_module import cli, main as server_main
from memory_module.cli import cmd_batch, cmd_interactive, cmd_list, cmd_recall, cmd_store
from memory_module.localization import Localization
from memory_module.text_memory import TextMemory
from memory_module import update_checker


class _ListMemory:
    def list_memories(self, source, limit):
        assert source == "both"
        assert limit == 20
        return [
            {
                "index": 3,
                "layer": "stm",
                "key_text": "harbor code",
                "value_text": "AZURE-PINE-482",
                "intensity": 1.25,
                "usage": 2,
                "age": 4,
                "trusted": False,
            }
        ]


class _RecallMemory:
    def __init__(self):
        self.save_calls = 0

    def recall(self, query, top_k):
        assert query == "harbor"
        assert top_k == 5
        return Namespace(
            text="AZURE-PINE-482",
            key_text="harbor code",
            confidence=0.8754,
            source="STM",
        )

    def save(self):
        self.save_calls += 1


class _CaptureStoreMemory:
    def __init__(self):
        self.store_calls = []
        self.save_calls = 0

    def store(self, key, value, **kwargs):
        self.store_calls.append((key, value, kwargs))
        return 1

    def save(self):
        self.save_calls += 1


class RuntimeCliRegressionTests(unittest.TestCase):
    def test_all_cli_write_paths_forward_one_process_scoped_provenance(self):
        memory = _CaptureStoreMemory()
        cmd_store(
            memory,
            Namespace(key="direct", value="one", emotion="neutral", intensity=1.0),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            batch_path = Path(temp_dir, "batch.tsv")
            batch_path.write_text("batch\ttwo\n", encoding="utf-8")
            cmd_batch(memory, Namespace(file=str(batch_path), separator="\t"))

        with patch("builtins.input", side_effect=["store interactive | three", "quit"]):
            cmd_interactive(memory, Namespace(auto_step=False))

        self.assertEqual(
            [(key, value) for key, value, _ in memory.store_calls],
            [("direct", "one"), ("batch", "two"), ("interactive", "three")],
        )
        provenances = [kwargs["provenance"] for _, _, kwargs in memory.store_calls]
        self.assertTrue(all(item["source_class"] == "cli" for item in provenances))
        self.assertTrue(all(item["origin"] == "local-cli" for item in provenances))
        session_ids = {item["session_id"] for item in provenances}
        self.assertEqual(len(session_ids), 1)
        session_id = session_ids.pop()
        self.assertTrue(session_id.startswith("cli:"))
        UUID(session_id.removeprefix("cli:"))

    def test_cli_create_and_reinforcement_persist_provenance_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = TextMemory(
                state_file=str(Path(temp_dir, "memory.bdbm")),
                auto_load=False,
            )
            args = Namespace(
                key="alpha key",
                value="alpha value",
                emotion="neutral",
                intensity=1.0,
            )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(cmd_store(memory, args), 0)
                self.assertEqual(cmd_store(memory, args), 0)

            self.assertIn("New centers: 1", output.getvalue())
            self.assertIn("New centers: 0", output.getvalue())
            provenance = memory.list_memories(limit=1)[0]["provenance"]
            self.assertEqual(provenance["source_class"], "cli")
            self.assertEqual(provenance["origin"], "local-cli")
            self.assertTrue(provenance["session_id"].startswith("cli:"))
            self.assertEqual(
                provenance["source_history"],
                [
                    {
                        "source_class": "cli",
                        "origin": "local-cli",
                        "session_id": provenance["session_id"],
                    }
                ],
            )

    def test_console_parsers_use_installed_entry_point_names(self):
        self.assertEqual(cli.create_parser().prog, "biomem")

        output = StringIO()
        with patch.object(sys, "argv", ["biomem-server", "--help"]):
            with redirect_stdout(output):
                with self.assertRaises(SystemExit) as exit_context:
                    server_main.parse_args()
        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn("usage: biomem-server", output.getvalue())

    def test_runtime_version_matches_distribution_metadata(self):
        self.assertEqual(
            memory_module.__version__,
            distribution_version("biomem-memory"),
        )

    def test_module_usage_examples_use_current_console_names(self):
        self.assertNotIn("memory-cli", cli.__doc__ or "")
        self.assertNotIn("memory-cli", memory_module.__doc__ or "")
        self.assertIn("biomem store", cli.__doc__ or "")
        self.assertIn("biomem store", memory_module.__doc__ or "")

    def test_list_uses_text_memory_record_schema_without_untranslated_keys(self):
        output = StringIO()
        with redirect_stdout(output):
            result = cmd_list(_ListMemory(), Namespace(source="both", limit=20))

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertNotIn("cli.found_n", rendered)
        self.assertIn("Found 1", rendered)
        self.assertIn("[stm] harbor code", rendered)
        self.assertIn("AZURE-PINE-482", rendered)

    def test_verbose_recall_formats_numeric_confidence_once(self):
        memory = _RecallMemory()
        output = StringIO()
        with redirect_stdout(output):
            result = cmd_recall(
                memory,
                Namespace(query="harbor", top_k=5, verbose=True),
            )

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertEqual(memory.save_calls, 1)
        self.assertIn("Confidence: 0.875", rendered)
        self.assertNotIn("{:.3f}", rendered)

    def test_recall_persists_read_count_and_usage_across_processes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir, "memory.bdbm")
            memory = TextMemory(state_file=str(state_path), auto_load=False)
            self.assertEqual(
                memory.store(
                    "alpha harbor",
                    "AZURE-PINE-482",
                    provenance={"source_class": "test"},
                ),
                1,
            )
            memory.save()
            self.assertEqual(memory.get_stats()["reads"], 0)
            self.assertEqual(memory.list_memories(limit=1)[0]["usage"], 0)

            environment = os.environ.copy()
            environment.update(
                {
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "memory_module.cli",
                    "--state-file",
                    str(state_path),
                    "recall",
                    "alpha harbor",
                    "--top-k",
                    "1",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("AZURE-PINE-482", completed.stdout)

            restored = TextMemory(state_file=str(state_path), auto_load=True)
            self.assertEqual(restored.get_stats()["reads"], 1)
            self.assertGreaterEqual(restored.list_memories(limit=1)[0]["usage"], 1)

    def test_update_checker_uses_package_network_helper(self):
        class _Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps({"name": "empty", "assets": []}).encode()

        with patch("urllib.request.urlopen", lambda *args, **kwargs: _Response()):
            with patch("memory_module.net.build_ssl_context", lambda: None):
                self.assertEqual(update_checker._fetch_release_assets(), [])

    def test_smoke_script_has_one_direct_execution_guard(self):
        smoke_path = Path(__file__).with_name("test_smoke.py")
        tree = ast.parse(smoke_path.read_text(encoding="utf-8"))
        guards = [
            node
            for node in tree.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ]
        self.assertEqual(len(guards), 1)

    def test_localized_cli_count_and_confidence_are_format_compatible(self):
        self.assertEqual(Localization.get("cli.found_n", 3), "Found 3 memories")
        self.assertEqual(
            Localization.get("cli.confidence", 0.8754),
            "   Confidence: 0.875",
        )


if __name__ == "__main__":
    unittest.main()
