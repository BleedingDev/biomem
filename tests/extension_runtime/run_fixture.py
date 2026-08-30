#!/usr/bin/env python3
"""Deterministic local browser-memory lifecycle fixture.

Runs two clean rounds with a real loopback biomem daemon and Chrome for Testing.
The provider page, messages, and canaries are synthetic; no provider account or
normal browser profile is touched.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from websockets.sync.client import connect


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = REPO_ROOT / ".venv-mac" / "bin" / "python"
CHROME_CANDIDATES = (
    Path("/Users/satan/.agent-browser/browsers/chrome-151.0.7922.34/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
)


class FixtureFailure(AssertionError):
    pass


class BlockedEnvironment(FixtureFailure):
    """A positively identified local precondition prevents the fixture run."""


def _classification(error: BaseException) -> tuple[str, int]:
    if isinstance(error, (BlockedEnvironment, PermissionError)):
        return "BLOCKED_ENVIRONMENT", 2
    return "FAIL", 1


def _free_port_pair() -> tuple[int, int]:
    start = 18_000 + secrets.randbelow(9_000)
    for offset in range(10_000):
        port = 18_000 + ((start - 18_000 + offset) % 10_000)
        if port >= 27_999:
            continue
        with socket.socket() as first:
            try:
                first.bind(("127.0.0.1", port))
            except OSError:
                continue
        with socket.socket() as second:
            try:
                second.bind(("127.0.0.1", port + 1))
            except OSError:
                continue
        return port, port + 1
    raise BlockedEnvironment("unable to reserve adjacent owned loopback ports")


def _free_port() -> int:
    start = 30_000 + secrets.randbelow(9_000)
    for offset in range(10_000):
        port = 30_000 + ((start - 30_000 + offset) % 10_000)
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise BlockedEnvironment("unable to reserve an owned loopback browser-debug port")


def _request_json(url: str, payload: dict[str, Any] | None = None, timeout: float = 10) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_health(http_port: int, process: subprocess.Popen[str], timeout: float = 45) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout_path = getattr(process, "_biomem_stdout_path", None)
            try:
                output = Path(stdout_path).read_text(encoding="utf-8", errors="replace") if stdout_path else ""
            except OSError:
                output = ""
            raise FixtureFailure(
                f"daemon exited before health check ({process.returncode}):\n{output[-1200:]}"
            )
        try:
            data = _request_json(f"http://127.0.0.1:{http_port}/api/health", timeout=2)
            if data.get("ready") is True and data.get("product") == "biomem":
                return data
            last_error = repr(data)
        except Exception as error:  # service is still starting
            last_error = str(error)
        time.sleep(0.2)
    raise FixtureFailure(f"daemon health timeout after process start: {last_error}")


def _start_daemon(data_root: Path, ws_port: int) -> subprocess.Popen[str]:
    if not PYTHON.exists():
        raise BlockedEnvironment(f"missing project interpreter: {PYTHON}")
    if not os.access(PYTHON, os.X_OK):
        raise BlockedEnvironment(f"project interpreter is not executable: {PYTHON}")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["HTTP_PROXY"] = "http://127.0.0.1:9"
    env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    env["ALL_PROXY"] = "http://127.0.0.1:9"
    env["NO_PROXY"] = ""
    env["no_proxy"] = ""
    data_root.mkdir(parents=True, exist_ok=True)
    stdout_path = data_root / "daemon-stdout.log"
    stdout_stream = stdout_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            str(PYTHON), "-m", "memory_module.main",
            "--host", "127.0.0.1",
            "--port", str(ws_port),
            "--data-dir", str(data_root),
            "--no-gui", "--no-tray",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=stdout_stream,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    stdout_stream.close()
    process._biomem_stdout_path = stdout_path  # type: ignore[attr-defined]
    try:
        _wait_for_health(ws_port + 1, process)
    except Exception:
        _stop_process(process, "daemon startup")
        raise
    return process


def _stop_process(process: subprocess.Popen[str], name: str) -> None:
    process_group = process.pid
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGKILL)
        process.wait(timeout=5)
        raise FixtureFailure(f"{name} required forced termination")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except (ProcessLookupError, PermissionError):
            # The owned parent has already been reaped. EPERM during a
            # zero-signal probe can mean macOS has already recycled the PGID
            # for an unsignalable group; never escalate against that identity.
            # Callers separately prove every owned listener port is closed.
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process_group, signal.SIGKILL)
    time.sleep(0.1)
    try:
        os.killpg(process_group, 0)
    except (ProcessLookupError, PermissionError):
        return
    raise FixtureFailure(f"{name} owned process group {process_group} is still alive")


def _wait_ports_closed(ports: tuple[int, ...], owner: str, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        open_ports = []
        for port in ports:
            with socket.socket() as sock:
                sock.settimeout(0.1)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    open_ports.append(port)
        if not open_ports:
            return
        time.sleep(0.05)
    raise FixtureFailure(f"{owner} owned loopback ports did not close: {open_ports}")


def _daemon_stdout_tail(data_root: Path, max_chars: int = 3000) -> str:
    path = data_root / "daemon-stdout.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<daemon stdout unavailable>"
    return text[-max_chars:]


class _StaticServer:
    def __init__(self) -> None:
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, directory=str(REPO_ROOT), **kwargs)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        handler = QuietHandler
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def __enter__(self) -> "_StaticServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _CDP:
    def __init__(self, websocket_url: str) -> None:
        self.socket = connect(websocket_url, open_timeout=5, close_timeout=2, legacy=True)
        self.next_id = 1

    def close(self) -> None:
        self.socket.close()

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.socket.recv(timeout=max(0.1, deadline - time.monotonic()))
            message = json.loads(raw)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise FixtureFailure(f"CDP {method} failed: {message['error']}")
            return message.get("result", {})
        raise FixtureFailure(f"CDP {method} timed out")


def _chrome_path() -> Path:
    override = os.environ.get("BIOMEM_FIXTURE_CHROME")
    if override:
        path = Path(override)
        if path.exists() and os.access(path, os.X_OK):
            return path
        if not path.exists():
            raise BlockedEnvironment(f"BIOMEM_FIXTURE_CHROME does not exist: {path}")
        raise BlockedEnvironment(f"BIOMEM_FIXTURE_CHROME is not executable: {path}")
    for path in CHROME_CANDIDATES:
        if path.exists() and os.access(path, os.X_OK):
            return path
    raise BlockedEnvironment("no executable Chrome/Chromium installation found")


def _wait_for_debugger(port: int, process: subprocess.Popen[str], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise FixtureFailure(f"Chrome exited before CDP became ready:\n{output[-3000:]}")
        try:
            _request_json(f"http://127.0.0.1:{port}/json/version", timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise FixtureFailure("Chrome DevTools endpoint did not start after process launch")


def _run_browser_page(
    static_port: int,
    query: dict[str, str],
    timeout: float = 35,
    failure_state_path: Path | None = None,
) -> dict[str, Any]:
    chrome = _chrome_path()
    debug_port = _free_port()
    with tempfile.TemporaryDirectory(prefix="biomem-fixture-chrome-") as profile:
        process = subprocess.Popen(
            [
                str(chrome),
                "--headless=new",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--metrics-recording-only",
                "--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE 127.0.0.1",
                "about:blank",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        cdp: _CDP | None = None
        try:
            _wait_for_debugger(debug_port, process)
            encoded = urllib.parse.urlencode(query)
            target_url = f"http://127.0.0.1:{static_port}/tests/extension_runtime/fixture.html?{encoded}"
            request = urllib.request.Request(
                f"http://127.0.0.1:{debug_port}/json/new?{urllib.parse.quote(target_url, safe='')}",
                method="PUT",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                target = json.loads(response.read().decode("utf-8"))
            cdp = _CDP(target["webSocketDebuggerUrl"])
            cdp.call("Runtime.enable")

            deadline = time.monotonic() + timeout
            last_status: Any = None
            while time.monotonic() < deadline:
                evaluated = cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify(window.__fixture || null)",
                        "returnByValue": True,
                    },
                )
                remote = evaluated.get("result", {})
                value = remote.get("value")
                if value:
                    state = json.loads(value)
                    last_status = state.get("status")
                    if last_status == "failed":
                        if failure_state_path is not None:
                            failure_state_path.write_text(
                                json.dumps(state, indent=2, ensure_ascii=False),
                                encoding="utf-8",
                            )
                        raise FixtureFailure(f"browser fixture failed: {state.get('error')}")
                    if last_status == "complete":
                        return state
                time.sleep(0.1)
            raise FixtureFailure(f"browser fixture timeout (last status={last_status!r})")
        finally:
            if cdp is not None:
                with contextlib.suppress(Exception):
                    cdp.close()
            _stop_process(process, "Chrome")
            _wait_ports_closed((debug_port,), "Chrome")


def _daemon_command(http_port: int, command: dict[str, Any]) -> dict[str, Any]:
    return _request_json(f"http://127.0.0.1:{http_port}/api", command, timeout=15)


def _assert_bounded_prompt(prompt: str, label: str) -> None:
    text = str(prompt or "")
    if re.search(r"\bUnknown\b", text, flags=re.IGNORECASE):
        raise FixtureFailure(f"{label}: non-informative Unknown memory reached the provider prompt")
    blocks = re.findall(r"<relevant_memories>\s*([\s\S]*?)\s*</relevant_memories>", text, flags=re.IGNORECASE)
    for block in blocks:
        memory_count = sum(1 for line in block.splitlines() if line.strip().startswith("User:"))
        if memory_count > 5:
            raise FixtureFailure(f"{label}: prompt contains {memory_count} memories; limit is 5")


def _assert_round(round_no: int, static_port: int) -> dict[str, Any]:
    token = secrets.token_hex(8)
    user_canary = f"user-{round_no}-{token}"
    answer_canary = f"answer-{round_no}-{token}"
    second_user_canary = f"second-user-{round_no}-{token}"
    second_answer_canary = f"second-answer-{round_no}-{token}"
    invalid_canary = f"invalid-{round_no}-{token}"
    first_query = f"Record observatory telescope aperture calibration {user_canary}"
    second_query = f"Save saffron risotto emulsification cooking technique {second_user_canary}"
    first_response = (
        f"Astronomy calibration {answer_canary}: align the telescope aperture "
        "with a spectrograph reference star."
    )
    second_response = (
        f"Cooking technique {second_answer_canary}: toast the risotto rice, add saffron "
        "stock gradually, then emulsify."
    )
    ws_port, http_port = _free_port_pair()

    temp_root = Path(tempfile.mkdtemp(prefix=f"biomem-fixture-round-{round_no}-"))
    data_root = temp_root / "daemon-data"
    completed = False
    try:
        process = _start_daemon(data_root, ws_port)
        try:
            browser = _run_browser_page(
                static_port,
                {
                    "daemon_port": str(http_port),
                    "scenario": "turns",
                    "user_canary": user_canary,
                    "answer_canary": answer_canary,
                    "second_user_canary": second_user_canary,
                    "second_answer_canary": second_answer_canary,
                    "invalid_canary": invalid_canary,
                },
                failure_state_path=temp_root / "browser-failure-state.json",
            )
            result = browser["result"]
            records = _daemon_command(http_port, {"command": "list_memories", "layer": "both", "limit": 100})["records"]
            (temp_root / "pre-restart-evidence.json").write_text(
                json.dumps(
                    {
                        "ws_port": ws_port,
                        "http_port": http_port,
                        "browser_result": result,
                        "daemon_records": records,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            if result["retrieveSuccesses"] != 3:
                raise FixtureFailure(
                    f"round {round_no}: expected one retrieve per two valid and one invalid turn, "
                    f"got {result['retrieveSuccesses']}"
                )
            if result["storesBeforeRapidSecond"] != 0:
                raise FixtureFailure(
                    f"round {round_no}: first store completed before rapid second submit; "
                    "the pre-debounce case was not exercised"
                )
            if len(result["afterValid"]) != 2 or len(result["afterInvalid"]) != 2 or len(records) != 2:
                record_evidence = [
                    {
                        "key": item.get("key"),
                        "value": item.get("value"),
                        "provenance": item.get("provenance"),
                    }
                    for item in records
                ]
                raise FixtureFailure(
                    f"round {round_no}: invalid UI or duplicate mutation changed record count "
                    f"(valid={len(result['afterValid'])}, invalid={len(result['afterInvalid'])}, daemon={len(records)}, "
                    f"successful_stores={result['storeSuccesses']}); records={record_evidence}"
                )
            turn_commands = [
                entry for entry in result["commandLog"]
                if entry["command"] in ("retrieve", "store")
            ]
            retrieve_attempts = [entry for entry in turn_commands if entry["command"] == "retrieve"]
            store_attempts = [entry for entry in turn_commands if entry["command"] == "store"]
            if len(retrieve_attempts) != 3 or len(store_attempts) != 2:
                raise FixtureFailure(
                    f"round {round_no}: expected three retrieve and exactly two store attempts, "
                    f"got retrieve={len(retrieve_attempts)}, store={len(store_attempts)}"
                )
            if any(entry["request"].get("top_k") != 20 for entry in retrieve_attempts):
                raise FixtureFailure(
                    f"round {round_no}: public retrieve candidate limit is not uniformly top_k=20: "
                    f"{[entry['request'].get('top_k') for entry in retrieve_attempts]}"
                )
            for prompt_index, submitted_prompt in enumerate(result["submittedPrompts"], 1):
                _assert_bounded_prompt(submitted_prompt, f"round {round_no} turn prompt {prompt_index}")
            if re.search(r"\bUnknown\b", json.dumps(records, ensure_ascii=False), flags=re.IGNORECASE):
                raise FixtureFailure(f"round {round_no}: Unknown value appeared in daemon records")
            retrieves_by_query = {entry["request"]["query"]: entry for entry in retrieve_attempts}
            valid_queries = [first_query, second_query]
            invalid_query = f"Trigger invalid UI {invalid_canary}"
            if set(retrieves_by_query) != set(valid_queries + [invalid_query]):
                raise FixtureFailure(
                    f"round {round_no}: retrieve queries do not match the three synthetic turns: "
                    f"{list(retrieves_by_query)}"
                )
            valid_sessions = [retrieves_by_query[query]["request"].get("session_id") for query in valid_queries]
            invalid_session = retrieves_by_query[invalid_query]["request"].get("session_id")
            if not all(valid_sessions) or not invalid_session or len(set(valid_sessions + [invalid_session])) != 3:
                raise FixtureFailure(f"round {round_no}: turn retrieve sessions are missing or not distinct")
            store_sessions = [entry["request"].get("session_id") for entry in store_attempts]
            if len(set(store_sessions)) != 2 or set(store_sessions) != set(valid_sessions):
                raise FixtureFailure(
                    f"round {round_no}: rapid valid stores are not paired one-to-one with retrieve sessions: "
                    f"retrieve={valid_sessions}, store={store_sessions}"
                )
            for session_id in valid_sessions:
                retrieve_index = next(
                    index for index, entry in enumerate(turn_commands)
                    if entry["command"] == "retrieve" and entry["request"].get("session_id") == session_id
                )
                store_entry = next(
                    entry for entry in store_attempts if entry["request"].get("session_id") == session_id
                )
                store_index = turn_commands.index(store_entry)
                if store_index <= retrieve_index:
                    raise FixtureFailure(f"round {round_no}: store preceded its retrieve for session {session_id}")
                provenance = store_entry["request"].get("provenance") or {}
                if provenance.get("session_id") != session_id:
                    raise FixtureFailure(
                        f"round {round_no}: outbound store provenance session mismatch for {session_id}"
                    )
            invalid_retrieve_index = next(
                index for index, entry in enumerate(turn_commands)
                if entry["command"] == "retrieve" and entry["request"].get("session_id") == invalid_session
            )
            if any(index > invalid_retrieve_index and entry["command"] == "store" for index, entry in enumerate(turn_commands)):
                raise FixtureFailure(f"round {round_no}: invalid provider turn emitted a later store")
            if result["storeSuccesses"] != 2:
                raise FixtureFailure(f"round {round_no}: expected exactly two successful stores, got {result['storeSuccesses']}")

            expected_records = [
                (first_query, first_response, valid_sessions[0]),
                (second_query, second_response, valid_sessions[1]),
            ]
            for expected_key, expected_value, expected_session in expected_records:
                matching = [item for item in records if item["key"] == expected_key]
                if len(matching) != 1 or matching[0]["value"] != expected_value:
                    raise FixtureFailure(
                        f"round {round_no}: stored record does not preserve rapid turn "
                        f"{expected_key}/{expected_value}: {matching}"
                    )
                provenance = matching[0].get("provenance") or {}
                if (
                    provenance.get("source_class") != "browser"
                    or provenance.get("origin") != "127.0.0.1"
                    or provenance.get("session_id") != expected_session
                ):
                    raise FixtureFailure(
                        f"round {round_no}: daemon provenance mismatch for {expected_key}: {provenance}"
                    )
            if invalid_canary in json.dumps(records, ensure_ascii=False):
                raise FixtureFailure(f"round {round_no}: invalid provider UI was persisted")
        finally:
            _stop_process(process, "daemon")
            _wait_ports_closed((ws_port, http_port), "daemon")

        process = _start_daemon(data_root, ws_port)
        try:
            persisted = _daemon_command(http_port, {"command": "list_memories", "layer": "both", "limit": 100})["records"]
            before_ids = {item["memory_id"] for item in records}
            after_ids = {item["memory_id"] for item in persisted}
            if len(persisted) != 2 or after_ids != before_ids:
                raise FixtureFailure(
                    f"round {round_no}: both record identities did not survive daemon restart: "
                    f"before={before_ids}, after={after_ids}"
                )
            if invalid_canary in json.dumps(persisted, ensure_ascii=False):
                raise FixtureFailure(f"round {round_no}: invalid provider UI appeared after restart")

            recall_results = []
            for recall_target, expected_answer in (
                ("first", answer_canary),
                ("second", second_answer_canary),
            ):
                recall = _run_browser_page(
                    static_port,
                    {
                        "daemon_port": str(http_port),
                        "scenario": "recall",
                        "recall_target": recall_target,
                        "user_canary": user_canary,
                        "answer_canary": answer_canary,
                        "second_user_canary": second_user_canary,
                        "second_answer_canary": second_answer_canary,
                        "invalid_canary": invalid_canary,
                    },
                )["result"]
                (temp_root / f"recall-{recall_target}-raw.json").write_text(
                    json.dumps(recall, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                recalled = recall["retrievedMemories"]
                if not isinstance(recalled, list) or not recalled:
                    raise FixtureFailure(
                        f"round {round_no}: {recall_target} recall returned no memory list"
                    )
                malformed_items = [item for item in recalled if not isinstance(item, dict)]
                if malformed_items:
                    raise FixtureFailure(
                        f"round {round_no}: {recall_target} recall returned malformed memory items: "
                        f"{malformed_items}"
                    )
                recall_turn_commands = [
                    entry for entry in recall["commandLog"]
                    if entry["command"] in ("retrieve", "store")
                ]
                if [entry["command"] for entry in recall_turn_commands] != ["retrieve"]:
                    raise FixtureFailure(
                        f"round {round_no}: {recall_target} recall emitted a late or duplicate store: "
                        f"{[entry['command'] for entry in recall_turn_commands]}"
                    )
                retrieve_entry = recall_turn_commands[0]
                if not isinstance(retrieve_entry.get("request"), dict):
                    raise FixtureFailure(
                        f"round {round_no}: {recall_target} recall command is missing its request payload"
                    )
                if retrieve_entry["request"].get("top_k") != 20:
                    raise FixtureFailure(
                        f"round {round_no}: {recall_target} recall did not request top_k=20"
                    )
                expected_key, expected_value, expected_original_session = (
                    expected_records[0] if recall_target == "first" else expected_records[1]
                )
                persisted_match = [item for item in persisted if item.get("key") == expected_key]
                if len(persisted_match) != 1:
                    raise FixtureFailure(
                        f"round {round_no}: {recall_target} persisted target is missing or ambiguous: "
                        f"{persisted_match}"
                    )
                expected_id = persisted_match[0]["memory_id"]
                recalled_match = [item for item in recalled if item.get("memory_id") == expected_id]
                if len(recalled_match) != 1:
                    raise FixtureFailure(
                        f"round {round_no}: {recall_target} did not return stable ID {expected_id}: "
                        f"{recalled}"
                    )
                recalled_record = recalled_match[0]
                recalled_provenance = recalled_record.get("provenance")
                if not isinstance(recalled_provenance, dict):
                    raise FixtureFailure(
                        f"round {round_no}: {recall_target} record has malformed provenance: "
                        f"{recalled_provenance}"
                    )
                if (
                    recalled_record.get("key") != expected_key
                    or recalled_record.get("value") != expected_value
                    or recalled_record.get("model") != expected_value
                    or recalled_provenance.get("source_class") != "browser"
                    or recalled_provenance.get("origin") != "127.0.0.1"
                    or recalled_provenance.get("session_id") != expected_original_session
                ):
                    raise FixtureFailure(
                        f"round {round_no}: {recall_target} record identity/content/provenance mismatch: "
                        f"{recalled_record}"
                    )
                if invalid_canary in json.dumps(recalled, ensure_ascii=False):
                    raise FixtureFailure(f"round {round_no}: invalid canary appeared in {recall_target} recall")
                submitted = recall["submittedPrompt"]
                _assert_bounded_prompt(submitted, f"round {round_no} {recall_target} recall prompt")
                if expected_answer not in submitted or "<relevant_memories>" not in submitted:
                    raise FixtureFailure(
                        f"round {round_no}: {recall_target} memory was not injected into the provider prompt"
                    )
                recall_results.append(recall)
            recall_sessions = [
                next(
                    entry["request"]["session_id"]
                    for entry in recall["commandLog"]
                    if entry["command"] == "retrieve"
                )
                for recall in recall_results
            ]
            if len(set(recall_sessions)) != 2 or set(recall_sessions) & set(valid_sessions + [invalid_session]):
                raise FixtureFailure(
                    f"round {round_no}: recall sessions are not two distinct new sessions: {recall_sessions}"
                )
            (temp_root / "post-restart-evidence.json").write_text(
                json.dumps(
                    {"persisted_records": persisted, "new_conversation_recalls": recall_results},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        finally:
            _stop_process(process, "daemon")
            _wait_ports_closed((ws_port, http_port), "daemon")
        completed = True
        return {
            "record_count": 2,
            "store_successes": 2,
            "retrieve_before_turn": True,
            "retrieve_candidate_top_k": 20,
            "prompt_memory_limit": 5,
            "unknown_absent": True,
            "rapid_two_valid_turns_before_debounce": True,
            "distinct_valid_sessions": True,
            "delayed_final_node": True,
            "repeated_mutations_exact_once": True,
            "invalid_ui_absent": True,
            "restart_persisted": True,
            "new_conversation_recalled": True,
            "two_distinct_recall_sessions": True,
            "provenance": {"source_class": "browser", "origin": "127.0.0.1"},
            "run_evidence": {
                "data_root": str(temp_root),
                "ws_port": ws_port,
                "http_port": http_port,
                "data_root_cleaned": True,
                "owned_ports_closed": True,
            },
        }
    except (BlockedEnvironment, PermissionError) as error:
        traceback_text = traceback.format_exc()
        (temp_root / "failure-traceback.txt").write_text(traceback_text, encoding="utf-8")
        stdout_tail = _daemon_stdout_tail(data_root)
        raise BlockedEnvironment(
            f"{error}; evidence retained at {temp_root}; traceback: {temp_root / 'failure-traceback.txt'}; "
            f"daemon stdout tail:\n{stdout_tail}"
        ) from error
    except Exception as error:
        traceback_text = traceback.format_exc()
        (temp_root / "failure-traceback.txt").write_text(traceback_text, encoding="utf-8")
        stdout_tail = _daemon_stdout_tail(data_root)
        raise FixtureFailure(
            f"{error}; evidence retained at {temp_root}; traceback: {temp_root / 'failure-traceback.txt'}; "
            f"daemon stdout tail:\n{stdout_tail}"
        ) from error
    finally:
        if completed:
            shutil.rmtree(temp_root)


def main() -> int:
    try:
        with _StaticServer() as static:
            outcomes = [_assert_round(round_no, static.port) for round_no in (1, 2)]
        semantic_outcomes = [
            {key: value for key, value in outcome.items() if key != "run_evidence"}
            for outcome in outcomes
        ]
        if semantic_outcomes[0] != semantic_outcomes[1]:
            raise FixtureFailure(f"fresh-root semantic outcomes differ: {outcomes}")
        print(json.dumps({"status": "PASS", "rounds": outcomes}, indent=2, sort_keys=True))
        return 0
    except FixtureFailure as error:
        status, exit_code = _classification(error)
        print(json.dumps({"status": status, "error": str(error)}, indent=2), file=sys.stderr)
        return exit_code
    except Exception as error:
        status, exit_code = _classification(error)
        print(json.dumps({"status": status, "error": f"{type(error).__name__}: {error}"}, indent=2), file=sys.stderr)
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
