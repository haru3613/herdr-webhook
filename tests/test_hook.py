#!/usr/bin/env python3
"""Localhost-only tests for hook.py. Never calls a real Herdr or a remote URL."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hook.py"
FAKE_KEY = "test-key-not-real"
EVENT_NAME = "pane.agent_status_changed"


class _Recorder(HTTPServer):
    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.requests: list[dict] = []


def _handler():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            self.server.requests.append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
            )
            if self.path == "/fail":
                self.send_response(500)
                self.end_headers()
                return
            if self.path == "/redirect":
                host, port = self.server.server_address
                self.send_response(302)
                self.send_header("Location", f"http://{host}:{port}/next")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            return

    return Handler


def _event(**data) -> str:
    payload = {
        "pane_id": "w1:p1",
        "workspace_id": "w1",
        "agent_status": "done",
        **data,
    }
    return json.dumps({"event": EVENT_NAME, "data": payload})


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "config"
        self.config.mkdir()
        self.stub_log = self.dir / "herdr.log"
        self.stub = self.dir / "herdr"
        self.stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"Path({str(self.stub_log)!r}).write_text(' '.join(sys.argv[1:]) + '\\n')\n"
            "raw = os.environ.get('HERDR_STUB_AGENT_JSON') or '{}'\n"
            "if len(sys.argv) >= 3 and sys.argv[1:3] == ['agent', 'get']:\n"
            "    print(json.dumps({'result': {'agent': json.loads(raw)}}))\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC)
        self.server = _Recorder(("127.0.0.1", 0), _handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.base = f"http://{self.host}:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _write_env(self, extra: str = "", path: str = "/hook") -> None:
        (self.config / ".env").write_text(
            f"WEBHOOK_URL={self.base}{path}\n"
            f"WEBHOOK_KEY={FAKE_KEY}\n"
            f"{extra}",
            encoding="utf-8",
        )

    def _run(
        self,
        event_json: str,
        *,
        extra_env: dict[str, str] | None = None,
        agent: dict | None = None,
        herdr_bin: str | None = None,
    ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(
            {
                "HERDR_PLUGIN_EVENT": EVENT_NAME,
                "HERDR_PLUGIN_EVENT_JSON": event_json,
                "HERDR_PLUGIN_CONFIG_DIR": str(self.config),
                "HERDR_BIN_PATH": herdr_bin or str(self.stub),
                "HERDR_STUB_AGENT_JSON": json.dumps(agent or {}),
            }
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(HOOK)],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )

    def _assert_no_secrets(self, proc: subprocess.CompletedProcess) -> None:
        combined = f"{proc.stdout}\n{proc.stderr}"
        self.assertNotIn(FAKE_KEY, combined)
        self.assertNotIn(self.base, proc.stderr)
        self.assertNotIn("WEBHOOK_KEY", combined)

    def test_skip_from_config(self):
        self._write_env("SKIP_PANES=noise-*,logs\n")
        proc = self._run(
            _event(),
            agent={"name": "Noise-Worker", "cwd": "/tmp/src/api", "agent": "claude"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_no_secrets(proc)

    def test_dry_run_prints_payload_without_post(self):
        self._write_env("WEBHOOK_DRY_RUN=1\n")
        proc = self._run(
            _event(),
            extra_env={"HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_label": "api"})},
            agent={
                "name": "api",
                "cwd": "/tmp/src/api",
                "agent": "claude",
                "terminal_title_stripped": "claude",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["event"], EVENT_NAME)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["pane_id"], "w1:p1")
        self.assertEqual(payload["pane_name"], "api")
        self.assertEqual(payload["workspace_id"], "w1")
        self.assertEqual(payload["workspace_label"], "api")
        self.assertEqual(payload["cwd"], "/tmp/src/api")
        self.assertEqual(payload["agent"], "claude")
        self.assertIn("ts", payload)
        self.assertNotIn("pane name", payload)
        self._assert_no_secrets(proc)

    def test_h1_cwd_present_still_looks_up_name(self):
        self._write_env("SKIP_PANES=noise-pane\n")
        proc = self._run(
            _event(cwd="/tmp/src/api"),
            agent={"name": "noise-pane", "cwd": "/tmp/src/api", "agent": "claude"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self.assertTrue(self.stub_log.exists())
        self.assertIn("agent get w1:p1", self.stub_log.read_text(encoding="utf-8"))
        self._assert_no_secrets(proc)

    def test_redirect_is_not_followed(self):
        self._write_env(path="/redirect")
        proc = self._run(
            _event(),
            agent={"name": "api", "cwd": "/tmp/src/api", "agent": "claude"},
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["path"], "/redirect")
        self.assertIn("POST failed: HTTP 302", proc.stderr)
        self._assert_no_secrets(proc)

    def test_posts_snake_case_payload_and_bearer(self):
        self._write_env()
        proc = self._run(
            _event(),
            extra_env={"HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_label": "api"})},
            agent={
                "name": "api",
                "cwd": "/tmp/src/api",
                "agent": "claude",
                "terminal_title_stripped": "term",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        req = self.server.requests[0]
        self.assertEqual(req["headers"].get("authorization"), f"Bearer {FAKE_KEY}")
        self.assertNotIn("x-automation-key", req["headers"])
        payload = json.loads(req["body"])
        self.assertEqual(payload["pane_name"], "api")
        self.assertEqual(payload["pane_id"], "w1:p1")
        self.assertEqual(payload["workspace_label"], "api")
        self._assert_no_secrets(proc)

    def test_optional_key_header(self):
        self._write_env("WEBHOOK_KEY_HEADER=X-Automation-Key\n")
        proc = self._run(
            _event(),
            agent={"name": "api", "cwd": "/tmp/src/api", "agent": "claude"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        headers = self.server.requests[0]["headers"]
        self.assertEqual(headers.get("authorization"), f"Bearer {FAKE_KEY}")
        self.assertEqual(headers.get("x-automation-key"), FAKE_KEY)
        self._assert_no_secrets(proc)

    def test_fallback_pane_name_uses_terminal_title(self):
        self._write_env()
        proc = self._run(
            _event(),
            agent={
                "cwd": "/tmp/src/api",
                "agent": "claude",
                "terminal_title_stripped": "build worker",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(self.server.requests[0]["body"])
        self.assertEqual(payload["pane_name"], "build worker")
        self._assert_no_secrets(proc)

    def test_idle_does_not_post(self):
        self._write_env()
        proc = self._run(_event(agent_status="idle"), agent={"name": "api"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self.assertFalse(self.stub_log.exists())
        self._assert_no_secrets(proc)

    def test_missing_env_exits_zero(self):
        proc = self._run(_event(), agent={"name": "api"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_no_secrets(proc)

    def test_http_500(self):
        self._write_env(path="/fail")
        proc = self._run(
            _event(),
            agent={"name": "api", "cwd": "/tmp/src/api", "agent": "claude"},
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("POST failed: HTTP 500", proc.stderr)
        self._assert_no_secrets(proc)

    def test_wrong_event_env_is_ignored(self):
        self._write_env()
        proc = self._run(
            _event(),
            extra_env={"HERDR_PLUGIN_EVENT": "startup"},
            agent={"name": "api"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_no_secrets(proc)

    def test_lookup_failure_still_posts_with_stderr(self):
        self._write_env()
        proc = self._run(_event(), herdr_bin="/nonexistent/herdr-bin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self.assertIn("agent get failed for w1:p1", proc.stderr)
        self._assert_no_secrets(proc)

    def test_invalid_json_exits_zero(self):
        self._write_env()
        proc = self._run("{not-json")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("invalid HERDR_PLUGIN_EVENT_JSON", proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_no_secrets(proc)


if __name__ == "__main__":
    unittest.main()
