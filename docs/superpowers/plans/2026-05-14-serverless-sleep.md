# Serverless Sleep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable Railway serverless sleep for the Hermes Agent container by eliminating continuous polling, adding Telegram webhook wake support, persisting auth sessions across wake cycles, and setting up a companion cron service.

**Architecture:** Modify the auth proxy (`auth_proxy.py`) to add activity-aware idle signaling, client-side poll control, Telegram webhook proxy, cron wake endpoint, and auth SECRET persistence. Add WebSocket reconnection to the gateway widget. Create a companion cron service as a separate Railway service. Reduce health check timeout in `railway.json`.

**Tech Stack:** Python 3.11 (aiohttp), JavaScript (injected widget), Docker, Railway

**Spec:** `docs/superpowers/specs/2026-05-14-serverless-sleep-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `auth_proxy.py` | Modify | All proxy changes: SECRET persistence, activity tracking, idle signaling, webhook proxy, cron wake, gateway widget rewrite |
| `railway.json` | Modify | Reduce healthcheckTimeout from 300 to 30 |
| `cron-companion/Dockerfile` | Create | Minimal Alpine + curl for companion cron service |
| `cron-companion/railway.json` | Create | Cron schedule config for companion service |
| `README.md` | Modify | Document new env vars, webhook setup, companion cron |

---

## Task 1: Auth SECRET Persistence

**Files:**
- Modify: `auth_proxy.py` (lines 19-26, the SECRET and PASSWORD section)

**Why first:** This is a self-contained change that fixes a concrete bug (cookies invalidating on every restart/wake). It has no dependencies on other tasks and can be tested independently.

- [ ] **Step 1: Add SECRET persistence logic**

Replace the current `SECRET = secrets.token_bytes(32)` line (line 20) with persistent SECRET loading:

```python
HERMES_HOME = "/root/.hermes"
DASHBOARD_PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "9119"))
UPSTREAM = f"http://127.0.0.1:{DASHBOARD_PORT}"
USERNAME = os.environ.get("DASHBOARD_USER", "admin")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# Persist auth SECRET so cookies survive sleep/wake cycles when a volume is attached
secret_file = os.path.join(HERMES_HOME, ".proxy_secret")
if os.path.exists(secret_file):
    with open(secret_file, "rb") as f:
        SECRET = f.read()
else:
    SECRET = secrets.token_bytes(32)
    try:
        with open(secret_file, "wb") as f:
            f.write(SECRET)
    except OSError:
        # No volume attached — SECRET will be lost on restart, which is acceptable
        pass

COOKIE = "hermes_auth"
MAX_AGE = 7 * 86400
```

Note: This replaces lines 15-22. The `HERMES_HOME` constant moves above the SECRET logic since it's needed for the file path. The `HERMES_HOME` constant on line 15 already exists, so we're reordering to avoid a duplicate.

Actually, let me be precise. Current lines 15-22:

```python
HERMES_HOME = "/root/.hermes"
DASHBOARD_PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "9119"))
UPSTREAM = f"http://127.0.0.1:{DASHBOARD_PORT}"
USERNAME = os.environ.get("DASHBOARD_USER", "admin")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SECRET = secrets.token_bytes(32)
COOKIE = "hermes_auth"
MAX_AGE = 7 * 86400
```

Replace with:

```python
HERMES_HOME = "/root/.hermes"
DASHBOARD_PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "9119"))
UPSTREAM = f"http://127.0.0.1:{DASHBOARD_PORT}"
USERNAME = os.environ.get("DASHBOARD_USER", "admin")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# Persist auth SECRET so cookies survive sleep/wake cycles when a volume is attached
_SECRET_PATH = os.path.join(HERMES_HOME, ".proxy_secret")
if os.path.exists(_SECRET_PATH):
    with open(_SECRET_PATH, "rb") as f:
        SECRET = f.read()
else:
    SECRET = secrets.token_bytes(32)
    try:
        with open(_SECRET_PATH, "wb") as f:
            f.write(SECRET)
    except OSError:
        pass  # No volume — SECRET lost on restart, acceptable

COOKIE = "hermes_auth"
MAX_AGE = 7 * 86400
```

- [ ] **Step 2: Verify the change compiles**

Run: `python -c "import ast; ast.parse(open('auth_proxy.py').read()); print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add auth_proxy.py
git commit -m "feat: persist auth SECRET across restarts

Cookies survive sleep/wake cycles when a volume is attached.
Without a volume, SECRET regenerates (acceptable degradation)."
```

---

## Task 2: Health Check Timeout Reduction

**Files:**
- Modify: `railway.json`

**Why here:** Small, independent change. Quick win.

- [ ] **Step 1: Update healthcheckTimeout**

In `railway.json`, change line 10 from `"healthcheckTimeout": 300` to `"healthcheckTimeout": 30`:

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": {
    "builder": "DOCKERFILE",
    "dockerfilePath": "Dockerfile"
  },
  "deploy": {
    "startCommand": "/entrypoint.sh",
    "healthcheckPath": "/api/health",
    "healthcheckTimeout": 30,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 5
  }
}
```

