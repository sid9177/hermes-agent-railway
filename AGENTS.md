# AGENTS.md

## Environment Variable Precedence

Hermes loads `~/.hermes/.env` with `override=True` at gateway startup, which
**overwrites** Railway service variables. For Hermes-specific config
(`TELEGRAM_ALLOWED_USERS`, `TELEGRAM_WEBHOOK_URL`, `TELEGRAM_WEBHOOK_SECRET`,
`GATEWAY_ALLOW_ALL_USERS`, etc.), set them in the **Hermes dashboard's API Keys
page**, not as Railway service variables.

Railway service variables only work for the auth proxy and entrypoint:
- `DASHBOARD_PASSWORD` (required)
- `DASHBOARD_USER` (default: `admin`)
- `AUTO_UPDATE` (default: `false`)
- `IDLE_TIMEOUT_MINUTES` (default: `30`)
- `GATEWAY_POLL_INTERVAL` (default: `60`)
- `PORT` (injected by Railway)

## Architecture Notes

- **Auth proxy** (port 8080 or Railway's `PORT`): public-facing entry point,
  handles cookie auth, activity tracking, idle detection
- **Hermes Dashboard** (port 9199, internal only): FastAPI web UI, never
  exposed externally
- **Telegram Adapter** (port 8443, internal only): started by the gateway
  when `TELEGRAM_WEBHOOK_URL` is set; receives Telegram webhook POSTs
- **Webhook Platform** (port 8644, internal only): Hermes's generic webhook
  receiver, separate from Telegram

The auth proxy forwards `/telegram/webhook` requests to the Telegram adapter
on port 8443, not to the dashboard.

## Patch Maintenance

`patches/apply_patches.py` modifies upstream Hermes files at Docker build
time using exact string matching. When upstream `NousResearch/hermes-agent`
changes the target files, the build will fail with "Could not find target
block". To fix:

1. Clone upstream: `git clone https://github.com/NousResearch/hermes-agent.git`
2. Find the changed file (e.g. `web/src/components/ModelPickerDialog.tsx`)
3. Compare the patch's `old_*` strings against the current upstream code
4. Update the patterns to match the new upstream code
5. Test locally: `HERMES_AGENT_ROOT=/path/to/clone python patches/apply_patches.py`
6. Rebuild and verify

The backend half of the model picker feature (showing unauthenticated
providers) was merged upstream and is no longer patched. Only the frontend
activation UI (API key entry for unauthenticated providers) is still patched.

## Gateway Restart

The dashboard manages gateway restarts via `/api/actions/gateway-restart/*`
endpoints. The auth proxy does NOT restart the gateway on config/env writes
(removed to prevent port conflicts). Manual restart is available via:
- The dashboard's gateway widget (Restart button)
- `POST /api/gateway/restart`
- Railway terminal: `hermes gateway restart`