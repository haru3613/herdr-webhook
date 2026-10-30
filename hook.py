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
import time
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
PRIOR_NOTIFY_SEEN = frozenset({"working", "blocked"})
DEFAULT_STATUSES = ("done", "blocked")
# Stopgap until Herdr ships a job-level settle / session-end / run-finished
# signal. Today only pane.agent_status_changed exists; screen-manifest agents
# (incl Grok) emit turn-level done, so a turn gap looks like a finished job.
# 7b096c0 (2x10s) missed zed-llm-audit: ~22s quiet done, clean title, both
# rounds passed. Window is 3x20s. Optional later: per-job artifact gate.
# Do not invent a sidecar EXIT protocol.
DEFAULT_SETTLE_MS = 20000
DEFAULT_SETTLE_ROUNDS = 3
DEFAULT_MIN_WORKING_MS = 30000
DEFAULT_RETRACT_ON_RESUME = True
SETTLE_SAMPLES = 3
RESUME_STATUSES = frozenset({"working", "blocked"})
MID_RUN_TITLE_MARKERS = (
    "thinking",
    "responding",
    "waiting for response",
    "running",
    "writing edit",
    "tool",
    "summarized",
)
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")
STATUS_FILENAME = "last-status.json"
PLUGIN_ID = "jefflau.herdr-webhook"
STOP_MESSAGE_LIMIT = 4000


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


def _settle_ms(raw: str) -> int:
    text = raw.strip()
    if not text:
        return DEFAULT_SETTLE_MS
    try:
        value = int(text)
    except ValueError:
        return DEFAULT_SETTLE_MS
    if value < 0:
        return DEFAULT_SETTLE_MS
    return value


def _settle_rounds(raw: str) -> int:
    text = raw.strip()
    if not text:
        return DEFAULT_SETTLE_ROUNDS
    try:
        value = int(text)
    except ValueError:
        return DEFAULT_SETTLE_ROUNDS
    if value < 1:
        return DEFAULT_SETTLE_ROUNDS
    return value


def _min_working_ms(raw: str) -> int:
    text = raw.strip()
    if not text:
        return DEFAULT_MIN_WORKING_MS
    try:
        value = int(text)
    except ValueError:
        return DEFAULT_MIN_WORKING_MS
    if value < 0:
        return DEFAULT_MIN_WORKING_MS
    return value


def _retract_on_resume(raw: str) -> bool:
    text = raw.strip()
    if not text:
        return DEFAULT_RETRACT_ON_RESUME
    return _truthy(text)


def _live_agent_status(info: dict) -> str:
    return _as_status(info.get("agent_status"))


def _agent_title(info: dict) -> str:
    return (
        _text(info.get("terminal_title_stripped")) or _text(info.get("title"))
    ).casefold()


def _title_looks_mid_run(info: dict) -> bool:
    title = _agent_title(info)
    if not title:
        return False
    return any(marker in title for marker in MID_RUN_TITLE_MARKERS)


def _sample_matches(info: dict, status: str, check_title: bool) -> bool:
    if not info:
        return False
    live = _live_agent_status(info)
    # Viewing a done pane turns it idle; the turn is still finished.
    if live != status and not (status == "done" and live == "idle"):
        return False
    if check_title and _title_looks_mid_run(info):
        return False
    return True


def _status_settled(
    pane_id: str, status: str, settle_ms: int, *, check_title: bool = False
) -> bool:
    # Confirm live herdr agent get still matches. 7b096c0's 2x10s window
    # missed zed-llm-audit (~22s quiet done). Drop this wait when Herdr
    # adds a real job-level settle; do not invent a sidecar EXIT protocol.
    # SETTLE_MS=0: one immediate pass, no extra samples (fast tests).
    if settle_ms <= 0:
        return True
    interval = (settle_ms / 1000.0) / (SETTLE_SAMPLES - 1)
    for i in range(SETTLE_SAMPLES):
        if i:
            time.sleep(interval)
        info = _agent_get(pane_id)
        if not _sample_matches(info, status, check_title):
            return False
    return True


def _should_clear_posted(
    record: dict[str, str | int | None] | None, status: str
) -> bool:
    if status not in CLEAR_POSTED_STATUSES or record is None:
        return False
    return bool(record.get("posted"))


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
    seen: str,
    posted: str | None,
    claim: int = 0,
    working_since: int | None = None,
) -> dict[str, str | int | None]:
    state: dict[str, str | int | None] = {"seen": seen, "posted": posted}
    if claim:
        state["claim"] = claim
    if working_since:
        state["working_since"] = working_since
    return state


