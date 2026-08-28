#!/usr/bin/env bash
# Fast D-pad key injector for irexec. Calls ydotool directly (no Python) to keep
# per-press latency low — important because held-button IR repeats arrive quickly
# and any per-press overhead makes them queue up and fire in bursts.
# Usage: navkey.sh <button>   e.g. navkey.sh up
set -euo pipefail

: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
export YDOTOOL_SOCKET="${YDOTOOL_SOCKET:-$XDG_RUNTIME_DIR/.ydotool_socket}"
YDOTOOL="$(command -v ydotool || echo /usr/local/bin/ydotool)"

# Debounce (QUIET-GAP): this remote's USB-UIRT/lircd decode emits one physical
# press as a burst of 2-5 identical frames ~200ms apart (verified via capture).
# A "measure from last accepted" debounce leaked doubles when a burst outran the
# window. Instead we require a QUIET GAP: accept a press only if it's been at
# least DEBOUNCE_MS since the LAST FRAME SEEN for this button — and we update the
# stamp on EVERY frame (accepted or dropped). That way any-length ~200ms burst
# collapses to a single fire, and a deliberate repeat only registers once you
# pause > DEBOUNCE_MS. Default 300ms > the ~200ms burst spacing, < a normal
# deliberate re-press. Tune via NAVKEY_DEBOUNCE_MS.
# NOTE: this makes held-button auto-repeat into press-to-repeat (each frame
# inside a hold is <gap so it's coalesced) — acceptable trade for no doubles.
DEBOUNCE_MS="${NAVKEY_DEBOUNCE_MS:-300}"
_btn="${1:-}"
# Milliseconds since epoch. NOTE: `date +%s%3N` is NOT reliable here — on this
# system %3N does not truncate to 3 digits, it yields full nanoseconds appended
# (a 19-digit value), which silently broke the debounce (diffs never < window).
# Compute ms from seconds + nanoseconds explicitly instead.
_ns=$(date +%s%N)                # e.g. 1785254437568608694 (sec + 9-digit ns)
_now=$(( _ns / 1000000 ))        # -> integer milliseconds
_stamp="$XDG_RUNTIME_DIR/.navkey_${_btn}"
_accept=1
if [ -f "$_stamp" ]; then
  _last=$(cat "$_stamp" 2>/dev/null || echo 0)
  if [ $(( _now - _last )) -lt "$DEBOUNCE_MS" ]; then
    _accept=0   # within the burst window — coalesce (drop this frame)
  fi
fi
# Update the stamp on EVERY frame so the quiet-gap is measured from the most
# recent frame, not the last accepted press (this is what collapses long bursts).
printf '%s' "$_now" > "$_stamp"
[ "$_accept" = "1" ] || exit 0

# Cross-button ghost guard: this remote also emits a trailing repeat frame of a
# NEARBY press attributed to a DIFFERENT button ~180ms later (verified: e.g.
# "up" then a phantom right/up ~178-184ms apart). The per-button debounce can't
# catch that. So also drop ANY accepted key that lands within GLOBAL_MS of the
# last ACCEPTED key regardless of button. A deliberate two-direction press is
# always slower than this (~400ms+ observed), so real input passes; only the
# ~180ms ghost is filtered. Only enforced for a DIFFERENT button (same-button
# repeats are already handled above, and holding one direction should repeat).
GLOBAL_MS="${NAVKEY_GLOBAL_MS:-230}"
_gstamp="$XDG_RUNTIME_DIR/.navkey_global"
if [ -f "$_gstamp" ]; then
  # Read "ms btn" via word-splitting (robust even without a trailing newline;
  # `read < file` returned empty here because printf wrote no newline).
  _g=$(cat "$_gstamp" 2>/dev/null)
  _glast=${_g%% *}; _gbtn=${_g##* }
  case "$_glast" in ''|*[!0-9]*) _glast=0 ;; esac
  if [ "$_gbtn" != "$_btn" ] && [ $(( _now - _glast )) -lt "$GLOBAL_MS" ]; then
    # A different button within GLOBAL_MS of the last accepted key: this remote
    # echoes the previous direction ~180ms after every new press (verified). Drop
    # it. Do NOT update the global stamp on a drop, so a run of phantoms after
    # one real press all get filtered (they keep measuring from the real press).
    exit 0
  fi
fi
printf '%s %s\n' "$_now" "$_btn" > "$_gstamp"

# Logical button -> Linux input keycode (see linux/input-event-codes.h)
case "${1:-}" in
  up)              CODE=103 ;;
  down)            CODE=108 ;;
  left)            CODE=105 ;;
  right)           CODE=106 ;;
  ok|select|enter) CODE=28  ;;
  back|exit|esc)   CODE=1   ;;
  menu)            CODE=139 ;;
  home)            CODE=172 ;;   # KEY_HOMEPAGE / XF86HomePage (Bigscreen Home; NOT 102=KEY_HOME)
  playpause|play|pause) CODE=164 ;;
  *) echo "navkey.sh: unknown button '${1:-}'" >&2; exit 2 ;;
esac

exec "$YDOTOOL" key "${CODE}:1" "${CODE}:0"
