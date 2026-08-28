#!/usr/bin/env bash
#
# install-launcher.sh — set up the app-launcher half of avr-tv-bridge on a
# KDE Plasma (Wayland) machine: launch-or-focus GUI apps from IR remote buttons
# and/or Home Assistant.
#
# This is separate from install.sh (the system-level AVR/TV bridge) because the
# launcher must run in the USER graphical session, not as a system service.
#
# Idempotent. Run as your normal user (NOT root) — it installs a --user service.
#
#   ./install-launcher.sh            # full launcher setup
#   ./install-launcher.sh --no-ir    # skip irexec/IR wiring (HA/MQTT only)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/avr-tv-bridge"
CONFIG_DIR="$HOME/.config/avr-tv-bridge"
DO_IR=1
for a in "$@"; do case "$a" in --no-ir) DO_IR=0;; *) echo "unknown: $a">&2; exit 2;; esac; done

# Navigation input source (D-pad/OK/Back): "cec" (default) or "ir". Read from
# the user config if present so the installer wires the matching path.
NAV_MODE="cec"
if [ -f "$CONFIG_DIR/config.env" ]; then
  NAV_MODE="$(grep -E '^NAV_MODE=' "$CONFIG_DIR/config.env" | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
  [ -z "$NAV_MODE" ] && NAV_MODE="cec"
fi

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$EUID" -eq 0 ] && die "Run as your normal user, not root (this installs a --user service)."

# ---- 1. environment sanity checks (portability) --------------------------
log "Checking environment"
[ "${XDG_SESSION_TYPE:-}" = "wayland" ] || warn "Session is not Wayland (found '${XDG_SESSION_TYPE:-unknown}'). kdotool targets KDE; X11 may need xdotool instead."
if ! pgrep -u "$USER" -x plasmashell >/dev/null 2>&1 && ! pgrep -u "$USER" -x kwin_wayland >/dev/null 2>&1; then
  warn "KDE Plasma (plasmashell/kwin_wayland) not detected running. The launcher relies on KWin scripting via kdotool."
fi

# ---- 2. required tools ---------------------------------------------------
log "Checking required tools"
MISSING=()
need() { command -v "$1" >/dev/null 2>&1 || MISSING+=("$1 ($2)"); }
need kdotool "window focus on KDE Wayland — https://github.com/jinliu/kdotool"
need python3 "python runtime"
# playerctl drives media play/pause/next/prev via MPRIS (Chrome/Netflix, browsers)
# with real playing/paused state; without it, media control falls back to key
# injection only. Install if missing.
if ! command -v playerctl >/dev/null 2>&1; then
  log "Installing playerctl (media transport control)"
  sudo apt-get install -y -qq playerctl 2>/dev/null || warn "could not install playerctl — media status/control limited to key injection"
fi
# swayidle drives the inactivity auto-power-off (idle_off.py). It watches the
# Wayland idle protocol (ext_idle_notifier_v1 / org_kde_kwin_idle) for
# mouse/keyboard/remote inactivity. Optional — idle-off just won't run without it.
if ! command -v swayidle >/dev/null 2>&1; then
  log "Installing swayidle (inactivity auto-power-off)"
  sudo apt-get install -y -qq swayidle 2>/dev/null || warn "could not install swayidle — idle auto-power-off disabled"
fi
# The camera popup is a Wayland layer-shell overlay: a small QML app (qml6) using
# org.kde.layershell + QtMultimedia. Needed for the on-detection/remote-0 camera
# popup to position itself in a corner above fullscreen apps (works under Plasma
# Bigscreen, which forces normal windows to maximized/non-movable).
if ! command -v qml6 >/dev/null 2>&1; then
  log "Installing qml-qt6 (qml6 runtime for the camera popup)"
  sudo apt-get install -y -qq qml-qt6 2>/dev/null \
    || warn "could not install qml6 (qml-qt6) — camera popup disabled"
fi
# QML modules the popup imports (QtQuick, QtMultimedia, layer-shell).
for pkg in qml6-module-qtquick qml6-module-qtquick-window qml6-module-qtquick-effects \
           qml6-module-qtmultimedia layer-shell-qt qml6-module-org-kde-layershell; do
  dpkg -s "$pkg" >/dev/null 2>&1 || sudo apt-get install -y -qq "$pkg" 2>/dev/null \
    || warn "camera popup needs $pkg (install it if the popup does not appear)"