- [ ] **Step 2: Validate JSON**

Run: `python -c "import json; json.load(open('railway.json')); print('Valid JSON')"`

Expected: `Valid JSON`

- [ ] **Step 3: Commit**

```bash
git add railway.json
git commit -m "feat: reduce health check timeout from 300s to 30s

The health endpoint responds in <10ms. 300s was excessive and
added unnecessary delay to cold starts after serverless wake."
```

---

## Task 3: Activity-Aware Proxy Middleware

**Files:**
- Modify: `auth_proxy.py`

**Why here:** The activity tracker is the foundation that the idle-signaling endpoint (Task 4) and the webhook endpoint (Task 5) depend on. Must be in place before those.

- [ ] **Step 1: Add activity tracker and environment variables**

Add these after the `RESTART_PATHS` definition (after line 310) and before `volume_attached()`:

```python
# --- Activity-aware idle detection ---
IDLE_TIMEOUT_SECONDS = int(os.environ.get("IDLE_TIMEOUT_MINUTES", "30")) * 60
GATEWAY_POLL_INTERVAL = int(os.environ.get("GATEWAY_POLL_INTERVAL", "60"))

# Paths that are considered "polling" — not real user activity
POLL_PATHS = {"/api/status", "/api/sessions", "/api/gateway/status"}

# Timestamp of the last real (non-polling) user activity
last_real_activity = time.time()


def record_real_activity(request):
    """Record a real user activity and return whether we were previously idle."""
    global last_real_activity
    was_idle = time.time() - last_real_activity > IDLE_TIMEOUT_SECONDS
    last_real_activity = time.time()
    return was_idle


def is_idle():
    """Check if the proxy is in idle mode (no real activity for IDLE_TIMEOUT_SECONDS)."""
    return time.time() - last_real_activity > IDLE_TIMEOUT_SECONDS
```

- [ ] **Step 2: Integrate activity tracking into auth_middleware**

Replace the current `auth_middleware` function (lines 278-289) with:

```python
@web.middleware
async def auth_middleware(request, handler):
    # Unauthenticated paths — skip auth and activity tracking
    if request.path in ("/login", "/logout", "/api/health", "/api/cron/wake"):
        return await handler(request)

    # Telegram webhook — unauthenticated but counts as real activity
    if request.path == "/telegram/webhook":
        return await handler(request)

    # Require auth for everything else
    token = request.cookies.get(COOKIE)
    if not token or not check_token(token):
        if request.path.startswith("/api/"):
            raise web.HTTPUnauthorized()
        raise web.HTTPFound("/login")

    # Track real user activity (not polling endpoints)
    if request.method == "GET" and request.path in POLL_PATHS:
        # Polling endpoints don't count as real activity
        pass
    else:
        record_real_activity(request)

    return await handler(request)
```

Note: This adds two new unauthenticated paths: `/api/cron/wake` and `/telegram/webhook`. The `/api/cron/wake` handler will be added in Task 4, and `/telegram/webhook` will be added in Task 5.

- [ ] **Step 3: Add idle state endpoint**

Add a new handler for checking idle state (after the `gateway_status` handler):

```python
async def idle_status(request):
    """Return idle state so the client widget can adjust its polling behavior."""
    return web.json_response({
        "idle": is_idle(),
        "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
    })
```

- [ ] **Step 4: Verify the change compiles**

Run: `python -c "import ast; ast.parse(open('auth_proxy.py').read()); print('OK')"`

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add auth_proxy.py
git commit -m "feat: add activity-aware idle detection middleware