def _claim_n(record: dict | None) -> int:
    if not record:
        return 0
    n = record.get("claim")
    return n if type(n) is int and n > 0 else 0


def _working_since_n(record: dict | None) -> int | None:
    if not record:
        return None
    n = record.get("working_since")
    return n if type(n) is int and n > 0 else None


def _next_working_since(record: dict | None, status: str) -> int | None:
    # Stamp unix seconds when seen becomes working. Keep across idle/blocked
    # and unconfirmed done. Clear on unknown. Done POST clears after success.
    if status == "unknown":
        return None
    existing = _working_since_n(record)
    if status == "working":
        return existing or int(time.time())
    return existing


def _min_working_ok(record: dict | None, min_working_ms: int) -> bool:
    # SETTLE_MS=0 tests set MIN_WORKING_MS=0 to skip. Otherwise refuse
    # done/blocked unless working_since is set and aged past the floor.
    if min_working_ms <= 0:
        return True
    ws = _working_since_n(record)
    if not ws:
        return False
    return (int(time.time()) - ws) * 1000 >= min_working_ms


def _abort_pane_state(
    record: dict[str, str | int | None] | None, status: str
) -> dict[str, str | int | None]:
    posted = record.get("posted") if record else None
    return _pane_state(status, posted, _claim_n(record), _working_since_n(record))


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
    return _pane_state(seen, posted, _claim_n(value), _working_since_n(value))


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
        seeded[pane_id] = _pane_state(
            status,
            _posted_for_seen(status, notify),
            working_since=_next_working_since(None, status),
        )
    _persist_store(path, seeded)
    return dict(seeded)


def _should_post(
    record: dict[str, str | int | None] | None,
    status: str,
    notify: set[str],
) -> bool:
    if record is None:
        return False
    # First-seed and unknown/idle → done must not fire. A notify POST
    # needs a prior seen of working or blocked.
    if record.get("seen") not in PRIOR_NOTIFY_SEEN:
        return False
    return status in notify and record.get("seen") != status and record.get("posted") != status


def _should_retract(
    record: dict[str, str | int | None] | None,
    status: str,
    notify: set[str],
    enabled: bool,
) -> bool:
    # working/blocked after a posted notify means the prior done (or
    # blocked) was a turn gap, not job end. POST retract and drop posted
    # so a later real done can fire. RETRACT_ON_RESUME=0 skips this.
    if not enabled or record is None:
        return False
    if status not in RESUME_STATUSES:
        return False
    posted = record.get("posted")
    if not isinstance(posted, str) or not posted:
        return False
    if posted == status:
        return False
    if posted not in notify and posted != "done":
        return False
    return True


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
    return _pane_state(
        status, new_posted, claim, _next_working_since(record, status)
    )


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
        store[pane_id] = _pane_state(
            current["seen"], None, claim, _working_since_n(current)
        )
        _persist_store(path, store)


def _clear_working_since_if_claim(
    path: str, pane_id: str, status: str, notify: set[str], claim: int
) -> None:
    """Drop working_since after a successful done POST if this run still owns the latch."""
    if status != "done" or claim <= 0:
        return
    with _status_lock(path):
        store = _load_or_seed_statuses(path, notify)
        current = store.get(pane_id)
        if current is None:
            return
        if current.get("seen") != status or current.get("posted") != status:
            return
        if _claim_n(current) != claim:
            return
        if _working_since_n(current) is None:
            return
        store[pane_id] = _pane_state(status, status, claim, working_since=None)
        _persist_store(path, store)


