# Herdr Webhook

Herdr already knows when a pane is done or blocked. This plugin POSTs that change as JSON to a URL you own.

POSTs when a pane changes into a notify status (done or blocked by default) that has not already been posted for that pane. last-status.json stores `seen`, `posted`, and `working_since` per pane. When the pane goes working or blocked after a notify was posted, RETRACT_ON_RESUME (default 1) POSTs a retract payload and clears `posted`, so a later done can fire. If RETRACT_ON_RESUME=0, working only updates `seen` and keeps `posted`, so done then working then done is one POST. A done or blocked POST requires a prior `seen` of working or blocked. First sighting of a pane is stored and skipped, and unknown or idle into done does not POST. `working_since` is stamped when seen becomes working and is required for a done or blocked POST unless MIN_WORKING_MS=0. The POST is refused until that stamp is at least MIN_WORKING_MS old (default 30000). It is cleared after a successful done POST and on unknown. Before a done or blocked POST, the hook samples live `herdr agent get` at least 3 times across SETTLE_MS (default 20000), and repeats that window SETTLE_ROUNDS times (default 3) back-to-back. Every sample must still equal the event status. One flip in any round aborts, records `seen`, and does not POST. If `terminal_title_stripped` or `title` from that get still looks mid-run (thinking, responding, waiting for response, running, writing edit, tool, summarized), treat it as unsettled and do not POST. idle or unknown clears `posted` only after the same multi-sample confirm still reads idle or unknown, so a brief idle flicker does not reopen the latch. SETTLE_MS=0 skips the wait and extra samples (one immediate check total, for tests) and can be paired with MIN_WORKING_MS=0. If the file is missing, the hook seeds it from `herdr agent list` so leftover events for panes that are already done or blocked stay quiet. An older file that stored only the last status is migrated. `posted` is claimed before the POST goes out, so overlapping hook runs for the same pane cannot both fire. If the POST fails, that run's claim is cleared and the next transition into that status retries. A newer overlapping claim is left alone.

Requires Herdr 0.7.5 or newer and `python3` on linux or macOS.

## Install

```sh
herdr plugin install jefflau/herdr-webhook
```

GitHub installs replace the plugin checkout. The config directory `.env` stays in place, so reinstall does not wipe secrets.

Or clone the repo and link it:

```sh
herdr plugin link /path/to/herdr-webhook
```

## Config

Put secrets in the plugin config directory. GitHub installs replace the checkout, so a `.env` in the repo would vanish or get published.

```sh
CONFIG_DIR="$(herdr plugin config-dir jefflau.herdr-webhook)"
cat > "$CONFIG_DIR/.env" <<'EOF'
WEBHOOK_URL=
WEBHOOK_KEY=
WEBHOOK_KEY_HEADER=
NOTIFY_STATUSES=done,blocked
SKIP_PANES=
WEBHOOK_DRY_RUN=0
SETTLE_MS=20000
SETTLE_ROUNDS=3
MIN_WORKING_MS=30000
RETRACT_ON_RESUME=1
EOF
```

That writes the file even if your shell is not in the plugin checkout. If you cloned the repo, `cp .env.example "$CONFIG_DIR/.env"` works from the clone. Then edit `$CONFIG_DIR/.env`.

**WEBHOOK_URL.** The POST target.

**WEBHOOK_KEY.** Sent as `Authorization: Bearer <key>`.

**WEBHOOK_KEY_HEADER.** Optional. If set, the raw key is also sent under that header. Grok Bot webhook routines need `WEBHOOK_KEY_HEADER=X-Automation-Key`.

**NOTIFY_STATUSES.** Defaults to `done,blocked`. Allowed values are `idle`, `working`, `blocked`, `done`, `unknown`.

**SKIP_PANES.** Comma-separated case-insensitive globs matched against the pane name. Empty means do not skip.

**WEBHOOK_DRY_RUN.** `1` prints the JSON to stdout and skips the POST.

**SETTLE_MS.** Milliseconds spanning the live-status samples before claiming a done or blocked POST, and before idle or unknown clears `posted`. Default 20000. Three samples at the start, middle, and end of this window; one flip aborts. `0` skips the wait and extra samples (one immediate check total). Set it in `.env` or the process environment.

**SETTLE_ROUNDS.** How many SETTLE_MS windows must pass back-to-back before a done or blocked POST is claimed. Default 3 (~60s sustained with the default SETTLE_MS). One failed round aborts. Ignored when SETTLE_MS=0. Idle or unknown latch-clear stays one window.

**MIN_WORKING_MS.** Minimum time the pane must have been `working` before a done or blocked POST. Default 30000. The latch stores `working_since` (unix seconds) when seen becomes working. Refuse the POST unless that stamp is set and `(now - working_since) * 1000 >= MIN_WORKING_MS`. Cleared after a successful done POST and on unknown. `0` skips the floor (use with SETTLE_MS=0 for tests). Set it in `.env` or the process environment.

