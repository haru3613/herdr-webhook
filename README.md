# Herdr Webhook

Herdr already knows when a pane is done or blocked. This plugin POSTs that change as JSON to a URL you own.

POSTs only when agent_status changes into a notify status, done or blocked by default. A pane that is already settled does not fire again. First sighting of a pane is stored in last-status.json in the plugin config directory and skipped. If that file is missing, the hook seeds it from `herdr agent list` so leftover events for panes that are already done or blocked stay quiet.

Requires Herdr 0.7.5 or newer and `python3` on linux or macOS.

## Install

```sh
herdr plugin install jefflau/herdr-webhook
```

Or clone the repo and link it:

```sh
herdr plugin link /path/to/herdr-webhook
```

## Config

Put secrets in the plugin config directory. GitHub installs replace the checkout, so a `.env` in the repo would vanish or get published.

```sh
CONFIG_DIR="$(herdr plugin config-dir jefflau.herdr-webhook)"
cp .env.example "$CONFIG_DIR/.env"
```

Edit `$CONFIG_DIR/.env`.

**WEBHOOK_URL.** The POST target.

**WEBHOOK_KEY.** Sent as `Authorization: Bearer <key>`.

**WEBHOOK_KEY_HEADER.** Optional. If set, the raw key is also sent under that header. Grok Bot webhook routines need `WEBHOOK_KEY_HEADER=X-Automation-Key`.

**NOTIFY_STATUSES.** Defaults to `done,blocked`. Allowed values are `idle`, `working`, `blocked`, `done`, `unknown`.

**SKIP_PANES.** Comma-separated case-insensitive globs matched against the pane name. Empty means do not skip.

**WEBHOOK_DRY_RUN.** `1` prints the JSON to stdout and skips the POST.

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

Skip matching uses the resolved pane name. A pane Herdr cannot name still notifies, with a stderr line saying lookup failed.

## Dry run

Set `WEBHOOK_DRY_RUN=1` and trip a notify transition. `herdr plugin log list` should show the JSON and no POST.

## Grok Bot webhook routines

Create a webhook routine in Grok Bot. The URL and sender key live in that routine panel. Do not paste the sender key into chat.

Put `WEBHOOK_URL` and `WEBHOOK_KEY` in the plugin `.env`. For Grok Bot also set `WEBHOOK_KEY_HEADER=X-Automation-Key`.

When a pane transitions into done or blocked, Herdr POSTs the JSON. The Grok Bot lead receives the webhook and dumps the result to the user.