def _restore_posted_after_failed_retract(
    path: str,
    pane_id: str,
    notify: set[str],
    prior_status: str,
    seen: str,
) -> None:
    """Put posted back if this retract POST failed and no one else claimed."""
    if not prior_status:
        return
    with _status_lock(path):
        store = _load_or_seed_statuses(path, notify)
        current = store.get(pane_id)
        if current is None:
            return
        if current.get("posted") is not None:
            return
        if current.get("seen") != seen:
            return
        store[pane_id] = _pane_state(
            current["seen"],
            prior_status,
            _claim_n(current),
            _working_since_n(current),
        )
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
    settle_ms = _settle_ms(os.environ.get("SETTLE_MS") or cfg.get("SETTLE_MS") or "")
    settle_rounds = _settle_rounds(
        os.environ.get("SETTLE_ROUNDS") or cfg.get("SETTLE_ROUNDS") or ""
    )
    min_working_ms = _min_working_ms(
        os.environ.get("MIN_WORKING_MS") or cfg.get("MIN_WORKING_MS") or ""
    )
    retract_enabled = _retract_on_resume(
        os.environ.get("RETRACT_ON_RESUME") or cfg.get("RETRACT_ON_RESUME") or ""
    )

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
    pending_http = False
    pending_retract = False
    prior_posted = ""
    needs_confirm = False
    with _status_lock(status_path):
        store = _load_or_seed_statuses(status_path, notify)
        record = store.get(pane_id)
        if status == "idle" and "done" in notify and record and record.get("seen") == "working":
            # Herdr reports a turn that ends in an already-seen (focused) pane
            # as idle, not done. Straight from working, that is still job end.
            status = "done"
        if skipped:
            # Skipped panes track seen only. Never claim posted for them.
            store[pane_id] = _pane_state(
                status, None, _claim_n(record), _next_working_since(record, status)
            )
            _persist_store(status_path, store)
            return 0
        should_retract = configured and _should_retract(
            record, status, notify, retract_enabled
        )
        should_post = (
            (not should_retract) and configured and _should_post(record, status, notify)
        )
        should_clear = (not should_retract) and _should_clear_posted(record, status)
        if should_post and not _min_working_ok(record, min_working_ms):
            store[pane_id] = _abort_pane_state(record, status)
            _persist_store(status_path, store)
            print(f"min working unconfirmed for {pane_id}", file=sys.stderr)
            return 0
        if should_post and _title_looks_mid_run(info):
            store[pane_id] = _abort_pane_state(record, status)
            _persist_store(status_path, store)
            print(f"settle unconfirmed for {pane_id}", file=sys.stderr)
            return 0
        needs_confirm = settle_ms > 0 and (should_post or should_clear)
        if not needs_confirm:
            if should_post:
                # Claim posted before HTTP so an overlapping run stays quiet.
                # Success only drops working_since if this claim still owns
                # done; failure releases only this run's claim.
                nxt = _next_pane_state(record, status, notify, posted_now=True)
                store[pane_id] = nxt
                _persist_store(status_path, store)
                claimed = _claim_n(nxt)
                pending_http = True
            elif should_retract:
                # Clear posted before the retract POST so overlapping
                # working events do not double-fire. Restore posted if
                # this POST fails. Success leaves posted empty.
                posted = record.get("posted") if record else None
                prior_posted = posted if isinstance(posted, str) else ""
                # The retracted job already ran past the floor; a short
                # resumed turn must still post the done that closes it.
                store[pane_id] = _pane_state(
                    status,
                    None,
                    _claim_n(record),
                    int(time.time()) - min_working_ms // 1000 - 1,
                )
                _persist_store(status_path, store)
                pending_http = True
                pending_retract = True
            else:
                store[pane_id] = _next_pane_state(
                    record, status, notify, posted_now=False
                )
                _persist_store(status_path, store)
                return 0

    if needs_confirm:
        # Notify POSTs need SETTLE_ROUNDS consecutive windows (3x20s after
        # 7b096c0 missed a ~22s quiet done). Idle/unknown latch-clear stays
        # one window. SETTLE_MS=0 never reaches here.
        rounds = settle_rounds if should_post else 1
        confirmed = True
        for _ in range(rounds):
            if not _status_settled(
                pane_id, status, settle_ms, check_title=should_post
            ):
                confirmed = False
                break
        if not confirmed:
            print(f"settle unconfirmed for {pane_id}", file=sys.stderr)
            with _status_lock(status_path):
                store = _load_or_seed_statuses(status_path, notify)
                record = store.get(pane_id)
                store[pane_id] = _abort_pane_state(record, status)
                _persist_store(status_path, store)
            return 0
        with _status_lock(status_path):
            store = _load_or_seed_statuses(status_path, notify)
            record = store.get(pane_id)
            if (
                configured
                and _should_post(record, status, notify)
                and _min_working_ok(record, min_working_ms)
            ):
                nxt = _next_pane_state(record, status, notify, posted_now=True)
                store[pane_id] = nxt
                _persist_store(status_path, store)
                claimed = _claim_n(nxt)
                pending_http = True
            else:
                store[pane_id] = _next_pane_state(
                    record, status, notify, posted_now=False
                )
                _persist_store(status_path, store)
                return 0

    if not pending_http:
        return 0
    if pending_retract:
        print(f"claimed retract POST for {pane_id}", file=sys.stderr)
    else:
        print(f"claimed {status} POST for {pane_id}", file=sys.stderr)

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
    if pending_retract:
        payload["retracted"] = True
        payload["prior_status"] = prior_posted
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if dry_run:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        if not pending_retract:
            _clear_working_since_if_claim(status_path, pane_id, status, notify, claimed)
        return 0

    url_error = _check_url(webhook_url)
    if url_error:
        print(url_error, file=sys.stderr)
        if pending_retract:
            _restore_posted_after_failed_retract(
                status_path, pane_id, notify, prior_posted, status
            )
        else:
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
            if pending_retract:
                _restore_posted_after_failed_retract(
                    status_path, pane_id, notify, prior_posted, status
                )
            else:
                _release_claim(status_path, pane_id, status, notify, claimed)
            return 1
    result = _post(webhook_url, body, headers)
    if result != 0:
        if pending_retract:
            _restore_posted_after_failed_retract(
                status_path, pane_id, notify, prior_posted, status
            )
        else:
            _release_claim(status_path, pane_id, status, notify, claimed)
    elif not pending_retract:
        _clear_working_since_if_claim(status_path, pane_id, status, notify, claimed)
    return result