done
# App launchers referenced by the registry — warn (not fail) if missing, since
# the set of apps is configurable.
for app in google-chrome snap; do
  command -v "$app" >/dev/null 2>&1 || warn "optional: '$app' not found (needed by some app registry entries)"
done
if [ "$DO_IR" -eq 1 ]; then
  need irexec "IR button handling — part of the 'lirc' package"
fi
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo; die "Missing required tools:$(printf '\n  - %s' "${MISSING[@]}")"
fi

# ---- 3. install code + venv ----------------------------------------------
# Reuse the system install dir if present; else create a user-owned one.
if [ ! -d "$INSTALL_DIR" ]; then
  log "Creating $INSTALL_DIR"
  sudo mkdir -p "$INSTALL_DIR" && sudo chown "$USER":"$USER" "$INSTALL_DIR"
fi
log "Syncing source"
mkdir -p "$INSTALL_DIR/src"
cp -f "$REPO_DIR"/src/*.py "$INSTALL_DIR/src/"
cp -f "$REPO_DIR"/src/*.qml "$INSTALL_DIR/src/" 2>/dev/null || true
install -m 755 "$REPO_DIR/src/launch_app.sh" "$INSTALL_DIR/src/launch_app.sh"
install -m 755 "$REPO_DIR/src/navkey.sh" "$INSTALL_DIR/src/navkey.sh" 2>/dev/null || true
install -m 755 "$REPO_DIR/src/mqtt_pub.sh" "$INSTALL_DIR/src/mqtt_pub.sh" 2>/dev/null || true
if [ ! -d "$INSTALL_DIR/venv" ]; then
  log "Creating venv"
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" -q install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" -q install -r "$REPO_DIR/requirements.txt"

# ---- 4. config -----------------------------------------------------------
install -d -m 700 "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.env" ]; then
  install -m 600 "$REPO_DIR/config.env.example" "$CONFIG_DIR/config.env"
  warn "Created $CONFIG_DIR/config.env — edit MQTT settings before starting the service."
fi

# ---- 5. launcher user service (MQTT/HA) ----------------------------------
log "Installing launcher --user service"
mkdir -p "$HOME/.config/systemd/user"
install -m 644 "$REPO_DIR/systemd/avr-tv-bridge-launcher.service" \
  "$HOME/.config/systemd/user/avr-tv-bridge-launcher.service"
systemctl --user daemon-reload
systemctl --user enable avr-tv-bridge-launcher.service
# Allow the user service to run without an active login (so it survives logout).
sudo loginctl enable-linger "$USER" 2>/dev/null || warn "could not enable linger (service only runs while logged in)"
systemctl --user restart avr-tv-bridge-launcher.service || warn "launcher service failed to start — check: systemctl --user status avr-tv-bridge-launcher"

# ---- 5b. D-pad key injection (ydotool built from source + ydotoold service) --
# The packaged ydotool 0.1.8 `key` command is broken (no events emitted), so we
# build a working ydotool from source. Required for D-pad navigation (navkey).
log "Building ydotool from source (packaged version's key command is broken)"
bash "$REPO_DIR/setup/build_ydotool.sh" || warn "ydotool build failed — D-pad nav won't work until fixed"

log "Installing ydotoold --user service"
# Some distros (e.g. Ubuntu 26.04) ship a packaged user unit
# /usr/lib/systemd/user/ydotool.service that auto-starts the PACKAGED
# /usr/bin/ydotoold (whose `key` command is broken) — it competes with our
# source-built daemon over /dev/uinput and breaks injection. Mask it so only
# our ydotoold runs.
if systemctl --user list-unit-files ydotool.service >/dev/null 2>&1; then
  log "Masking packaged ydotool.service (conflicts with our ydotoold)"
  systemctl --user mask ydotool.service 2>/dev/null || true
fi
install -m 644 "$REPO_DIR/systemd/ydotoold.service" \
  "$HOME/.config/systemd/user/ydotoold.service"
systemctl --user daemon-reload
systemctl --user enable ydotoold.service
systemctl --user restart ydotoold.service || warn "ydotoold failed to start (check /dev/uinput access — user must be able to open it)"

# ---- 5c. CEC D-pad navigation service ------------------------------------
# The TV forwards D-pad/OK/Back over CEC; cec_nav reads them (as the `video`
# group, no root) and injects via ydotool. This is the primary navigation path
# (CEC decodes cleanly; the IR path had unreliable NEC repeat frames). Needs
# cec-utils (cec-follower) and the user in the `video` group.
if [ "$NAV_MODE" = "cec" ]; then
  if command -v cec-follower >/dev/null 2>&1; then
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx video; then
      warn "user '$USER' is not in the 'video' group — cec-follower can't read /dev/cec0. Run: sudo usermod -aG video $USER (then re-login)."
    fi
    log "Installing CEC D-pad navigation --user service (NAV_MODE=cec)"
    install -m 644 "$REPO_DIR/systemd/avr-tv-bridge-cecnav.service" \
      "$HOME/.config/systemd/user/avr-tv-bridge-cecnav.service"
    systemctl --user daemon-reload
    systemctl --user enable avr-tv-bridge-cecnav.service
    systemctl --user restart avr-tv-bridge-cecnav.service || warn "cecnav failed to start — check: systemctl --user status avr-tv-bridge-cecnav"
    # Guard against a CONFLICTING system-wide irexec injecting the D-pad over IR
    # in parallel with CEC (causes double-presses + cross-button ghosting). A
    # stock lirc install ships a system irexec.service reading
    # /etc/lirc/irexec.lircrc; if that file maps D-pad/OK keys, disable the
    # system service (our launcher runs its OWN --user irexec for launch buttons).
    if systemctl is-enabled irexec.service >/dev/null 2>&1 || systemctl is-active irexec.service >/dev/null 2>&1; then
      if grep -qiE 'KEY_(UP|DOWN|LEFT|RIGHT|OK|BACK)' /etc/lirc/irexec.lircrc 2>/dev/null; then
        warn "A system-wide irexec.service maps the D-pad over IR — it will DOUBLE every press alongside CEC. Disabling it (NAV_MODE=cec)."
        sudo systemctl disable --now irexec.service 2>/dev/null \
          || warn "could not disable system irexec.service — run: sudo systemctl disable --now irexec.service"
      fi
    fi
  else
    warn "cec-follower not found (install cec-utils) — CEC D-pad navigation disabled"
  fi
else
  # NAV_MODE=ir: ignore CEC for nav — stop/disable cecnav so it doesn't
  # double-fire with the IR D-pad mappings.
  log "NAV_MODE=ir: disabling CEC nav (D-pad/OK come from IR instead)"
  systemctl --user disable --now avr-tv-bridge-cecnav.service 2>/dev/null || true
fi

# ---- 5d. Bigscreen homescreen tile overrides -----------------------------
# On a Plasma Bigscreen (or plain KDE) homescreen, app tiles come from .desktop
# files. A snap's own tile (e.g. plex-htpc_plex-htpc.desktop) runs the app
# directly with no launch-or-focus, so tapping it while the app is already open
# spawns a DUPLICATE window. We shadow such tiles with a user-level .desktop of
# the SAME basename (user XDG dir wins over /var/lib/snapd/desktop) whose Exec
# routes through our launcher, which raises the existing window instead.
# Idempotent: rewritten every run. Only created if the snap tile exists.
# install_tile_override <src_tile_path> <basename> <app_key> <name> <icon> <categories> [wmclass]
# Shadows a stock app tile (<src_tile_path>) with a user-level .desktop of the
# same <basename> whose Exec routes through our launcher (launch-or-focus).
# Only acts if the stock tile exists. <icon> may be a glob (first match used) or
# a plain icon name/path.
install_tile_override() {
  local src_tile="$1" basename="$2" app_key="$3" name="$4" icon="$5" cats="$6" wmclass="${7:-}"
  [ -f "$src_tile" ] || { warn "tile $src_tile not found — skipping $app_key homescreen override"; return; }
  case "$icon" in
    */*) icon="$(ls $icon 2>/dev/null | head -1)";;
  esac
  local dest="$HOME/.local/share/applications/$basename"
  mkdir -p "$HOME/.local/share/applications"
  {
    echo "[Desktop Entry]"
    echo "# User override shadowing $src_tile."
    echo "# Routes launch through avr-tv-bridge's launcher (launch-or-focus:"
    echo "# raises the existing window instead of spawning a new one)."
    echo "# Regenerated by install-launcher.sh — edit the APPS registry in"
    echo "# src/launcher.py, not here."
    echo "Type=Application"
    echo "Name=$name"
    echo "Exec=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/src/launcher.py $app_key"
    echo "Icon=$icon"
    echo "Terminal=false"
    echo "Categories=$cats"
    [ -n "$wmclass" ] && echo "StartupWMClass=$wmclass"
  } > "$dest"
  log "Installed homescreen tile override: $name -> launcher ($app_key)"
}
log "Installing Bigscreen homescreen tile overrides (launch-or-focus)"
install_tile_override "/var/lib/snapd/desktop/applications/plex-htpc_plex-htpc.desktop" \
  "plex-htpc_plex-htpc.desktop" "plex" "Plex HTPC" \
  "/snap/plex-htpc/current/meta/gui/icon.png" "AudioVideo;Player;" "Plex HTPC"
