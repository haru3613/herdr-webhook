#!/usr/bin/env python3
"""POST when a Herdr pane agent hits a configured status."""

from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from urllib.parse import urlparse

EVENT_NAME = "pane.agent_status_changed"
# JSON envelope uses snake_case EventKind; the env var uses the dotted name.
EVENT_JSON_NAMES = frozenset({EVENT_NAME, "pane_agent_status_changed"})
KNOWN_STATUSES = frozenset({"idle", "working", "blocked", "done", "unknown"})
CLEAR_POSTED_STATUSES = frozenset({"idle", "unknown"})
DEFAULT_STATUSES = ("done", "blocked")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")
STATUS_FILENAME = "last-status.json"


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


def _as_status(value) -> str:
    status = _text(value).casefold()
    return status if status in KNOWN_STATUSES else ""


def _pane_state(
    seen: str, posted: str | None, claim: int = 0
) -> dict[str, str | int | None]:
    state: dict[str, str | int | None] = {"seen": seen, "posted": posted}
    if claim:
        state["claim"] = claim
    return state


def _claim_n(record: dict | None) -> int:
    if not record:
        return 0
    n = record.get("claim")
    return n if type(n) is int and n > 0 else 0


def _posted_for_seen(status: str, notify: set[str]) -> str | None:
    if status in CLEAR_POSTED_STATUSES:
        return None
    if status in notify:
        return status
    return None


def _coerce_pane_state(
    value, notify: set[str]
) -> dict[str, str | int | None] | None:
    if isinstance(value, str):
        status = _as_status(value)
        if not status:
            return None
        return _pane_state(status, _posted_for_seen(status, notify))
    if not isinstance(value, dict):
        return None
    seen = _as_status(value.get("seen"))
    if not seen:
        return None
    posted = _as_status(value.get("posted")) or None
    return _pane_state(seen, posted, _claim_n(value))


def _normalize_status_store(
    raw, notify: set[str]
) -> dict[str, dict[str, str | int | None]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str | int | None]] = {}
    for key, value in raw.items():
        pane_id = _text(key)
        if not pane_id:
            continue
        state = _coerce_pane_state(value, notify)
        if state is not None:
            out[pane_id] = state
    return out