def _stop_config_dir() -> str:
    got = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or ""
    if got:
        return got
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        proc = subprocess.run(
            [herdr, "plugin", "config-dir", PLUGIN_ID],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def stop_main(agent: str) -> int:
    """Claude Code / Codex Stop hook: POST done now, no settle.

    The agent itself says its turn ended, so there is nothing to guess. Latch
    posted=done only after a successful POST: the settled plugin run for this
    turn (~60s later) then stays quiet and a later working event still
    retracts, while a failed POST leaves the latch untouched for the plugin
    to deliver. Claiming before the POST would let a failure swallow the
    plugin run. Always exit 0: stdout and the exit code belong to the agent's
    hook protocol, never block its stop.
    """
    pane_id = _text(os.environ.get("HERDR_PANE_ID"))
    if os.environ.get("HERDR_ENV") != "1" or not pane_id:
        return 0
    try:
        hook_input = json.load(sys.stdin)
    except (OSError, ValueError):
        hook_input = {}
    if not isinstance(hook_input, dict):
        hook_input = {}
    if hook_input.get("stop_hook_active") is True:
        # Claude is continuing because a Stop hook blocked; the turn is not over.
        return 0

    config_dir = _stop_config_dir()
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
    if not dry_run and not (webhook_url and webhook_key):
        return 0
    notify = _notify_statuses(cfg.get("NOTIFY_STATUSES") or "")
    if "done" not in notify:
        return 0

    info = _agent_get(pane_id)
    name = _text(info.get("name"))
    display_agent = _text(info.get("display_agent"))
    pane_name = name or _text(info.get("terminal_title_stripped")) or display_agent
    if _skip_pane(pane_name, _csv(cfg.get("SKIP_PANES") or "")):
        return 0

    message = _text(hook_input.get("last_assistant_message"))
    payload = {
        "event": EVENT_NAME,
        "status": "done",
        "pane_id": pane_id,
        "pane_name": pane_name,
        "workspace_id": _text(os.environ.get("HERDR_WORKSPACE_ID")) or _text(info.get("workspace_id")),
        "workspace_label": "",
        "cwd": _text(hook_input.get("cwd")) or _text(info.get("cwd")),
        "agent": agent or _text(info.get("agent")),
        "display_agent": display_agent or None,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "stop_hook",
        "session_id": _text(hook_input.get("session_id")),
        "last_assistant_message": message[:STOP_MESSAGE_LIMIT],
    }
    if len(message) > STOP_MESSAGE_LIMIT:
        payload["last_assistant_message_truncated"] = True
    body = json.dumps(payload, separators=(",", ":"))
    if dry_run:
        print(body, file=sys.stderr)
    elif not _stop_post(webhook_url, webhook_key, extra_header, body):
        return 0
    print(f"posted done for {pane_id} (stop hook)", file=sys.stderr)
    status_path = os.path.join(config_dir, STATUS_FILENAME)
    with _status_lock(status_path):
        store = _load_or_seed_statuses(status_path, notify)
        store[pane_id] = _pane_state("done", "done", _claim_n(store.get(pane_id)) + 1)
        _persist_store(status_path, store)
    return 0


def _stop_post(webhook_url: str, webhook_key: str, extra_header: str, body: str) -> bool:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {webhook_key}",
    }
    error = _check_url(webhook_url)
    if not error and extra_header:
        if HEADER_NAME.fullmatch(extra_header):
            headers[extra_header] = webhook_key
        else:
            error = "invalid WEBHOOK_KEY_HEADER"
    if error:
        print(error, file=sys.stderr)
        return False
    return _post(webhook_url, body.encode("utf-8"), headers) == 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["stop"]:
        try:
            stop_main(sys.argv[2] if len(sys.argv) > 2 else "")
        except Exception as err:
            # Bad stdin or .env must not surface as a failed agent hook.
            print(f"stop hook failed: {type(err).__name__}", file=sys.stderr)
        sys.exit(0)
    sys.exit(main())
