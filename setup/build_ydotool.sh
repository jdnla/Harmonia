#!/usr/bin/env bash
#
# build_ydotool.sh — build ydotool + ydotoold from source and install to
# /usr/local/bin.
#
# WHY: the Ubuntu-packaged ydotool 0.1.8's `key` command is broken — it emits
# no kernel events, so arrow keys / Enter never reach apps (only `type` works).
# The current upstream source builds a working `key`. This is required for the
# D-pad navigation (navkey.py) to function.
#
# Idempotent: skips the build if a working /usr/local/bin/ydotool is already
# present (unless --force).
set -euo pipefail

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Already installed from source? (packaged one lives in /usr/bin)
if [ "$FORCE" -eq 0 ] && [ -x /usr/local/bin/ydotool ] && [ -x /usr/local/bin/ydotoold ]; then
  log "ydotool already built in /usr/local/bin — skipping (use --force to rebuild)"
  exit 0
fi

log "Checking build dependencies"
MISSING=()
for t in cmake make gcc g++ git; do command -v "$t" >/dev/null 2>&1 || MISSING+=("$t"); done
if [ "${#MISSING[@]}" -gt 0 ]; then
  log "Installing build deps: ${MISSING[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y -qq cmake make gcc g++ git
fi

SRC=$(mktemp -d)
trap 'rm -rf "$SRC"' EXIT
log "Cloning ydotool"
git clone --depth 1 https://github.com/ReimuNotMoe/ydotool.git "$SRC/ydotool" 2>&1 | tail -1

log "Building (docs off — avoids scdoc dependency)"
mkdir -p "$SRC/ydotool/build"
cd "$SRC/ydotool/build"
cmake -DCMAKE_BUILD_TYPE=Release -DBUILD_DOCS=OFF .. >/dev/null
make -j"$(nproc)" >/dev/null
[ -x ydotool ] && [ -x ydotoold ] || die "build did not produce binaries"

log "Installing to /usr/local/bin"
sudo install -m755 ydotool  /usr/local/bin/ydotool
sudo install -m755 ydotoold /usr/local/bin/ydotoold

# Verify the packaged (broken) daemon isn't shadowing ours via an active service.
if systemctl --user is-enabled ydotoold.service >/dev/null 2>&1; then
  log "Restarting user ydotoold.service with the new binary"
  systemctl --user restart ydotoold.service 2>/dev/null || true
fi

log "Done: $(/usr/local/bin/ydotool --version 2>/dev/null || echo ydotool installed)"
