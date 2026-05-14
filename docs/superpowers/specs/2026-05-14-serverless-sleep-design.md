# Serverless Sleep Design Spec

**Date:** 2026-05-14
**Status:** Draft
**Context:** Hermes Agent deployment on Railway with serverless mode enabled but never sleeping due to continuous internal polling.

## Problem

The Hermes Agent container on Railway runs 24/7, consuming resources and increasing cost. Railway's serverless (sleep) mode is enabled, but the container never sleeps because:

1. The gateway status widget polls `/api/gateway/status` every 10 seconds unconditionally
2. The dashboard polls `/api/status` and `/api/sessions` continuously (~10s intervals)
3. The Telegram gateway runs a continuous outbound long-polling loop
4. Health checks hit `/api/health` periodically

Result: ~730 hours/month of compute instead of the ~60-120 hours that would be needed based on actual usage, representing an 83-92% cost savings opportunity.

## Design Goals

1. Container sleeps when idle (~30 min without real activity)
2. Telegram messages wake the container via webhook
3. Time-critical cron jobs fire reliably via a companion Railway cron service
4. Dashboard users see no disruption (automatic reconnection on wake)
5. Auth sessions survive sleep/wake cycles
6. Zero manual setup required from the user beyond setting Telegram webhook env vars

## User Constraints

- Uses Telegram only (no Discord/Slack)
- Has time-critical cron jobs
- Closes dashboard tab when not in use
- Wants to keep the gateway status widget but poll less aggressively
- Primary goal: reduce Railway cost

## Architecture

```
Internet → Railway Proxy → Auth Proxy (cookie auth + activity tracking)
                                 ↓
                          Hermes Dashboard (port 9199)
                          Hermes Gateway (Telegram webhook mode)
                          /api/health (Railway health check)
                          /api/cron/wake (companion cron trigger)

Companion Cron Service (separate Railway service):
  Every 5-15 min → GET https://<hermes-url>/api/cron/wake
  (wakes container before scheduled Hermes cron jobs)
```

## Component Changes

### 1. Client-Side Poll Control (auth_proxy.py — GATEWAY_WIDGET)

**Current behavior:** `setInterval(gwStatus, 10000)` — unconditional 10s polling, never stops.

**New behavior:**

- Poll interval: 10s → 60s when visible
- Pause all polling when tab hidden (`document.visibilitychange` API)
- After 5 minutes of no user interaction (mouse, keyboard, scroll), switch to a 5-minute "check if awake" interval (not stop entirely — the check ensures reconnection when the container wakes)
- When the proxy returns 204 to a status/sessions poll, the client also switches to the 5-minute "check if awake" interval
- On receiving a non-204 response (actual data), resume normal 60s polling immediately
- Resume 60s polling immediately on any user interaction (mouse, keyboard, scroll) or tab visibility change
- This layered approach ensures: (a) traffic stops when idle, (b) the client automatically detects when the server wakes

**Rationale:** Railway determines sleep based on inbound network traffic at the infrastructure level. Returning 204 from the proxy does not reduce Railway's observed traffic. The client must actually stop sending requests for Railway to detect inactivity.

### 2. Activity-Aware Proxy (auth_proxy.py)

**Purpose:** Track real user activity and signal the client when idle.

**Implementation:**

- Maintain `last_real_activity` timestamp in memory
- "Real activity" = any authenticated, non-polling request:
  - Page loads (GET for HTML)
  - Config changes (PUT /api/config, PUT /api/env)
  - API key saves
  - Logout
  - Telegram webhook requests
  - Cron wake requests
- "Not real activity" = polling endpoints:
  - GET /api/status
  - GET /api/sessions
  - GET /api/gateway/status
- After `IDLE_TIMEOUT_MINUTES` (default 30) of no real activity:
  - The proxy continues to forward all requests to Hermes (no 204 short-circuit)
  - However, this is primarily for the **client signal**: the client-side idle detection stops sending polls
  - The proxy can optionally log idle state for monitoring
- Any real request resets the idle timer

**Important:** The proxy does NOT block legitimate requests when idle. It tracks activity and signals idle state to the client. The actual traffic reduction comes from the client stopping polls.

### 3. Telegram Webhook Wake (auth_proxy.py — new endpoint)

**New route:** `POST /telegram/webhook`

- Unauthenticated (Telegram verifies via `TELEGRAM_WEBHOOK_SECRET`)
- Not subject to idle throttling — always resets the activity timer
- Proxies the request to the Hermes gateway's internal webhook endpoint
- Must handle the case where the gateway hasn't started yet (container just woke):
  - Return 200 OK immediately to Telegram (Telegram retries on timeout, but accepting quickly prevents retries)
  - Queue the message or rely on Hermes's own webhook handling once started

**Configuration:**
- User sets `TELEGRAM_WEBHOOK_URL=https://<railway-url>/telegram/webhook` in Hermes dashboard
- User sets `TELEGRAM_WEBHOOK_SECRET=<random-secret>` (required by Hermes)
- Hermes gateway switches from long-polling to webhook mode

