#!/usr/bin/env bash
# register_hotkeys.sh — keyboard hotkeys to power the media center on/off.
#
# Creates two KDE "command" global shortcuts that publish the Media Center
# On/Off scene over MQTT (same scene the HA buttons / remote use):
#     Meta+F11  ->  Media Center On
#     Meta+F12  ->  Media Center Off
#
# How it works: KDE runs a command from a global shortcut via a .desktop file
# with X-KDE-GlobalAccel-CommandShortcut plus an entry in kglobalshortcutsrc.
# On this Plasma 6 Wayland session KWin owns global shortcuts and only scans
# command-shortcuts at LOGIN — so this script ALSO registers them live over the
# kglobalaccel D-Bus API (doRegister + setShortcut) when a session is present,
# so the keys work immediately without a re-login.
#
# Idempotent. Override keys/paths via env: HOTKEY_ON, HOTKEY_OFF (KDE key
# strings), INSTALL_DIR.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/avr-tv-bridge}"
MQ="$INSTALL_DIR/src/mqtt_pub.sh"
APPS="$HOME/.local/share/applications"
KG="$HOME/.config/kglobalshortcutsrc"
HOTKEY_ON="${HOTKEY_ON:-Meta+F11}"
HOTKEY_OFF="${HOTKEY_OFF:-Meta+F12}"

# Qt key ints for the live D-Bus registration. Meta modifier = 0x10000000.
#   F11 = 0x01000038, F12 = 0x01000039
KEYINT_ON="${KEYINT_ON:-285212728}"    # Meta+F11
KEYINT_OFF="${KEYINT_OFF:-285212729}"  # Meta+F12

mkdir -p "$APPS"

_desktop() {  # name, comment, icon, topic
  cat <<D
[Desktop Entry]
Type=Application
Name=$1
Comment=$2
Exec=$MQ $4 PRESS
Icon=$3
NoDisplay=true
X-KDE-GlobalAccel-CommandShortcut=true
D
}
_desktop "Harmonia: Media Center On"  "Power on the AVR + TV (Media Center On scene)"  \
         television-play "avr-tv-bridge/scene/media_center_on/set"  > "$APPS/harmonia-power-on.desktop"
_desktop "Harmonia: Media Center Off" "Power off the AVR + TV (Media Center Off scene)" \
         television-off  "avr-tv-bridge/scene/media_center_off/set" > "$APPS/harmonia-power-off.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true

# Persisted shortcut config (read by KWin at next login). KDE command shortcuts
# live under the [services] group.
if command -v kwriteconfig6 >/dev/null 2>&1; then
  kwriteconfig6 --file "$KG" --group services --group "harmonia-power-on.desktop"  --key _launch "$HOTKEY_ON"
  kwriteconfig6 --file "$KG" --group services --group "harmonia-power-off.desktop" --key _launch "$HOTKEY_OFF"
fi

# Live registration (so the keys work now, no re-login) — best-effort.
if command -v busctl >/dev/null 2>&1 \
   && busctl --user status org.kde.kglobalaccel >/dev/null 2>&1; then
  K=org.kde.kglobalaccel; O=/kglobalaccel; I=org.kde.KGlobalAccel
  busctl --user call $K $O $I doRegister as 4 \
    "harmonia-power-on.desktop"  "_launch" "Harmonia: Media Center On"  "Media Center On"  >/dev/null 2>&1 || true
  busctl --user call $K $O $I doRegister as 4 \
    "harmonia-power-off.desktop" "_launch" "Harmonia: Media Center Off" "Media Center Off" >/dev/null 2>&1 || true
  busctl --user call $K $O $I setShortcut asaiu 4 \
    "harmonia-power-on.desktop"  "_launch" "Harmonia: Media Center On"  "Media Center On"  1 "$KEYINT_ON"  2 >/dev/null 2>&1 || true
  busctl --user call $K $O $I setShortcut asaiu 4 \
    "harmonia-power-off.desktop" "_launch" "Harmonia: Media Center Off" "Media Center Off" 1 "$KEYINT_OFF" 2 >/dev/null 2>&1 || true
  echo "Hotkeys registered live: $HOTKEY_ON = On, $HOTKEY_OFF = Off"
else
  echo "Hotkeys written; they will activate at next login ($HOTKEY_ON = On, $HOTKEY_OFF = Off)."
fi