# Browsers: the stock tiles open a NEW window on every click; route them through
# the launcher for launch-or-focus.
install_tile_override "/var/lib/snapd/desktop/applications/firefox_firefox.desktop" \
  "firefox_firefox.desktop" "firefox" "Firefox" \
  "/snap/firefox/current/default256.png" "Network;WebBrowser;" "firefox_firefox"
install_tile_override "/usr/share/applications/google-chrome.desktop" \
  "google-chrome.desktop" "chrome" "Google Chrome" \
  "google-chrome" "Network;WebBrowser;" "google-chrome"
install_tile_override "/var/lib/snapd/desktop/applications/iptvnator_iptvnator.desktop" \
  "iptvnator_iptvnator.desktop" "iptvnator" "IPTVnator" \
  "/snap/iptvnator/current/meta/gui/icon.png" "AudioVideo;Player;" "iptvnator"
# Remove a dead/stale command shortcut that pointed at an archived script and
# an unexpandable ~ path (harmless but confusing; safe to delete if present).
DEAD_TILE="$HOME/.local/share/applications/net.local.startplex.desktop"
if [ -f "$DEAD_TILE" ] && grep -q 'dotfiles/bin/startplex' "$DEAD_TILE" 2>/dev/null \
   && [ ! -x "$HOME/dotfiles/bin/startplex" ]; then
  log "Removing dead startplex shortcut ($DEAD_TILE)"
  rm -f "$DEAD_TILE"