**Cold start flow:**
1. Telegram sends webhook POST to `/telegram/webhook`
2. Railway wakes the container (if sleeping)
3. `entrypoint.sh` runs: dashboard + gateway start
4. Gateway starts in webhook mode, registers with Telegram API
5. Telegram delivers messages via webhook

**Fallback:** If `TELEGRAM_WEBHOOK_URL` is not set, the gateway uses long-polling mode. Messages sent while the container is asleep will be queued by Telegram and delivered on next long-poll cycle after wake. Some delay is expected.

### 4. Cron Wake Endpoint (auth_proxy.py — new endpoint)

**New route:** `GET /api/cron/wake`

- Unauthenticated, lightweight
- Returns `{"status": "awake", "timestamp": "<iso8601>"}` immediately
- Resets the activity timer
- Wakes the container if sleeping (inbound traffic triggers Railway wake)

**Companion service:** A separate Railway service with `cronSchedule` that pings this endpoint on a regular schedule. See section 7.

### 5. Auth SECRET Persistence (auth_proxy.py)

**Current behavior:** `SECRET = secrets.token_bytes(32)` — generated fresh on every process start. Every sleep/wake cycle invalidates all auth cookies.

**New behavior:**

```python
secret_file = os.path.join(HERMES_HOME, ".proxy_secret")
if os.path.exists(secret_file):
    with open(secret_file, "rb") as f:
        SECRET = f.read()
else:
    SECRET = secrets.token_bytes(32)
    with open(secret_file, "wb") as f:
        f.write(SECRET)
```

- Persists to `/root/.hermes/.proxy_secret` (survives wakes if volume is attached)
- If no volume is attached, SECRET regenerates on each start (acceptable — no persistence without a volume)
- Auth cookies survive sleep/wake cycles when volume is present

### 6. WebSocket Reconnection (auth_proxy.py — GATEWAY_WIDGET)

**Current behavior:** No reconnection logic. WebSocket connections die silently when the container sleeps.

**New behavior:**

- Add reconnection logic with exponential backoff to the injected widget JS
- On WebSocket `close` event:
  - If close code is 1000 (normal) or 1001 (going away): reconnect with 1s initial delay
  - For other close codes: start with 2s delay, exponential backoff (2s → 4s → 8s → 16s), max 30s
  - After 10 failed reconnection attempts: show "Connection lost" status with a "Retry" button
- On successful reconnection: silently resume, no page reload needed
- Use the existing 7-day auth cookie for re-authentication (no login prompt)

### 7. Companion Cron Service (new Railway service)

**Purpose:** Wake the Hermes container on a schedule so that time-critical cron jobs can fire.

**Implementation:**

A lightweight Dockerfile/service in the same Railway project:

```dockerfile
FROM alpine:3.19
RUN apk add --no-cache curl
CMD ["sh", "-c", "while true; do curl -sf https://$HERMES_URL/api/cron/wake || true; sleep 300; done"]
```

Wait — Railway cron jobs use `cronSchedule` which starts a separate container on a schedule, not a long-running loop. Better approach:

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "deploy": {
    "cronSchedule": "*/5 * * * *",
    "startCommand": "curl -sf ${HERMES_URL}/api/cron/wake",
    "restartPolicyType": "NEVER"
  }
}
```

The `HERMES_URL` environment variable is set in the companion service's Railway config to point to the Hermes service's public URL (e.g., `https://hermes-agent-production.up.railway.app`). In Railway's private network, you can also use the internal URL (`http://hermes-agent.railway.internal:8080`).

This creates a minimal container every 5 minutes that pings the Hermes service and exits. Since it runs for only ~2 seconds per invocation, compute cost is negligible (~0.15 hours/month).

**Minimum schedule:** Railway requires minimum 5-minute intervals for cron (`*/5 * * * *`). If the user has cron jobs more frequent than every 5 minutes, the companion service keeps the container awake continuously (equivalent to current behavior for that period).

**Alternative:** Users with infrequent cron jobs (hourly, daily) can use a less aggressive schedule (e.g., `0 * * * *` for hourly).

### 8. AUTO_UPDATE Default Change (entrypoint.sh)

**Current:** `AUTO_UPDATE="${AUTO_UPDATE:-false}"` — wait, looking at the actual entrypoint.sh:

```bash
AUTO_UPDATE="${AUTO_UPDATE:-false}"
```

It's already `false` by default. ✅ No change needed.

### 9. Health Check Timeout (railway.json)

**Current:** `"healthcheckTimeout": 300` (5 minutes)

**Change:** Reduce to `"healthcheckTimeout": 30`

The auth proxy health endpoint responds in <10ms. 300 seconds is excessive and adds unnecessary delay to cold starts. Railway waits until the health check passes before routing traffic, so a shorter timeout means faster wake-up responsiveness.

### 10. Dashboard Frontend /api/status Polling