**NOTE.** SETTLE_MS re-check is a stopgap. Herdr 0.8.2 and preview plugins expose only `pane.agent_status_changed`. There is no job-level settle, session-end, or run-finished event. Screen-manifest agents, including Grok, emit turn-level done, so a turn gap looks like a finished job. 7b096c0 (2x10s dual rounds) missed zed-llm-audit: ~22s quiet done, title was the plain job name so the title guard did not fire, both rounds passed, latch posted=done, step9-llm-audit.md still missing, Zed still mid-job. Window is now 3x20s plus MIN_WORKING_MS. Still a stopgap until Herdr ships a job-level signal. A later per-job artifact gate is optional. Do not invent a sidecar EXIT file.

**RETRACT_ON_RESUME.** Default 1. When the event status is working or blocked and latch `posted` is done or another notify status, POST the usual payload plus `retracted: true` and `prior_status` set to that posted value. `status` is the current event status (working or blocked). After a successful retract POST, `posted` is cleared so a later done can fire. `0` only updates `seen` and keeps `posted` (old latch).

## Ceiling

Herdr `done` is turn-level for screen-manifest agents, including Grok. Sleep settle cannot tell a long think gap from a finished job without waiting many minutes, and that wait delays real notifies.

zed-llm-audit went ~22s quiet past 7b096c0's dual rounds. Title was the plain job name, so the title guard did not fire. zed-terminal-orch (w64:p1) went ~62s quiet past cdd5d03 (20s×3 plus MIN_WORKING 30s). plugin-log claimed done after ~62285ms on a clean title. Latch `seen=working` `posted=done`. `terminal-v1-status.md` was missing. Zed was still mid-run.

Do not raise SETTLE_MS or SETTLE_ROUNDS further. Upgrade when Herdr ships a job-level settle, session-end, or run-finished event. A later per-job artifact gate is optional. Do not invent a sidecar EXIT file.

A missing `.env`, an empty URL, or an empty key makes the hook exit 0. An installed plugin with no config stays quiet.

A notify transition always runs `herdr agent get` for the pane name, even when the event already has a cwd. If the lookup fails, the hook still POSTs what it has and writes one line to stderr.

Plain `http://` is refused unless the host is loopback. Redirects are not followed, so a 302 cannot carry the bearer token to another host.

## Payload

```json
{
  "event": "pane.agent_status_changed",
  "status": "done",
  "pane_id": "w1:p1",
  "pane_name": "api",
  "workspace_id": "w1",
  "workspace_label": "api",
  "cwd": "/home/you/src/api",
  "agent": "claude",
  "display_agent": null,
  "ts": "2026-09-02T14:05:00Z"
}
```

Pane name is `name`, then `terminal_title_stripped`, then `display_agent`. Workspace label comes from `HERDR_PLUGIN_CONTEXT_JSON` when Herdr provides it.

A retract POST is that same object plus `retracted: true` and `prior_status` set to the latch value being undone. `status` is the current event status, usually `working`.

## Skip list

Names you never want to notify go in `SKIP_PANES`. Globs work, so `logs-*` is one entry.

A glob like `*-portrait` also hides job panes with that name, such as `tess-portrait`, so keep skip patterns narrower than your job names.

Skip matching uses the resolved pane name. A pane Herdr cannot name still notifies, with a stderr line saying lookup failed.

## Dry run

Set `WEBHOOK_DRY_RUN=1` and trip a notify transition. `herdr plugin log list` should show the JSON and no POST.

## Agent Stop hooks (instant done)

Claude Code and Codex know exactly when their turn ends. `python3 hook.py stop <agent>` is a Stop hook that POSTs done right away, skipping SETTLE_MS and MIN_WORKING_MS. It only runs inside a Herdr pane (`HERDR_ENV=1` and `HERDR_PANE_ID`), reads the same plugin `.env`, honors SKIP_PANES, NOTIFY_STATUSES and WEBHOOK_DRY_RUN, and skips when `stop_hook_active` is true. It claims `posted=done` in the shared latch before the POST, so the settled plugin run for that turn stays quiet and a later working event still retracts. If the POST fails it restores the latch and the plugin run delivers as before. It always exits 0 and writes nothing to stdout, so it never blocks the agent.

The payload is the usual object plus `source: "stop_hook"`, `session_id`, and `last_assistant_message` (first 4000 characters; `last_assistant_message_truncated: true` when cut). The config dir comes from `HERDR_PLUGIN_CONFIG_DIR`, else `herdr plugin config-dir jefflau.herdr-webhook`.

Claude Code `~/.claude/settings.json`:

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 /path/to/herdr-webhook/hook.py stop claude", "timeout": 15}]}]}}
```

Codex `~/.codex/hooks.json` (needs `[features] hooks = true`):

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 /path/to/herdr-webhook/hook.py stop codex", "timeout": 15}]}]}}
```

## Grok Bot webhook routines

Create a webhook routine in Grok Bot. The URL and sender key live in that routine panel. Do not paste the sender key into chat.

Put `WEBHOOK_URL` and `WEBHOOK_KEY` in the plugin `.env`. For Grok Bot also set `WEBHOOK_KEY_HEADER=X-Automation-Key`.

When a pane transitions into done or blocked, Herdr POSTs the JSON. The Grok Bot lead receives the webhook and dumps the result to the user. A payload with `retracted: true` means the prior notify (`prior_status`) was a turn gap, not job end. Ignore that prior done or blocked. The job is running again. Wait for a later done or blocked without `retracted`.