fi
# Chrome --app "web apps" (Netflix, Sportsnet) have no native .desktop. For each
# we install TWO files:
#  1. a visible homescreen tile (avr-<key>.desktop) routing through the launcher;
#  2. an icon-association shim named EXACTLY after the window's Wayland app_id
#     (chrome-<host>__-Default.desktop). KWin/the task switcher looks up a
#     .desktop of that name to find the window's icon; without it, alt-tab shows
#     a generated letter tile (e.g. "W" from www.netflix.com). The shim is
#     NoDisplay=true so it adds no second tile, and carries StartupWMClass so the
#     match is robust. Both point at the same bundled artwork.
# install_chrome_app_tiles <app_key> <name> <app_id> <icon_src> <icon_dest_name>
install_chrome_app_tiles() {
  local app_key="$1" name="$2" app_id="$3" icon_src="$4" icon_name="$5"
  local apps="$HOME/.local/share/applications"
  local icon="$HOME/.local/share/icons/$icon_name"
  mkdir -p "$apps" "$HOME/.local/share/icons"
  install -m 644 "$icon_src" "$icon" 2>/dev/null \
    || warn "could not install $name icon ($icon_src) — tile will use a fallback"
  # 1. visible tile
  cat > "$apps/avr-$app_key.desktop" <<TILE
[Desktop Entry]
# $name tile -> avr-tv-bridge launcher (launch-or-focus, fullscreen Chrome
# --app). Regenerated by install-launcher.sh.
Type=Application
Name=$name
Exec=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/src/launcher.py $app_key
Icon=$icon
Terminal=false
Categories=AudioVideo;Player;
TILE
  # 2. icon-association shim named after the runtime app_id (fixes alt-tab icon)
  cat > "$apps/$app_id.desktop" <<TILE
[Desktop Entry]
# Icon-association shim for the $name Chrome --app window. Its Wayland app_id is
# $app_id; KWin/the task switcher looks up a .desktop of that
# exact name to resolve the window's icon. Without it, alt-tab shows a generated
# letter tile. NoDisplay so it adds no homescreen tile (the visible one is
# avr-$app_key.desktop). Regenerated by install-launcher.sh.
Type=Application
Name=$name
Icon=$icon
Exec=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/src/launcher.py $app_key
StartupWMClass=$app_id
NoDisplay=true
Terminal=false
TILE
  log "Installed $name tile + icon-association shim ($app_id)"
}
install -d "$HOME/.local/share/icons"
install_chrome_app_tiles netflix "Netflix" "chrome-www.netflix.com__-Default" \
  "$REPO_DIR/assets/avr-netflix.svg" "avr-netflix.svg"