Hermes's built-in dashboard frontend (not our code) also polls `/api/status` continuously. The auth proxy injects the gateway widget but doesn't control Hermes's own poll loop.

**Mitigation:** The activity-aware proxy approach handles this:
- When the browser tab is hidden, the gateway widget polls stop (our JS)
- Hermes's own frontend polls may still run in some browsers
- However, if the tab is closed entirely, all polls stop
- Since the user closes the dashboard tab when not in use, this is acceptable

**Future consideration:** If Hermes's frontend polling becomes an issue (e.g., users leave tabs open), we could inject additional JS that throttles the dashboard's own poll frequency. This is out of scope for v1.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `IDLE_TIMEOUT_MINUTES` | `30` | Minutes with no real activity before client-side idle detection kicks in |
| `GATEWAY_POLL_INTERVAL` | `60` | Seconds between gateway status widget polls when visible |
| `TELEGRAM_WEBHOOK_URL` | (empty) | Set to `https://<railway-url>/telegram/webhook` to enable webhook wake |
| `TELEGRAM_WEBHOOK_SECRET` | (empty) | Required when TELEGRAM_WEBHOOK_URL is set. Hermes enforces this. |

Note: `AUTO_UPDATE` is already `false` by default. No change needed.

## Files to Modify

| File | Changes |
|---|---|
| `auth_proxy.py` | Client-side poll control (GATEWAY_WIDGET rewrite), activity tracking middleware, Telegram webhook proxy endpoint, cron wake endpoint, auth SECRET persistence, WebSocket reconnection logic |
| `railway.json` | Reduce healthcheckTimeout from 300 to 30 |
| (new) `cron-companion/Dockerfile` | Minimal Alpine + curl image for companion cron service |
| (new) `cron-companion/railway.json` | Cron schedule configuration |

## Files Not Modified

| File | Reason |
|---|---|
| `Dockerfile` | No changes to build process |
| `entrypoint.sh` | AUTO_UPDATE already defaults to false |
| `patches/apply_patches.py` | No patches needed to Hermes upstream |
| `README.md` | Update after implementation |

## Edge Case Matrix

| Scenario | Behavior |
|---|---|
| Dashboard tab closed by user | All polling stops. Container sleeps after Railway idle timeout (~5-15 min after last request). |
| Dashboard tab open, user away 30+ min | Client-side idle detection stops polls. Container sleeps. |
| Dashboard tab open, user active | Normal operation. 60s gateway widget polls, dashboard polls, all proxied. |
| Dashboard tab hidden (background) | Page Visibility API pauses all polls. Container sleeps. |
| Telegram message while awake (webhook mode) | Inbound webhook → proxies to gateway → normal processing. |
| Telegram message while asleep (webhook mode) | Railway wakes container → cold start (10-30s) → gateway starts → message delivered on next webhook or reconnect. |
| Telegram message while asleep (polling mode) | **Message queued by Telegram, delivered on next wake.** Recommend webhook mode. |
| cron job while asleep | Companion cron service has woken container on its schedule → job runs. |
| cron job while awake | Normal processing. |
| Health check while asleep | Railway's internal health probe — exempt from sleep detection. |
| Auth cookie after wake (with volume) | SECRET persisted → cookie survives → no re-login needed. |
| Auth cookie after wake (without volume) | SECRET regenerated → cookie invalid → user must re-login. Acceptable; volume recommended anyway. |
| WebSocket connection on sleep | Connection closed. Client-side reconnection with exponential backoff. |
| Browser tab open during wake | HTTP request gets proxied, resets activity timer, dashboard reconnects. |
| Multiple browser tabs | Each tab tracks visibility independently. Hidden tabs pause polls. |

## Limitations

1. **Discord bots are incompatible with serverless sleep** — Discord requires persistent WebSocket connections. Users needing Discord should not enable serverless sleep.
2. **Slack Socket Mode is incompatible** — Slack Events API works; Socket Mode does not.
3. **Cold start delay** — ~10-30 seconds for container wake + service startup. Telegram webhook messages during this window may be retried by Telegram (which has its own retry mechanism).
4. **Dashboard polling from Hermes's own frontend** — Our client-side idle detection only controls the gateway widget. Hermes's built-in dashboard poll loop continues when the tab is visible. If users leave tabs open indefinitely, the container won't sleep until the tab is closed.
5. **Companion cron schedule granularity** — Minimum 5-minute intervals via Railway cron. Cron jobs more frequent than 5 minutes require the container to stay awake.
6. **No auto-detection of Hermes cron schedules** — The companion cron service runs on a fixed schedule, not synced to Hermes's cron configuration. Users must ensure the companion schedule is aggressive enough to wake the container before their Hermes cron jobs fire.

## Out of Scope (v1)

- Injecting JS to throttle Hermes's built-in dashboard polls
- Slack Events API webhook support (can be added later following the Telegram webhook pattern)
- Auto-syncing companion cron schedule with Hermes cron configuration
- Volume-aware SECRET persistence (implemented but gracefully degrades without volume)