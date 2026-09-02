#!/usr/bin/env python3
"""POST when a Herdr pane agent hits a configured status."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from urllib.parse import urlparse

EVENT_NAME = "pane.agent_status_changed"
KNOWN_STATUSES = frozenset({"idle", "working", "blocked", "done", "unknown"})
DEFAULT_STATUSES = ("done", "blocked")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _field(data: dict, raw: dict, *keys: str) -> str:
    for key in keys:
        for src in (data, raw):
            got = _text(src.get(key))
            if got:
                return got
    return ""


def _truthy(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _load_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if name:
                out[name] = value
    return out


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _notify_statuses(raw: str) -> set[str]:
    if not raw.strip():
        return set(DEFAULT_STATUSES)
    picked = {part.casefold() for part in _csv(raw) if part.casefold() in KNOWN_STATUSES}
    return picked or set(DEFAULT_STATUSES)


def _agent_get(pane_id: str) -> dict:
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        proc = subprocess.run(
            [herdr, "agent", "get", pane_id],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(body, dict):
        return {}
    result = body.get("result")
    if isinstance(result, dict):
        agent = result.get("agent")
        if isinstance(agent, dict):
            return agent
        return result
    agent = body.get("agent")
    if isinstance(agent, dict):
        return agent
    return {}


def _context() -> dict:
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _workspace_label(ctx: dict) -> str:
    got = _text(ctx.get("workspace_label"))
    if got:
        return got
    workspace = ctx.get("workspace")
    if isinstance(workspace, dict):
        return _text(workspace.get("label")) or _text(workspace.get("workspace_label"))
    return ""


def _skip_pane(name: str, patterns: list[str]) -> bool:
    folded = name.casefold()
    for pattern in patterns:
        if fnmatchcase(folded, pattern.casefold()):
            return True
    return False


def _is_loopback(host: str) -> bool:
    folded = host.casefold().rstrip(".")
    if folded == "localhost":
        return True
    try:
        return ipaddress.ip_address(folded).is_loopback
    except ValueError:
        return False


def _check_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "POST failed: URL must be http or https"
    host = parsed.hostname or ""
    if not host:
        return "POST failed: invalid URL"
    if parsed.scheme == "http" and not _is_loopback(host):
        return "POST failed: http is only allowed for loopback"
    return ""


def _post(url: str, body: bytes, headers: dict[str, str]) -> int:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=8) as resp:
            resp.read()
    except urllib.error.HTTPError as err:
        print(f"POST failed: HTTP {err.code}", file=sys.stderr)
        return 1
    except Exception as err:
        print(f"POST failed: {type(err).__name__}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    env_event = os.environ.get("HERDR_PLUGIN_EVENT")
    if env_event and env_event != EVENT_NAME:
        return 0

    raw_json = os.environ.get("HERDR_PLUGIN_EVENT_JSON") or ""
    if not raw_json.strip():
        return 0
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError:
        print("invalid HERDR_PLUGIN_EVENT_JSON", file=sys.stderr)
        return 0
    if not isinstance(raw, dict):
        return 0
    json_event = raw.get("event")
    if isinstance(json_event, str) and json_event and json_event != EVENT_NAME:
        return 0

    nested = raw.get("data")
    data = nested if isinstance(nested, dict) else {}

    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or ""
    env_path = os.path.join(config_dir, ".env") if config_dir else ""
    if not env_path or not os.path.isfile(env_path):
        return 0
    try:
        cfg = _load_env(env_path)
    except OSError:
        return 0

    webhook_url = cfg.get("WEBHOOK_URL", "").strip()
    webhook_key = cfg.get("WEBHOOK_KEY", "").strip()
    extra_header = cfg.get("WEBHOOK_KEY_HEADER", "").strip()
    dry_run = _truthy(os.environ.get("WEBHOOK_DRY_RUN") or cfg.get("WEBHOOK_DRY_RUN") or "")
    if not dry_run and (not webhook_url or not webhook_key):
        return 0

    status = _field(data, raw, "agent_status").casefold()
    if status not in _notify_statuses(cfg.get("NOTIFY_STATUSES") or ""):
        return 0

    pane_id = _field(data, raw, "pane_id")
    agent = _field(data, raw, "agent")
    display_agent = _field(data, raw, "display_agent")
    name = _field(data, raw, "name")
    cwd = _field(data, raw, "cwd")
    workspace_id = _field(data, raw, "workspace_id") or _text(os.environ.get("HERDR_WORKSPACE_ID"))
    terminal_title_stripped = _field(data, raw, "terminal_title_stripped")

    info: dict = {}
    if pane_id:
        info = _agent_get(pane_id)
        if not info:
            print(
                f"agent get failed for {pane_id}; sending partial payload",
                file=sys.stderr,
            )
        else:
            cwd = cwd or _text(info.get("cwd"))
            name = name or _text(info.get("name"))
            display_agent = display_agent or _text(info.get("display_agent"))
            agent = agent or _text(info.get("agent"))
            terminal_title_stripped = terminal_title_stripped or _text(
                info.get("terminal_title_stripped")
            )
            workspace_id = workspace_id or _text(info.get("workspace_id"))

    pane_name = name or terminal_title_stripped or display_agent
    if _skip_pane(pane_name, _csv(cfg.get("SKIP_PANES") or "")):
        return 0

    payload = {
        "event": EVENT_NAME,
        "status": status,
        "pane_id": pane_id,
        "pane_name": pane_name,
        "workspace_id": workspace_id,
        "workspace_label": _workspace_label(_context()),
        "cwd": cwd,
        "agent": agent,
        "display_agent": display_agent or None,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if dry_run:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        return 0

    url_error = _check_url(webhook_url)
    if url_error:
        print(url_error, file=sys.stderr)
        return 1

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {webhook_key}",
    }
    if extra_header:
        if HEADER_NAME.fullmatch(extra_header):
            headers[extra_header] = webhook_key
        else:
            print("invalid WEBHOOK_KEY_HEADER", file=sys.stderr)
            return 1
    return _post(webhook_url, body, headers)


if __name__ == "__main__":
    sys.exit(main())
