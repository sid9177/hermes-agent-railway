#!/bin/sh
# Apply patches for Hermes Agent Railway wrapper
#
# These patches fix the model picker to show all providers (not just
# authenticated ones) and allow API key entry for unauthenticated ones.
#
# In the Docker build, patches are applied directly via the Dockerfile.
# This script exists for local testing / manual application.

set -e

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo /opt/hermes-agent)"

echo "Applying web_server_model_options.patch..."
patch -p1 --no-backup-if-diff < patches/web_server_model_options.patch

echo "Applying model_picker_dialog.patch..."
patch -p1 --no-backup-if-diff < patches/model_picker_dialog.patch

echo "All patches applied successfully."
echo ""
echo "To apply these changes, rebuild the web frontend:"
echo "  cd web && npm install && npm run build"