install_chrome_app_tiles sportsnet "Sportsnet" "chrome-watch.sportsnet.ca__-Default" \
  "$REPO_DIR/assets/avr-sportsnet.png" "avr-sportsnet.png"

command -v update-desktop-database >/dev/null 2>&1 \
  && update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

# ---- 5e. Audio: keep the ALSA sink active (no idle-suspend) --------------
# WirePlumber suspends idle audio sinks after ~5s; resuming on the next play
# costs ~1-2s before sound. Install a drop-in that disables idle-suspend so
# audio starts instantly. Harmless if PipeWire/WirePlumber isn't used.
if [ -f "$REPO_DIR/wireplumber/50-disable-suspend.conf" ]; then
  log "Installing WirePlumber no-suspend drop-in (instant audio)"
  install -d "$HOME/.config/wireplumber/wireplumber.conf.d"
  install -m 644 "$REPO_DIR/wireplumber/50-disable-suspend.conf" \
    "$HOME/.config/wireplumber/wireplumber.conf.d/50-disable-suspend.conf"
  systemctl --user restart wireplumber 2>/dev/null \
    || warn "could not restart wireplumber (config will apply next login)"
fi

# ---- 5f. Audio: upmix stereo across all AVR speakers ---------------------
# Pairs with the channel-sync feature (channel_sync.py). It sets the HDMI sink
# to the receiver's max surround profile; this drop-in makes PipeWire synthesize
# center/LFE/rears from stereo so no speaker sits silent on 2.0 content.
if [ -f "$REPO_DIR/wireplumber/51-hdmi-upmix.conf" ]; then
  log "Installing WirePlumber HDMI upmix drop-in (fill all speakers on stereo)"
  install -d "$HOME/.config/wireplumber/wireplumber.conf.d"
  install -m 644 "$REPO_DIR/wireplumber/51-hdmi-upmix.conf" \
    "$HOME/.config/wireplumber/wireplumber.conf.d/51-hdmi-upmix.conf"
  systemctl --user restart wireplumber 2>/dev/null \
    || warn "could not restart wireplumber (config will apply next login)"
fi

# ---- 6. IR / irexec ------------------------------------------------------
if [ "$DO_IR" -eq 1 ]; then
  log "Installing irexec config (IR buttons -> launcher + D-pad nav)"
  # Render the lircrc with the absolute wrapper paths (launch + nav). Include
  # the IR D-pad block only for NAV_MODE=ir; strip it (DPAD_IR_BEGIN..END) for
  # cec mode so nav isn't double-fired.
  _lircrc_src="$REPO_DIR/lirc/irexec.lircrc"
  if [ "$NAV_MODE" = "ir" ]; then
    log "NAV_MODE=ir: mapping D-pad/OK/Back over IR"
    _lircrc_body="$(cat "$_lircrc_src")"
  else
    _lircrc_body="$(sed '/# DPAD_IR_BEGIN/,/# DPAD_IR_END/d' "$_lircrc_src")"
  fi
  printf '%s\n' "$_lircrc_body" | sed \
      -e "s|LAUNCH_CMD|$INSTALL_DIR/src/launch_app.sh|g" \
      -e "s|NAVKEY_CMD|$INSTALL_DIR/src/navkey.sh|g" \
      -e "s|MQTTPUB_CMD|$INSTALL_DIR/src/mqtt_pub.sh|g" \
    > "$HOME/.config/lircrc"
  # irexec as a --user service so it shares the session env for launching apps.
  cat > "$HOME/.config/systemd/user/irexec.service" <<UNIT
[Unit]
Description=irexec — run commands on IR remote button presses
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/irexec $HOME/.config/lircrc
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable irexec.service
  systemctl --user restart irexec.service || warn "irexec failed to start — check lircd is running and the remote config is loaded"
fi

cat <<EOF

$(log "Launcher install complete.")
  - Edit MQTT settings if needed: $CONFIG_DIR/config.env
  - Launcher service:  systemctl --user status avr-tv-bridge-launcher
  - IR button service: systemctl --user status irexec
  - Test a launch:     $INSTALL_DIR/src/launch_app.sh plex
  - In Home Assistant: look for "Launch Plex" / "Launch Netflix" buttons.

Apps are defined in src/launcher.py (APPS registry). Add more there.
EOF
