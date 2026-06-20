# Hermes Agent on Railway

Deploy [Hermes Agent](https://hermes-agent.nousresearch.com/) to Railway with one click. Hermes is an open-source AI agent by Nous Research with tool use, memory, messaging platform integrations, and a web dashboard.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/template/TEMPLATE_ID?referralCode=REFERRAL_CODE)

## Features

This template goes beyond a basic Hermes deploy:

- **Full dashboard access** — manage config, API keys, sessions, logs, analytics, cron jobs, and skills from your browser. No SSH or CLI needed.
- **Messaging gateway included** — Telegram, Discord, and Slack bots run alongside the dashboard. Configure platform tokens in the UI, hit restart, and your bot is live.
- **Gateway management widget** — a floating status indicator and restart button injected into the dashboard. See at a glance if the gateway is running, restart it after config changes without redeploying. Smart polling reduces traffic when idle.
- **Serverless sleep support** — the container sleeps when idle, reducing Railway costs by up to 90%. Telegram webhooks wake the container on incoming messages. A companion cron service ensures scheduled jobs run on time.
- **Cookie-based auth with persistent sessions** — password-protected login page with session cookies that survive sleep/wake cycles when a volume is attached.
- **Auto-updates** — pulls the latest Hermes release on every container restart. Always up to date, no manual intervention. Disable with `AUTO_UPDATE=false` to pin a version (recommended for serverless deployments).
- **Zero config to start** — deploy with just a password, then set up everything else (LLM provider, API keys, messaging platforms) from the dashboard UI.
- **Persistent storage** — attach a Railway volume to keep sessions, memories, config, and logs across redeploys.

## Setup

1. Click the **Deploy on Railway** button above
2. Set `DASHBOARD_PASSWORD` (required)
3. Deploy — log in at your Railway URL
4. Add your LLM provider key (e.g. OpenRouter) on the **API Keys** page
5. Optionally configure Telegram/Discord/Slack tokens and hit **Restart** on the gateway widget

## Environment Variables

| Variable | Description |
|---|---|
| `DASHBOARD_USER` | Login username (default: `admin`) |
| `DASHBOARD_PASSWORD` | Login password (**required** — deploy will fail without it) |
| `AUTO_UPDATE` | Pull latest Hermes on every restart (default: `false`, set to `true` to auto-update) |
| `IDLE_TIMEOUT_MINUTES` | Minutes without real activity before entering idle mode (default: `30`) |
| `GATEWAY_POLL_INTERVAL` | Seconds between gateway status widget polls (default: `60`) |

All other configuration is done through the dashboard after deploy.

## Persistent Storage

To keep your data across redeploys, attach a Railway volume:

1. Right-click the service in your Railway project
2. Select **Attach Volume**
3. Set mount path to `/root/.hermes`

This persists sessions, memories, API keys, config, logs, auth cookies, and cron jobs.

## Serverless Sleep

This deployment supports Railway's serverless (sleep) mode to reduce costs when idle. The container will sleep after a period of inactivity and automatically wake when:

- You visit the dashboard URL
- A Telegram message arrives (if webhook mode is configured)
- The companion cron service pings `/api/cron/wake`

### Telegram Webhook Setup (Recommended)

To enable instant wake-on-message for Telegram:

1. In the Hermes dashboard, go to **API Keys** and set:
   - `TELEGRAM_WEBHOOK_URL` = `https://<your-railway-url>/telegram/webhook`
   - `TELEGRAM_WEBHOOK_SECRET` = a random secret string (e.g., generate with `openssl rand -hex 32`)
2. Restart the gateway via the widget or redeploy
3. Telegram messages will now wake the container if it's asleep

Without webhook mode, Telegram messages sent while the container is sleeping will be queued and delivered on next wake. Some delay is expected.

### Companion Cron Service

A separate lightweight Railway service pings `/api/cron/wake` every 5 minutes to ensure the container is awake for scheduled Hermes cron jobs. See `cron-companion/` for the Dockerfile and configuration.

To deploy the companion service:
1. Add a new service to your Railway project
2. Point it to the `cron-companion/` directory
3. Set the `HERMES_URL` environment variable to your Hermes service's URL (e.g., `https://hermes-agent-production.up.railway.app`)

### Limitations

- **Discord bots require persistent WebSocket connections** and are incompatible with serverless sleep
- **Cold start delay**: ~10-30 seconds when waking from sleep
- **Dashboard polling pauses** when the browser tab is hidden or the user is idle for 5+ minutes
- **Auth cookies survive sleep/wake** when a volume is attached; without a volume, re-login is required after each wake

## Architecture

```
Internet -> Railway -> Auth Proxy (cookie login + activity tracking + idle detection)
                            |
                            +-> Hermes Dashboard (port 9199)
                            +-> Messaging Gateway (Telegram webhook / polling)
                            +-> Telegram Adapter (port 8443, webhook receiver when TELEGRAM_WEBHOOK_URL is set)
                            +-> /api/health (unauthenticated, Railway health checks)
                            +-> /api/cron/wake (unauthenticated, companion cron trigger)
                            +-> /api/gateway/status (authenticated, idle-aware: returns 204 when idle)
                            +-> /api/idle (authenticated, check idle state)
                            +-> /telegram/webhook (unauthenticated, proxied to Telegram Adapter on port 8443)
```

## Resources

- [Hermes Agent Documentation](https://hermes-agent.nousresearch.com/docs)
- [GitHub Repository](https://github.com/NousResearch/hermes-agent)
- [Web Dashboard Guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard)
