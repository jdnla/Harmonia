#!/usr/bin/env bash
# Thin wrapper so irexec (and anything else) can invoke the launcher with the
# correct interpreter and module path. install.sh points irexec's config lines
# at the installed copy of this script.
#
# Usage: launch_app.sh <app>   e.g. launch_app.sh plex
set -euo pipefail

# INSTALL_DIR is replaced by install.sh; fall back to the repo layout for dev.
INSTALL_DIR="${AVR_TV_BRIDGE_DIR:-/opt/avr-tv-bridge}"
PYTHON="$INSTALL_DIR/venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

exec "$PYTHON" "$INSTALL_DIR/src/launcher.py" "$@"
