#!/usr/bin/env bash

# Internal port for Hermes dashboard (never exposed externally).
# Must differ from Railway's PORT env var to avoid port collision.
INTERNAL_DASHBOARD_PORT=9199

AUTO_UPDATE="${AUTO_UPDATE:-false}"

if [ "$AUTO_UPDATE" = "true" ]; then
  echo "Checking for Hermes updates..."
  cd /opt/hermes-agent
  if git pull --recurse-submodules 2>&1 | grep -v 'Already up to date'; then
    echo "Updating dependencies..."
    VIRTUAL_ENV=/opt/hermes-agent/venv uv pip install -e ".[all,messaging]" --quiet
    echo "Update complete."
  else
    echo "Already up to date."
  fi
fi

# Ensure HERMES_HOME directories exist
mkdir -p /root/.hermes/{cron,sessions,logs,memories,skills,pairing,hooks,image_cache,audio_cache}

# Ensure config files exist
if [ ! -f /root/.hermes/config.yaml ]; then
  if [ -f /opt/hermes-agent/cli-config.yaml.example ]; then
    cp /opt/hermes-agent/cli-config.yaml.example /root/.hermes/config.yaml
  fi
fi
touch /root/.hermes/.env

# Export internal port for auth_proxy.py to use as upstream
export HERMES_DASHBOARD_PORT="$INTERNAL_DASHBOARD_PORT"

echo "Starting Hermes dashboard on internal port $INTERNAL_DASHBOARD_PORT..."
hermes dashboard --host 127.0.0.1 --port "$INTERNAL_DASHBOARD_PORT" --no-open 2>&1 &

echo "Starting auth proxy on port ${PORT:-8080}..."
exec python /auth_proxy.py