def _agent_list_statuses() -> dict[str, str]:
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        proc = subprocess.run(
            [herdr, "agent", "list"],
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
    agents = None
    result = body.get("result")
    if isinstance(result, dict):
        maybe = result.get("agents")
        if isinstance(maybe, list):
            agents = maybe
    elif isinstance(result, list):
        agents = result
    if agents is None:
        maybe = body.get("agents")
        if isinstance(maybe, list):
            agents = maybe
    if not isinstance(agents, list):
        return {}
    out: dict[str, str] = {}
    for item in agents:
        if not isinstance(item, dict):
            continue
        pane_id = _text(item.get("pane_id"))
        status = _text(item.get("agent_status")).casefold()
        if pane_id and status in KNOWN_STATUSES:
            out[pane_id] = status
    return out


def _atomic_write_json(path: str, data: dict) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".last-status.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _status_lock(path: str):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _persist_store(path: str, store: dict) -> None:
    try:
        _atomic_write_json(path, store)
    except OSError:
        pass


def _load_or_seed_statuses(
    path: str, notify: set[str]
) -> dict[str, dict[str, str | int | None]]:
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            raw = None
        else:
            if isinstance(raw, dict):
                return _normalize_status_store(raw, notify)
    seeded: dict[str, dict[str, str | int | None]] = {}
    for pane_id, status in _agent_list_statuses().items():
        seeded[pane_id] = _pane_state(status, _posted_for_seen(status, notify))
    _persist_store(path, seeded)
    return dict(seeded)


def _should_post(
    record: dict[str, str | int | None] | None,
    status: str,
    notify: set[str],
) -> bool:
    if record is None:
        return False
    return status in notify and record.get("seen") != status and record.get("posted") != status


def _next_pane_state(
    record: dict[str, str | int | None] | None,
    status: str,
    notify: set[str],
    *,
    posted_now: bool,
) -> dict[str, str | int | None]:
    posted = record.get("posted") if record else None
    claim = _claim_n(record)
    if status in CLEAR_POSTED_STATUSES:
        new_posted = None
    elif posted_now:
        new_posted = status
        claim += 1
    elif record is None:
        new_posted = _posted_for_seen(status, notify)
    else:
        new_posted = posted
    return _pane_state(status, new_posted, claim)


def _release_claim(
    path: str, pane_id: str, status: str, notify: set[str], claim: int
) -> None:
    """Undo this run's posted claim after a failed POST so a later transition can retry."""
    with _status_lock(path):
        store = _load_or_seed_statuses(path, notify)
        current = store.get(pane_id)
        if current is None or current.get("posted") != status:
            return
        if _claim_n(current) != claim:
            return
        store[pane_id] = _pane_state(current["seen"], None, claim)
        _persist_store(path, store)


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
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except ValueError:
        return "POST failed: invalid URL"
    if parsed.scheme not in {"http", "https"}:
        return "POST failed: URL must be http or https"
    if not host:
        return "POST failed: invalid URL"
    if parsed.scheme == "http" and not _is_loopback(host):
        return "POST failed: http is only allowed for loopback"
    return ""


def _post(url: str, body: bytes, headers: dict[str, str]) -> int:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
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
    if isinstance(json_event, str) and json_event and json_event not in EVENT_JSON_NAMES:
        return 0

    nested = raw.get("data")
    data = nested if isinstance(nested, dict) else {}

    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or ""
    if not config_dir:
        return 0

    cfg: dict[str, str] = {}
    env_path = os.path.join(config_dir, ".env")
    if os.path.isfile(env_path):
        try:
            cfg = _load_env(env_path)
        except OSError:
            cfg = {}

    status = _field(data, raw, "agent_status").casefold()
    pane_id = _field(data, raw, "pane_id")
    if not pane_id or status not in KNOWN_STATUSES:
        return 0

    status_path = os.path.join(config_dir, STATUS_FILENAME)
    notify = _notify_statuses(cfg.get("NOTIFY_STATUSES") or "")
    webhook_url = cfg.get("WEBHOOK_URL", "").strip()
    webhook_key = cfg.get("WEBHOOK_KEY", "").strip()
    extra_header = cfg.get("WEBHOOK_KEY_HEADER", "").strip()
    dry_run = _truthy(os.environ.get("WEBHOOK_DRY_RUN") or cfg.get("WEBHOOK_DRY_RUN") or "")
    configured = dry_run or bool(webhook_url and webhook_key)

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
                f"agent get failed for {pane_id}; using event fields only",
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
    skipped = _skip_pane(pane_name, _csv(cfg.get("SKIP_PANES") or ""))

    claimed = 0
    with _status_lock(status_path):
        store = _load_or_seed_statuses(status_path, notify)
        record = store.get(pane_id)
        if skipped:
            # Skipped panes track seen only. Never claim posted for them.
            store[pane_id] = _pane_state(status, None, _claim_n(record))
            _persist_store(status_path, store)
            return 0
        if not configured or not _should_post(record, status, notify):
            store[pane_id] = _next_pane_state(record, status, notify, posted_now=False)
            _persist_store(status_path, store)
            return 0
        # Claim posted before HTTP so an overlapping run stays quiet. Success
        # writes nothing; failure releases only this run's claim.
        nxt = _next_pane_state(record, status, notify, posted_now=True)
        store[pane_id] = nxt
        _persist_store(status_path, store)
        claimed = _claim_n(nxt)

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
        _release_claim(status_path, pane_id, status, notify, claimed)
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
            _release_claim(status_path, pane_id, status, notify, claimed)
            return 1
    result = _post(webhook_url, body, headers)
    if result != 0:
        _release_claim(status_path, pane_id, status, notify, claimed)
    return result


if __name__ == "__main__":
    sys.exit(main())