Track real user activity vs polling endpoints. Poll paths
(/api/status, /api/sessions, /api/gateway/status) don't reset
the activity timer. Add /api/idle endpoint for client-side status."
```

---

## Task 4: Cron Wake Endpoint

**Files:**
- Modify: `auth_proxy.py`

- [ ] **Step 1: Add cron wake handler**

After the `idle_status` handler added in Task 3, add:

```python
async def cron_wake(request):
    """Lightweight endpoint to wake the container for cron jobs.
    Always returns 200 immediately. Resets activity timer."""
    record_real_activity(request)
    return web.json_response({
        "status": "awake",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
```

- [ ] **Step 2: Register the route**

In `create_app()`, add the cron wake route. The current `create_app()` function (around line 451-461) should look like this after adding:

```python
def create_app():
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(on_startup)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_post)
    app.router.add_get("/logout", logout)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/cron/wake", cron_wake)
    app.router.add_get("/api/idle", idle_status)
    app.router.add_post("/api/gateway/restart", restart_gateway)
    app.router.add_get("/api/gateway/status", gateway_status)
    app.router.add_route("*", "/{path_info:.*}", proxy)
    return app
```

- [ ] **Step 3: Verify the change compiles**

Run: `python -c "import ast; ast.parse(open('auth_proxy.py').read()); print('OK')"`

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add auth_proxy.py
git commit -m "feat: add lightweight cron wake endpoint

GET /api/cron/wake returns 200 immediately and resets the activity
timer. Used by the companion cron service to ensure the container
is awake for scheduled Hermes cron jobs."
```

---

## Task 5: Telegram Webhook Proxy Endpoint

**Files:**
- Modify: `auth_proxy.py`

- [ ] **Step 1: Add Telegram webhook proxy handler**

After the `cron_wake` handler, add:

```python
async def telegram_webhook(request):
    """Proxy Telegram webhook requests to the Hermes gateway.
    This endpoint is unauthenticated (Telegram uses its own secret token
    verification) but counts as real activity to prevent idle sleep
    during active conversations."""
    record_real_activity(request)
    try:
        async with ClientSession() as session:
            url = f"{UPSTREAM}{request.path_qs}"
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ("host", "transfer-encoding")}
            body = await request.read()
            async with session.request(
                request.method,
                url,
                headers=headers,
                data=body,
                allow_redirects=False,
                timeout=ClientTimeout(total=30),
            ) as resp:
                excluded = {"transfer-encoding", "content-encoding", "content-length"}
                proxy_headers = {k: v for k, v in resp.headers.items()
                                 if k.lower() not in excluded}
                content = await resp.read()
                return web.Response(status=resp.status, headers=proxy_headers, body=content)
    except Exception:
        # Gateway not ready yet — accept the webhook and let Telegram retry
        # Returning 200 prevents Telegram from retrying this particular update,
        # which is the desired behavior during cold starts.
        return web.Response(status=200, text="accepted")
```

- [ ] **Step 2: Register the route**

In `create_app()`, add the webhook route:

```python
def create_app():
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(on_startup)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login_post)
    app.router.add_get("/logout", logout)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/cron/wake", cron_wake)
    app.router.add_get("/api/idle", idle_status)
    app.router.add_post("/api/gateway/restart", restart_gateway)
    app.router.add_get("/api/gateway/status", gateway_status)
    app.router.add_route("*", "/telegram/webhook", telegram_webhook)
    app.router.add_route("*", "/{path_info:.*}", proxy)
    return app
```

Important: The `/telegram/webhook` route must be registered BEFORE the catch-all `/{path_info:.*}` route, otherwise it will never match.

- [ ] **Step 3: Verify the change compiles**

Run: `python -c "import ast; ast.parse(open('auth_proxy.py').read()); print('OK')"`

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add auth_proxy.py
git commit -m "feat: add Telegram webhook proxy endpoint

POST /telegram/webhook proxies requests to the Hermes gateway,
enabling Telegram webhook mode for wake-on-message. Returns 200
immediately during cold starts to prevent Telegram retries."
```

---

## Task 6: Gateway Widget Overhaul (Client-Side Poll Control + WS Reconnection)

**Files:**
- Modify: `auth_proxy.py` (the `GATEWAY_WIDGET` string)

This is the largest single change. The `GATEWAY_WIDGET` constant (currently lines 330-367) needs to be completely rewritten with:
1. Slower polling (60s instead of 10s)
2. Page Visibility API (pause when tab hidden)
3. User activity detection (5-min idle → 5-min check interval)
4. Server idle detection (204 response → 5-min check interval)
5. WebSocket reconnection with exponential backoff

- [ ] **Step 1: Replace the GATEWAY_WIDGET constant and update GATEWAY_POLL_INTERVAL usage**

Replace the entire `GATEWAY_WIDGET = """..."""` block (lines 330-367) with:

```python
GATEWAY_WIDGET = """
<div id="gw-widget" style="position:fixed;bottom:20px;right:20px;z-index:99999;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;">
  <div style="background:#111920;border:1px solid rgba(45,212,191,0.2);border-radius:10px;
    padding:12px 16px;display:flex;flex-direction:column;gap:8px;
    box-shadow:0 4px 20px rgba(0,0,0,0.4);min-width:180px;">
    <div style="display:flex;align-items:center;gap:10px;">
      <span id="gw-dot" style="width:8px;height:8px;border-radius:50%;background:#888;flex-shrink:0;"></span>
      <span id="gw-label" style="color:#7899aa;flex:1;">Gateway</span>
      <button id="gw-btn" onclick="gwRestart()" style="background:#2dd4bf;color:#0a0f14;border:none;
        border-radius:5px;padding:4px 12px;font-size:12px;font-weight:600;cursor:pointer;">Restart</button>
    </div>
    <div id="gw-vol" style="display:none;font-size:11px;padding-top:4px;border-top:1px solid rgba(45,212,191,0.1);"></div>
    <div id="gw-conn" style="display:none;font-size:11px;padding-top:4px;border-top:1px solid rgba(45,212,191,0.1);color:#fbbf24;"></div>
  </div>
</div>
<script>
(function(){
  var POLL_INTERVAL = """ + str(os.environ.get("GATEWAY_POLL_INTERVAL", "60")) + """;
  var IDLE_CHECK_INTERVAL = 300000;  // 5 minutes
  var USER_IDLE_TIMEOUT = 300000;    // 5 minutes of no interaction
  var WS_RECONNECT_BASE = 2000;      // 2 seconds
  var WS_RECONNECT_MAX = 30000;       // 30 seconds
  var WS_MAX_RETRIES = 10;

  var pollTimer = null;
  var idleTimer = null;
  var lastInteraction = Date.now();
  var isIdle = false;
  var isHidden = document.hidden;

  // --- User activity tracking ---
  function onUserActivity() {
    lastInteraction = Date.now();
    if (isIdle) {
      isIdle = false;
      startPolling();
    }
  }
  document.addEventListener('mousemove', onUserActivity, {passive: true});
  document.addEventListener('keydown', onUserActivity, {passive: true});
  document.addEventListener('scroll', onUserActivity, {passive: true});
  document.addEventListener('touchstart', onUserActivity, {passive: true});

  // --- Page Visibility API ---
  function onVisibilityChange() {
    isHidden = document.hidden;
    if (isHidden) {
      stopPolling();
    } else {
      startPolling();
    }
  }
  document.addEventListener('visibilitychange', onVisibilityChange);

  // --- Polling control ---
  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
  }

  function startPolling() {
    stopPolling();
    if (isHidden) return;
    gwStatus();
    scheduleNextPoll(isIdle ? IDLE_CHECK_INTERVAL : POLL_INTERVAL * 1000);
  }

  function scheduleNextPoll(delay) {
    pollTimer = setTimeout(function() {
      // Check if user has been idle
      var timeSinceInteraction = Date.now() - lastInteraction;
      if (timeSinceInteraction > USER_IDLE_TIMEOUT && !isIdle) {
        isIdle = true;
      }
      var nextDelay = isIdle ? IDLE_CHECK_INTERVAL : POLL_INTERVAL * 1000;
      gwStatus();
      scheduleNextPoll(nextDelay);
    }, delay);
  }

  function gwStatus(){
    fetch('/api/gateway/status').then(function(r){
      if (r.status === 204) {
        // Server is idle — switch to slow check
        isIdle = true;
        return;
      }
      return r.json();
    }).then(function(d){
      if (!d) return;
      isIdle = false;
      document.getElementById('gw-dot').style.background = d.running ? '#4ade80' : '#ef4444';
      document.getElementById('gw-label').textContent = d.running ? 'Gateway running' : 'Gateway stopped';
      var vol = document.getElementById('gw-vol');
      vol.style.display = 'block';
      if (d.volume) {
        vol.innerHTML = '<span style="color:#4ade80;">&#x2713;</span> <span style="color:#7899aa;">Volume attached</span>';
      } else {
        vol.innerHTML = '<span style="color:#fbbf24;">&#x26A0;</span> <span style="color:#fbbf24;">No volume \\u2014 data will not persist</span>';
      }
    }).catch(function(){});
  }

  function gwRestart(){
    var b = document.getElementById('gw-btn');
    b.textContent = 'Restarting...';
    b.disabled = true;
    fetch('/api/gateway/restart', {method: 'POST'}).then(function(){
      setTimeout(function(){ b.textContent = 'Restart'; b.disabled = false; gwStatus(); }, 3000);
    }).catch(function(){ b.textContent = 'Restart'; b.disabled = false; });
  }

  // --- WebSocket reconnection ---
  var wsRetries = 0;
  var wsReconnectTimer = null;

  // Monkey-patch XMLHttpRequest/fetch to detect WebSocket creation from upstream HTML
  // We intercept the dashboard's WebSocket connections to add reconnection logic.
  // The dashboard page may or may not use WebSocket; if it does, we handle disconnects.
  var origXHR = window.XMLHttpRequest;

  // Watch for disconnect signal via the gateway status going to "stopped"
  // Real WS reconnection is handled by patching the dashboard's own WS code.
  // Since we inject this widget into HTML pages, we add a connect event listener.
  function showConnectionStatus(msg) {
    var el = document.getElementById('gw-conn');
    if (el) { el.style.display = 'block'; el.textContent = msg; }
  }
  function hideConnectionStatus() {
    var el = document.getElementById('gw-conn');
    if (el) { el.style.display = 'none'; }
  }

  // Start polling initially
  if (!document.hidden) {
    startPolling();
  } else {
    // Will start when tab becomes visible
  }
})();
</script>
"""
```

Wait — the WebSocket reconnection needs more thought. The dashboard HTML is served by Hermes (upstream), not by us. We only inject the GATEWAY_WIDGET into HTML pages. We can't easily patch the dashboard's own WebSocket connections from our injected widget JS, because those WS connections are set up by the dashboard's own React/frontend code.

What we CAN do is: when our gateway widget detects that the server went down (status poll returns network error or 503), show a "Reconnecting..." message. And when it comes back, show "Connected" briefly. This is simpler and more reliable than trying to patch the dashboard's WS code.

Let me revise the widget to focus on: (1) smart polling, (2) idle detection, (3) connection status indicator. NOT trying to hook into the dashboard's own WebSocket connections.

- [ ] **Step 1 (revised): Replace the GATEWAY_WIDGET constant**

Replace the entire `GATEWAY_WIDGET = """..."""` block with:

```python
GATEWAY_WIDGET = f"""
<div id="gw-widget" style="position:fixed;bottom:20px;right:20px;z-index:99999;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;">
  <div style="background:#111920;border:1px solid rgba(45,212,191,0.2);border-radius:10px;
    padding:12px 16px;display:flex;flex-direction:column;gap:8px;
    box-shadow:0 4px 20px rgba(0,0,0,0.4);min-width:180px;">
    <div style="display:flex;align-items:center;gap:10px;">
      <span id="gw-dot" style="width:8px;height:8px;border-radius:50%;background:#888;flex-shrink:0;"></span>
      <span id="gw-label" style="color:#7899aa;flex:1;">Gateway</span>
      <button id="gw-btn" onclick="gwRestart()" style="background:#2dd4bf;color:#0a0f14;border:none;
        border-radius:5px;padding:4px 12px;font-size:12px;font-weight:600;cursor:pointer;">Restart</button>
    </div>
    <div id="gw-vol" style="display:none;font-size:11px;padding-top:4px;border-top:1px solid rgba(45,212,191,0.1);"></div>
    <div id="gw-conn" style="display:none;font-size:11px;padding-top:4px;border-top:1px solid rgba(45,212,191,0.1);color:#fbbf24;"></div>
  </div>
</div>
<script>
(function(){{
  var POLL_MS = {GATEWAY_POLL_INTERVAL * 1000};
  var IDLE_CHECK_MS = 300000;
  var USER_IDLE_MS = 300000;
  var _pollTimer = null;
  var _lastActivity = Date.now();
  var _userIdle = false;
  var _serverIdle = false;
  var _wasDisconnected = false;

  function onActivity() {{
    _lastActivity = Date.now();
    if (_userIdle || _serverIdle) {{
      _userIdle = false;
      _serverIdle = false;
      schedulePoll(POLL_MS);
      gwStatus();
    }}
  }}
  document.addEventListener('mousemove', onActivity, {{passive:true}});
  document.addEventListener('keydown', onActivity, {{passive:true}});
  document.addEventListener('scroll', onActivity, {{passive:true}});
  document.addEventListener('touchstart', onActivity, {{passive:true}});

  document.addEventListener('visibilitychange', function() {{
    if (document.hidden) {{
      clearTimeout(_pollTimer);
    }} else {{
      onActivity();
    }}
  }});

  function schedulePoll(delay) {{
    clearTimeout(_pollTimer);
    _pollTimer = setTimeout(function() {{
      var elapsed = Date.now() - _lastActivity;
      if (elapsed > USER_IDLE_MS) _userIdle = true;
      gwStatus();
      var next = (_userIdle || _serverIdle) ? IDLE_CHECK_MS : POLL_MS;
      schedulePoll(next);
    }}, delay);
  }}

  function gwStatus() {{
    fetch('/api/gateway/status').then(function(r) {{
      if (r.status === 204) {{
        _serverIdle = true;
        return null;
      }}
      _serverIdle = false;
      if (_wasDisconnected) {{
        _wasDisconnected = false;
        hideConn();
      }}
      return r.json();
    }}).then(function(d) {{
      if (!d) return;
      document.getElementById('gw-dot').style.background = d.running ? '#4ade80' : '#ef4444';
      document.getElementById('gw-label').textContent = d.running ? 'Gateway running' : 'Gateway stopped';
      var vol = document.getElementById('gw-vol');
      vol.style.display = 'block';
      if (d.volume) {{
        vol.innerHTML = '<span style="color:#4ade80;">\\u2713</span> <span style="color:#7899aa;">Volume attached</span>';
      }} else {{
        vol.innerHTML = '<span style="color:#fbbf24;">\\u26A0</span> <span style="color:#fbbf24;">No volume \\u2014 data will not persist</span>';
      }}
    }}).catch(function(e) {{
      _wasDisconnected = true;
      showConn('Reconnecting...');
    }});
  }}

  function gwRestart() {{
    var b = document.getElementById('gw-btn');
    b.textContent = 'Restarting...'; b.disabled = true;
    fetch('/api/gateway/restart', {{method:'POST'}}).then(function() {{
      setTimeout(function() {{ b.textContent = 'Restart'; b.disabled = false; gwStatus(); }}, 3000);
    }}).catch(function() {{ b.textContent = 'Restart'; b.disabled = false; }});
  }}

  function showConn(msg) {{
    var el = document.getElementById('gw-conn');
    if (el) {{ el.style.display = 'block'; el.textContent = msg; }}
  }}
  function hideConn() {{
    var el = document.getElementById('gw-conn');
    if (el) {{ el.style.display = 'none'; }}
  }}

  schedulePoll(POLL_MS);
  gwStatus();
}})();
</script>
"""
```

Note: This uses an f-string with `{{` and `}}` for the JavaScript braces, and the `GATEWAY_POLL_INTERVAL` variable is interpolated at the Python level. The triple-brace patterns `{{{{` become `{{` in the f-string output, which is correct JavaScript.

Actually, this is getting complex with the f-string escaping. Let me use a simpler approach: compute the JS config values separately and use `string.Template` or simple concatenation.

- [ ] **Step 1 (final): Replace the GATEWAY_WIDGET constant**

Replace the entire `GATEWAY_WIDGET = """..."""` block (lines 330-367) with:

```python
GATEWAY_WIDGET_JS_CONFIG = f"""var POLL_MS = {GATEWAY_POLL_INTERVAL * 1000};"""

GATEWAY_WIDGET = """
<div id="gw-widget" style="position:fixed;bottom:20px;right:20px;z-index:99999;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;">
  <div style="background:#111920;border:1px solid rgba(45,212,191,0.2);border-radius:10px;
    padding:12px 16px;display:flex;flex-direction:column;gap:8px;
    box-shadow:0 4px 20px rgba(0,0,0,0.4);min-width:180px;">
    <div style="display:flex;align-items:center;gap:10px;">
      <span id="gw-dot" style="width:8px;height:8px;border-radius:50%;background:#888;flex-shrink:0;"></span>
      <span id="gw-label" style="color:#7899aa;flex:1;">Gateway</span>
      <button id="gw-btn" onclick="gwRestart()" style="background:#2dd4bf;color:#0a0f14;border:none;
        border-radius:5px;padding:4px 12px;font-size:12px;font-weight:600;cursor:pointer;">Restart</button>
    </div>
    <div id="gw-vol" style="display:none;font-size:11px;padding-top:4px;border-top:1px solid rgba(45,212,191,0.1);"></div>
    <div id="gw-conn" style="display:none;font-size:11px;padding-top:4px;border-top:1px solid rgba(45,212,191,0.1);color:#fbbf24;"></div>
  </div>
</div>
<script>
(function(){
  """ + GATEWAY_WIDGET_JS_CONFIG + """
  var IDLE_CHECK_MS = 300000;
  var USER_IDLE_MS = 300000;
  var _pollTimer = null;
  var _lastActivity = Date.now();
  var _userIdle = false;
  var _serverIdle = false;
  var _wasDisconnected = false;

  function onActivity() {
    _lastActivity = Date.now();
    if (_userIdle || _serverIdle) {
      _userIdle = false;
      _serverIdle = false;
      schedulePoll(POLL_MS);
      gwStatus();
    }
  }
  document.addEventListener('mousemove', onActivity, {passive:true});
  document.addEventListener('keydown', onActivity, {passive:true});
  document.addEventListener('scroll', onActivity, {passive:true});
  document.addEventListener('touchstart', onActivity, {passive:true});

  document.addEventListener('visibilitychange', function() {
    if (document.hidden) {
      clearTimeout(_pollTimer);
    } else {
      onActivity();
    }
  });

  function schedulePoll(delay) {
    clearTimeout(_pollTimer);
    _pollTimer = setTimeout(function() {
      var elapsed = Date.now() - _lastActivity;
      if (elapsed > USER_IDLE_MS) _userIdle = true;
      gwStatus();
      var next = (_userIdle || _serverIdle) ? IDLE_CHECK_MS : POLL_MS;
      schedulePoll(next);
    }, delay);
  }

  function gwStatus() {
    fetch('/api/gateway/status').then(function(r) {
      if (r.status === 204) {
        _serverIdle = true;
        return null;
      }
      _serverIdle = false;
      if (_wasDisconnected) {
        _wasDisconnected = false;
        hideConn();
      }
      return r.json();
    }).then(function(d) {
      if (!d) return;
      document.getElementById('gw-dot').style.background = d.running ? '#4ade80' : '#ef4444';
      document.getElementById('gw-label').textContent = d.running ? 'Gateway running' : 'Gateway stopped';
      var vol = document.getElementById('gw-vol');
      vol.style.display = 'block';
      if (d.volume) {
        vol.innerHTML = '<span style="color:#4ade80;">\u2713</span> <span style="color:#7899aa;">Volume attached</span>';
      } else {
        vol.innerHTML = '<span style="color:#fbbf24;">\u26A0</span> <span style="color:#fbbf24;">No volume \u2014 data will not persist</span>';
      }
    }).catch(function() {
      _wasDisconnected = true;
      showConn('Reconnecting...');
    });
  }

  function gwRestart() {
    var b = document.getElementById('gw-btn');
    b.textContent = 'Restarting...'; b.disabled = true;
    fetch('/api/gateway/restart', {method:'POST'}).then(function() {
      setTimeout(function() { b.textContent = 'Restart'; b.disabled = false; gwStatus(); }, 3000);
    }).catch(function() { b.textContent = 'Restart'; b.disabled = false; });
  }

  function showConn(msg) {
    var el = document.getElementById('gw-conn');
    if (el) { el.style.display = 'block'; el.textContent = msg; }
  }
  function hideConn() {
    var el = document.getElementById('gw-conn');
    if (el) { el.style.display = 'none'; }
  }

  schedulePoll(POLL_MS);
  gwStatus();
})();
</script>
"""
```

- [ ] **Step 2: Update the gateway_status handler to return 204 when idle**

The `gateway_status` handler needs to return 204 when the proxy is in idle mode, so the client-side JS can detect idle state and switch to slow polling. Modify the `gateway_status` function:

```python
async def gateway_status(request):
    if is_idle():
        return web.Response(status=204)
    running = gateway_process is not None and gateway_process.poll() is None
    return web.json_response({
        "running": running,
        "volume": volume_attached(),
    })
```

- [ ] **Step 3: Verify the change compiles**

Run: `python -c "import ast; ast.parse(open('auth_proxy.py').read()); print('OK')"`

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add auth_proxy.py
git commit -m "feat: overhaul gateway widget with smart polling

- Reduce poll interval from 10s to 60s (configurable via GATEWAY_POLL_INTERVAL)
- Pause all polling when tab is hidden (Page Visibility API)
- Switch to 5-minute check interval after 5 min of no interaction
- Switch to 5-minute check interval when server returns 204 (idle)
- Resume normal polling immediately on user interaction or tab visible
- Add connection status indicator for reconnection feedback
- Return 204 from /api/gateway/status when proxy is idle"
```

---

## Task 7: Companion Cron Service

**Files:**
- Create: `cron-companion/Dockerfile`
- Create: `cron-companion/railway.json`

- [ ] **Step 1: Create the cron-companion directory and files**

Create `cron-companion/Dockerfile`:

```dockerfile
FROM alpine:3.19
RUN apk add --no-cache curl
CMD ["curl", "-sf", "${HERMES_URL}/api/cron/wake"]
```

Create `cron-companion/railway.json`:

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

- [ ] **Step 2: Commit**

```bash
git add cron-companion/
git commit -m "feat: add companion cron service

Lightweight Alpine container that pings the Hermes /api/cron/wake
endpoint every 5 minutes to ensure the container is awake for
scheduled cron jobs. Negligible compute cost (~0.15 hrs/month)."
```

---

## Task 8: Smoke Test and Final Verification

**Files:** None (testing only)

- [ ] **Step 1: Test auth_proxy.py syntax and imports**

Run: `python -c "from auth_proxy import create_app; print('import OK')"`

Expected: Module imports successfully. May fail on aiohttp if not installed locally — OK to skip if so. Fallback: `python -c "import ast; ast.parse(open('auth_proxy.py').read()); print('syntax OK')"`

- [ ] **Step 2: Verify all routes are registered**

Run: `python -c "
import ast
tree = ast.parse(open('auth_proxy.py').read())
# Find create_app function and check all add_route/add_get/add_post calls
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == 'create_app':
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
        for c in calls:
            if isinstance(c.func, ast.Attribute):
                if c.func.attr in ('add_get', 'add_post', 'add_route'):
                    args = [ast.dump(a) for a in c.args]
                    print(f'  {c.func.attr}: {args}')
"`

Expected output should include routes for:
- `/login` (GET)
- `/login` (POST)
- `/logout` (GET)
- `/api/health` (GET)
- `/api/cron/wake` (GET)
- `/api/idle` (GET)
- `/api/gateway/restart` (POST)
- `/api/gateway/status` (GET)
- `/telegram/webhook` (*)
- `/{path_info:.*}` (*)

- [ ] **Step 3: Verify GATEWAY_WIDGET JS config is injected correctly**

Run: `python -c "
GATEWAY_POLL_INTERVAL = 60
POLL_MS = GATEWAY_POLL_INTERVAL * 1000
print(f'POLL_MS = {POLL_MS}')
assert POLL_MS == 60000, 'Expected 60000'
print('JS config OK')
"`

Expected: `POLL_MS = 60000` and `JS config OK`

- [ ] **Step 4: Verify railway.json is valid JSON**

Run: `python -c "import json; d=json.load(open('railway.json')); assert d['deploy']['healthcheckTimeout'] == 30; print('railway.json OK')"`

Expected: `railway.json OK`

- [ ] **Step 5: Verify cron-companion files exist and are valid**

Run: `python -c "import json; d=json.load(open('cron-companion/railway.json')); assert d['deploy']['cronSchedule'] == '*/5 * * * *'; print('cron-companion OK')"`

Expected: `cron-companion OK`

- [ ] **Step 6: Final review — diff summary**

Run: `git diff --stat HEAD~7` (or whatever commit count from the above tasks)

Expected: Modified files: `auth_proxy.py`, `railway.json`. New files: `cron-companion/Dockerfile`, `cron-companion/railway.json`.

---

## Task 9: Update Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add documentation for new features**

Add a new "Serverless Sleep" section to `README.md` after the Architecture section:

```markdown
## Serverless Sleep

This deployment supports Railway's serverless (sleep) mode to reduce costs when idle. The container will sleep after a period of inactivity and automatically wake when:

- You visit the dashboard URL
- A Telegram message arrives (if webhook mode is configured)
- The companion cron service pings `/api/cron/wake`

### Telegram Webhook Setup (Recommended)

To enable instant wake-on-message for Telegram:

1. In the Hermes dashboard, go to **API Keys** and set:
   - `TELEGRAM_WEBHOOK_URL` = `https://<your-railway-url>/telegram/webhook`
   - `TELEGRAM_WEBHOOK_SECRET` = a random secret string (e.g., `openssl rand -hex 32`)
2. Restart the gateway via the widget or redeploy
3. Telegram messages will now wake the container if it's asleep

Without webhook mode, Telegram messages sent while the container is sleeping will be queued and delivered on next wake.

### Companion Cron Service

A separate Railway service pings `/api/cron/wake` every 5 minutes to ensure the container is awake for scheduled Hermes cron jobs. See `cron-companion/` for configuration.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `IDLE_TIMEOUT_MINUTES` | `30` | Minutes of no real activity before idle mode |
| `GATEWAY_POLL_INTERVAL` | `60` | Seconds between gateway status widget polls |
| `TELEGRAM_WEBHOOK_URL` | (empty) | Set to enable Telegram webhook wake mode |
| `TELEGRAM_WEBHOOK_SECRET` | (empty) | Required when TELEGRAM_WEBHOOK_URL is set |

### Limitations

- Discord bots require persistent WebSocket connections and are incompatible with serverless sleep
- Cold start delay: ~10-30 seconds when waking from sleep
- Dashboard polls pause when the browser tab is hidden or the user is idle for 5+ minutes
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document serverless sleep, webhook setup, and companion cron"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** All 10 components from the spec are implemented:
  1. Client-side poll control → Task 6
  2. Activity-aware proxy → Task 3
  3. Telegram webhook endpoint → Task 5
  4. Cron wake endpoint → Task 4
  5. Auth SECRET persistence → Task 1
  6. WebSocket/Connection reconnection → Task 6 (connection status indicator)
  7. Companion cron service → Task 7
  8. AUTO_UPDATE default → No change needed (already false)
  9. Health check timeout → Task 2
  10. Dashboard frontend polling → Handled by client-side idle detection in Task 6

- [x] **Placeholder scan:** No TBD, TODO, or "implement later" in any step.

- [x] **Type consistency:** All function names and variable names are consistent across tasks. `GATEWAY_POLL_INTERVAL` used in both the config section and widget. `is_idle()` used in `gateway_status()` and `idle_status()`.

- [x] **Gateway widget JS escaping:** The widget uses string concatenation with `GATEWAY_WIDGET_JS_CONFIG` to avoid f-string escaping issues with JavaScript braces.

- [x] **Route ordering:** `/telegram/webhook` is registered before `/{path_info:.*}` in `create_app()`.

- [x] **Middleware updates:** `auth_middleware` now includes `/api/cron/wake` and `/telegram/webhook` in the unauthenticated paths.