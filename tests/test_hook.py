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
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hook.py"
FAKE_KEY = "test-key-not-real"
EVENT_NAME = "pane.agent_status_changed"
API = {"name": "api", "cwd": "/tmp/src/api", "agent": "claude"}


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
            if self.path == "/idle-during-post":
                status_path = getattr(self.server, "status_path", "")
                if status_path:
                    Path(status_path).write_text(
                        json.dumps({"w1:p1": {"seen": "idle", "posted": None}}) + "\n",
                        encoding="utf-8",
                    )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return
            if self.path == "/slow-fail":
                time.sleep(2.0)
                self.send_response(500)
                self.end_headers()
                return
            if self.path == "/slow":
                time.sleep(2.0)
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
            "if len(sys.argv) >= 2 and sys.argv[1:3] == ['agent', 'list']:\n"
            "    raw = os.environ.get('HERDR_STUB_LIST_JSON') or '[]'\n"
            "    print(json.dumps({'result': {'type': 'agent_list', 'agents': json.loads(raw)}}))\n"
            "    raise SystemExit(0)\n"
            "raw = os.environ.get('HERDR_STUB_AGENT_JSON') or '{}'\n"
            "if len(sys.argv) >= 3 and sys.argv[1:3] == ['agent', 'get']:\n"
            "    agent = json.loads(raw)\n"
            "    seq_path = os.environ.get('HERDR_STUB_SEQ_PATH') or ''\n"
            "    if seq_path:\n"
            "        p = Path(seq_path)\n"
            "        if p.is_file():\n"
            "            seq = json.loads(p.read_text() or '[]')\n"
            "            if seq:\n"
            "                nxt = seq.pop(0)\n"
            "                p.write_text(json.dumps(seq))\n"
            "                if isinstance(nxt, str):\n"
            "                    agent['agent_status'] = nxt\n"
            "                elif isinstance(nxt, dict):\n"
            "                    agent.update(nxt)\n"
            "    print(json.dumps({'result': {'agent': agent}}))\n"
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
        self.server.status_path = str(self.config / "last-status.json")

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

    def _write_status(self, mapping: dict) -> None:
        (self.config / "last-status.json").write_text(
            json.dumps(mapping) + "\n",
            encoding="utf-8",
        )

    def _seq_env(self, items: list) -> dict[str, str]:
        path = self.dir / "agent-seq.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        return {"HERDR_STUB_SEQ_PATH": str(path)}

    def _read_status(self) -> dict:
        path = self.config / "last-status.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_pane(self, seen: str, posted: str | None, pane_id: str = "w1:p1") -> None:
        rec = self._read_status().get(pane_id)
        self.assertIsInstance(rec, dict, rec)
        self.assertEqual(rec.get("seen"), seen)
        self.assertEqual(rec.get("posted"), posted)

    def _run(
        self,
        event_json: str,
        *,
        extra_env: dict[str, str] | None = None,
        agent: dict | None = None,
        herdr_bin: str | None = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            capture_output=True,
            text=True,
            env=self._env(event_json, extra_env=extra_env, agent=agent, herdr_bin=herdr_bin),
            timeout=15,
            check=False,
        )

    def _start(
        self,
        event_json: str,
        *,
        extra_env: dict[str, str] | None = None,
        agent: dict | None = None,
    ) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(HOOK)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env(event_json, extra_env=extra_env, agent=agent, herdr_bin=None),
        )

    def _env(
        self,
        event_json: str,
        *,
        extra_env: dict[str, str] | None,
        agent: dict | None,
        herdr_bin: str | None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "SETTLE_MS",
        ):
            env.pop(key, None)
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"
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
        if "SETTLE_MS" not in env:
            env["SETTLE_MS"] = "0"
        return env

    def _assert_no_secrets(self, proc: subprocess.CompletedProcess) -> None:
        combined = f"{proc.stdout}\n{proc.stderr}"
        self.assertNotIn(FAKE_KEY, combined)
        self.assertNotIn(self.base, proc.stderr)
        self.assertNotIn("WEBHOOK_KEY", combined)
        self.assertNotIn("WEBHOOK_URL", combined)
        status_path = self.config / "last-status.json"
        if status_path.exists():
            text = status_path.read_text(encoding="utf-8")
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn("WEBHOOK", text)
            self.assertNotIn(self.base, text)

    def test_skip_from_config(self):
        self._write_env("SKIP_PANES=noise-*,logs\n")
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            agent={"name": "Noise-Worker", "cwd": "/tmp/src/api", "agent": "claude"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_no_secrets(proc)

    def test_skip_glob_portrait_pane_does_not_post_or_latch_posted(self):
        self._write_env("SKIP_PANES=*-portrait\n")
        self._write_status({"w1M:p1": "working"})
        proc = self._run(
            _event(pane_id="w1M:p1", workspace_id="w1M"),
            agent={"name": "tess-portrait", "cwd": "/tmp/src/tess", "agent": "claude"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", None, pane_id="w1M:p1")
        self._assert_no_secrets(proc)

    def test_skip_glob_portrait_leaves_other_panes_posting(self):
        self._write_env("SKIP_PANES=*-portrait\n")
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            agent={"name": "tess", "cwd": "/tmp/src/tess", "agent": "claude"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(json.loads(self.server.requests[0]["body"])["pane_name"], "tess")
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_dry_run_prints_payload_without_post(self):
        self._write_env("WEBHOOK_DRY_RUN=1\n")
        self._write_status({"w1:p1": "working"})
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
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_h1_cwd_present_still_looks_up_name(self):
        self._write_env("SKIP_PANES=noise-pane\n")
        self._write_status({"w1:p1": "working"})
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
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            agent=API,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["path"], "/redirect")
        self.assertIn("POST failed: HTTP 302", proc.stderr)
        self._assert_pane("done", None)
        self._assert_no_secrets(proc)

    def test_posts_snake_case_payload_and_bearer(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
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
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_optional_key_header(self):
        self._write_env("WEBHOOK_KEY_HEADER=X-Automation-Key\n")
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            agent=API,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        headers = self.server.requests[0]["headers"]
        self.assertEqual(headers.get("authorization"), f"Bearer {FAKE_KEY}")
        self.assertEqual(headers.get("x-automation-key"), FAKE_KEY)
        self._assert_no_secrets(proc)

    def test_fallback_pane_name_uses_terminal_title(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
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
        self._assert_pane("idle", None)
        self._assert_no_secrets(proc)

    def test_missing_env_exits_zero(self):
        proc = self._run(_event(), agent={"name": "api"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_no_secrets(proc)

    def test_http_500(self):
        self._write_env(path="/fail")
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            agent=API,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("POST failed: HTTP 500", proc.stderr)
        self._assert_pane("done", None)
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
        self._write_status({"w1:p1": "working"})
        proc = self._run(_event(), herdr_bin="/nonexistent/herdr-bin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self.assertIn("agent get failed for w1:p1", proc.stderr)
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_invalid_json_exits_zero(self):
        self._write_env()
        proc = self._run("{not-json")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("invalid HERDR_PLUGIN_EVENT_JSON", proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_no_secrets(proc)

    def test_herdr_snake_case_event_envelope_posts(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        envelope = json.dumps(
            {
                "event": "pane_agent_status_changed",
                "data": {
                    "type": "pane_agent_status_changed",
                    "pane_id": "w1:p1",
                    "workspace_id": "w1",
                    "agent_status": "done",
                    "agent": "claude",
                },
            }
        )
        proc = self._run(
            envelope,
            agent=API,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        payload = json.loads(self.server.requests[0]["body"])
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["pane_id"], "w1:p1")
        self.assertEqual(payload["agent"], "claude")
        self._assert_no_secrets(proc)

    def test_malformed_ipv6_url_is_invalid_not_traceback(self):
        (self.config / ".env").write_text(
            "WEBHOOK_URL=http://[:::1]/hook\n"
            f"WEBHOOK_KEY={FAKE_KEY}\n",
            encoding="utf-8",
        )
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            agent=API,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("POST failed: invalid URL", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", None)
        self._assert_no_secrets(proc)

    def test_first_seen_done_does_not_post(self):
        self._write_env()
        proc = self._run(_event(), agent={"name": "api"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_seed_list_already_done_event_done_does_not_post(self):
        self._write_env()
        proc = self._run(
            _event(),
            extra_env={
                "HERDR_STUB_LIST_JSON": json.dumps(
                    [{"pane_id": "w1:p1", "agent_status": "done", "name": "api"}]
                )
            },
            agent={"name": "api"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_idle_then_done_does_not_post(self):
        self._write_env()
        self._write_status({"w1:p1": "idle"})
        proc = self._run(
            _event(),
            agent=API,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", None)
        self._assert_no_secrets(proc)

    def test_done_then_done_does_not_post(self):
        self._write_env()
        self._write_status({"w1:p1": "done"})
        proc = self._run(_event(), agent={"name": "api"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_seed_list_working_then_done_posts(self):
        self._write_env()
        proc = self._run(
            _event(),
            extra_env={
                "HERDR_STUB_LIST_JSON": json.dumps(
                    [
                        {
                            "pane_id": "w1:p1",
                            "agent_status": "working",
                            "name": "api",
                        }
                    ]
                )
            },
            agent=API,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        payload = json.loads(self.server.requests[0]["body"])
        self.assertEqual(payload["status"], "done")
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_proxy_env_does_not_divert_localhost(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            extra_env={
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "http_proxy": "http://127.0.0.1:1",
                "https_proxy": "http://127.0.0.1:1",
                "NO_PROXY": "",
                "no_proxy": "",
            },
            agent=API,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self._assert_no_secrets(proc)

    def test_idle_then_done_then_working_then_done_posts_once(self):
        self._write_env()
        self._write_status({"w1:p1": "idle"})
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", None)
        proc = self._run(_event(agent_status="working"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("working", None)
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_done_then_done_one_post_total(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_done_working_done_one_post_total(self):
        self._write_env()
        self._write_status({"w1:p1": {"seen": "done", "posted": "done"}})
        proc = self._run(_event(agent_status="working"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("working", "done")
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_done_idle_done_posts_twice(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._run(_event(agent_status="idle"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_pane("idle", None)
        proc = self._run(_event(agent_status="working"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 2)
        payload = json.loads(self.server.requests[1]["body"])
        self.assertEqual(payload["status"], "done")
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_working_then_blocked_posts(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(agent_status="blocked"),
            agent=API,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        payload = json.loads(self.server.requests[0]["body"])
        self.assertEqual(payload["status"], "blocked")
        self._assert_pane("blocked", "blocked")
        self._assert_no_secrets(proc)

    def test_old_flat_done_event_done_does_not_post(self):
        self._write_env()
        self._write_status({"w1:p1": "done"})
        proc = self._run(_event(), agent={"name": "api"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_idle_during_in_flight_post_does_not_rewind(self):
        self._write_env(path="/idle-during-post")
        self._write_status({"w1:p1": {"seen": "working", "posted": None}})
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self._assert_pane("idle", None)
        self._write_env()
        proc = self._run(_event(agent_status="working"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 2)
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_failed_post_releases_claim_so_next_done_retries(self):
        self._write_env(path="/fail")
        self._write_status({"w1:p1": "working"})
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 1)
        self._assert_pane("done", None)
        self._write_env()
        proc = self._run(_event(agent_status="working"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_pane("working", None)
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([r["path"] for r in self.server.requests], ["/fail", "/hook"])
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_overlapping_done_working_done_posts_once(self):
        self._write_env(path="/slow")
        self._write_status({"w1:p1": "working"})
        first = self._start(_event(), agent=API)
        time.sleep(0.7)
        self._assert_pane("done", "done")
        proc = self._run(_event(agent_status="working"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_pane("working", "done")
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out, err = first.communicate(timeout=15)
        self.assertEqual(first.returncode, 0, err)
        self.assertEqual(len(self.server.requests), 1)
        self._assert_pane("done", "done")
        self.assertNotIn(FAKE_KEY, out + err)

    def test_failed_post_does_not_release_a_newer_claim(self):
        self._write_env(path="/slow-fail")
        self._write_status({"w1:p1": "working"})
        first = self._start(_event(), agent=API)
        second = None
        try:
            time.sleep(0.7)
            self._assert_pane("done", "done")
            proc = self._run(_event(agent_status="idle"), agent=API)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self._assert_pane("idle", None)
            self._write_env()
            proc = self._run(_event(agent_status="working"), agent=API)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            second = self._start(_event(), agent=API)
            deadline = time.time() + 5
            while time.time() < deadline:
                rec = self._read_status().get("w1:p1") or {}
                if rec.get("seen") == "done" and rec.get("posted") == "done":
                    break
                time.sleep(0.05)
            else:
                self.fail("second run did not claim before the first POST finished")
            out1, err1 = first.communicate(timeout=15)
            self.assertEqual(first.returncode, 1, err1)
            self.assertIn("POST failed: HTTP 500", err1)
            self._assert_pane("done", "done")
            out2, err2 = second.communicate(timeout=15)
            self.assertEqual(second.returncode, 0, err2)
            proc = self._run(_event(agent_status="working"), agent=API)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            proc = self._run(_event(), agent=API)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                [r["path"] for r in self.server.requests], ["/slow-fail", "/hook"]
            )
            self._assert_pane("done", "done")
            self.assertNotIn(FAKE_KEY, out1 + err1 + out2 + err2)
        finally:
            for running in (first, second):
                if running is not None and running.poll() is None:
                    running.kill()
                    running.communicate(timeout=5)

    def test_idle_during_in_flight_post_from_idle_is_not_rewound(self):
        self._write_env(path="/slow")
        self._write_status({"w1:p1": "working"})
        first = self._start(_event(), agent=API)
        time.sleep(0.7)
        proc = self._run(_event(agent_status="idle"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out, err = first.communicate(timeout=15)
        self.assertEqual(first.returncode, 0, err)
        self._assert_pane("idle", None)
        proc = self._run(_event(agent_status="working"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 2)
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_unknown_clears_posted(self):
        self._write_env()
        self._write_status({"w1:p1": {"seen": "done", "posted": "done"}})
        proc = self._run(_event(agent_status="unknown"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("unknown", None)
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", None)
        proc = self._run(_event(agent_status="working"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_transient_done_does_not_post(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            extra_env={"SETTLE_MS": "80"},
            agent={**API, "agent_status": "working"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", None)
        self.assertIn("settle unconfirmed", proc.stderr)
        self._assert_no_secrets(proc)

    def test_stable_done_posts_after_settle(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            extra_env={"SETTLE_MS": "80"},
            agent={**API, "agent_status": "done"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        payload = json.loads(self.server.requests[0]["body"])
        self.assertEqual(payload["status"], "done")
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_transient_idle_keeps_posted(self):
        self._write_env()
        self._write_status({"w1:p1": {"seen": "done", "posted": "done"}})
        proc = self._run(
            _event(agent_status="idle"),
            extra_env={"SETTLE_MS": "80"},
            agent={**API, "agent_status": "working"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("idle", "done")
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_stable_idle_clears_posted(self):
        self._write_env()
        self._write_status({"w1:p1": {"seen": "done", "posted": "done"}})
        proc = self._run(
            _event(agent_status="idle"),
            extra_env={"SETTLE_MS": "80"},
            agent={**API, "agent_status": "idle"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("idle", None)
        proc = self._run(_event(agent_status="working"), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_settle_ms_zero_posts_without_live_confirm(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        started = time.monotonic()
        proc = self._run(
            _event(),
            extra_env={"SETTLE_MS": "0"},
            agent={**API, "agent_status": "working"},
        )
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self._assert_pane("done", "done")
        self.assertLess(elapsed, 1.0)
        self.assertNotIn("settle unconfirmed", proc.stderr)
        self._assert_no_secrets(proc)

    def test_mid_window_flip_does_not_post(self):
        # A single end-of-window sample (old 2.5s settle) would see done
        # and POST. Mid-window working must abort.
        self._write_env()
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            extra_env={
                "SETTLE_MS": "80",
                **self._seq_env(["done", "done", "working", "done"]),
            },
            agent={**API, "agent_status": "done"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", None)
        self.assertIn("settle unconfirmed", proc.stderr)
        self._assert_no_secrets(proc)

    def test_thinking_title_blocks_post(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            agent={
                **API,
                "agent_status": "done",
                "terminal_title_stripped": "Thinking",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", None)
        self.assertIn("settle unconfirmed", proc.stderr)
        self._assert_no_secrets(proc)

    def test_title_field_thinking_blocks_post(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            agent={**API, "agent_status": "done", "title": "Thinking"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", None)
        self._assert_no_secrets(proc)

    def test_mid_run_title_markers_block_post(self):
        self._write_env()
        for title in (
            "Responding",
            "Waiting for response",
            "Running",
            "Tool call",
        ):
            with self.subTest(title=title):
                self.server.requests.clear()
                self._write_status({"w1:p1": "working"})
                proc = self._run(
                    _event(),
                    agent={
                        **API,
                        "agent_status": "done",
                        "terminal_title_stripped": title,
                    },
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(self.server.requests, [])
                self._assert_pane("done", None)

    def test_working_then_done_sustained_posts(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        proc = self._run(
            _event(),
            extra_env={"SETTLE_MS": "80"},
            agent={**API, "agent_status": "done"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self._assert_pane("done", "done")
        self._assert_no_secrets(proc)

    def test_idle_mid_window_flicker_keeps_posted(self):
        self._write_env()
        self._write_status({"w1:p1": {"seen": "done", "posted": "done"}})
        proc = self._run(
            _event(agent_status="idle"),
            extra_env={
                "SETTLE_MS": "80",
                **self._seq_env(["idle", "idle", "working", "idle"]),
            },
            agent={**API, "agent_status": "idle"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("idle", "done")
        self._assert_no_secrets(proc)

    def test_unknown_then_done_does_not_post(self):
        self._write_env()
        self._write_status({"w1:p1": {"seen": "unknown", "posted": None}})
        proc = self._run(_event(), agent=API)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.server.requests, [])
        self._assert_pane("done", None)
        self._assert_no_secrets(proc)

    def test_settle_ms_zero_fast_path_does_not_wait(self):
        self._write_env()
        self._write_status({"w1:p1": "working"})
        started = time.monotonic()
        proc = self._run(
            _event(),
            extra_env={"SETTLE_MS": "0"},
            agent={**API, "agent_status": "done"},
        )
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self.server.requests), 1)
        self._assert_pane("done", "done")
        self.assertLess(elapsed, 1.0)
        self._assert_no_secrets(proc)


if __name__ == "__main__":
    unittest.main()
