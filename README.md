# Herdr Webhook

Herdr already knows when a pane is done or blocked. This plugin POSTs that change as JSON to a URL you own.

POSTs when a pane changes into a notify status (done or blocked by default) that has not already been posted for that pane. last-status.json stores `seen` and `posted` per pane. working does not clear `posted`, so done then working then done is one POST. A done or blocked POST requires a prior `seen` of working or blocked. First sighting of a pane is stored and skipped, and unknown or idle into done does not POST. Before a done or blocked POST, the hook samples live `herdr agent get` at least 3 times across SETTLE_MS (default 10000). Every sample must still equal the event status. One flip aborts, records `seen`, and does not POST. If `terminal_title_stripped` or `title` from that get still looks mid-run (thinking, responding, waiting for response, running, tool call), treat it as unsettled and do not POST. idle or unknown clears `posted` only after the same multi-sample confirm still reads idle or unknown, so a brief idle flicker does not reopen the latch. SETTLE_MS=0 skips the wait and extra samples (single immediate path, for tests). If the file is missing, the hook seeds it from `herdr agent list` so leftover events for panes that are already done or blocked stay quiet. An older file that stored only the last status is migrated. `posted` is claimed before the POST goes out, so overlapping hook runs for the same pane cannot both fire. If the POST fails, that run's claim is cleared and the next transition into that status retries. A newer overlapping claim is left alone.

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
SETTLE_MS=10000
EOF
```

That writes the file even if your shell is not in the plugin checkout. If you cloned the repo, `cp .env.example "$CONFIG_DIR/.env"` works from the clone. Then edit `$CONFIG_DIR/.env`.

**WEBHOOK_URL.** The POST target.

**WEBHOOK_KEY.** Sent as `Authorization: Bearer <key>`.

**WEBHOOK_KEY_HEADER.** Optional. If set, the raw key is also sent under that header. Grok Bot webhook routines need `WEBHOOK_KEY_HEADER=X-Automation-Key`.

**NOTIFY_STATUSES.** Defaults to `done,blocked`. Allowed values are `idle`, `working`, `blocked`, `done`, `unknown`.

**SKIP_PANES.** Comma-separated case-insensitive globs matched against the pane name. Empty means do not skip.

**WEBHOOK_DRY_RUN.** `1` prints the JSON to stdout and skips the POST.

**SETTLE_MS.** Milliseconds spanning the live-status samples before claiming a done or blocked POST, and before idle or unknown clears `posted`. Default 10000. Three samples at the start, middle, and end of this window; one flip aborts. `0` skips the wait and extra samples. Set it in `.env` or the process environment.

**NOTE.** SETTLE_MS re-check is a stopgap. Herdr 0.8.2 and preview plugins expose only `pane.agent_status_changed`. There is no job-level settle, session-end, or run-finished event. Screen-manifest agents, including Grok, emit turn-level done, so a turn gap looks like a finished job. Upgrade this when Herdr ships a cleaner job-level signal. Do not invent a sidecar EXIT file.

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

## Skip list

Names you never want to notify go in `SKIP_PANES`. Globs work, so `logs-*` is one entry.

A glob like `*-portrait` also hides job panes with that name, such as `tess-portrait`, so keep skip patterns narrower than your job names.

Skip matching uses the resolved pane name. A pane Herdr cannot name still notifies, with a stderr line saying lookup failed.

## Dry run

Set `WEBHOOK_DRY_RUN=1` and trip a notify transition. `herdr plugin log list` should show the JSON and no POST.

## Grok Bot webhook routines

Create a webhook routine in Grok Bot. The URL and sender key live in that routine panel. Do not paste the sender key into chat.

Put `WEBHOOK_URL` and `WEBHOOK_KEY` in the plugin `.env`. For Grok Bot also set `WEBHOOK_KEY_HEADER=X-Automation-Key`.

When a pane transitions into done or blocked, Herdr POSTs the JSON. The Grok Bot lead receives the webhook and dumps the result to the user.
