#!/usr/bin/env bash
#
# install.sh — provision a Linux host to bridge an AV receiver and/or display
# to Home Assistant over MQTT, and set up HDMI-CEC and USB-UIRT IR control.
#
# Idempotent: safe to re-run. It installs packages, lays down systemd services,
# configures LIRC for the USB-UIRT, and installs the bridge into /opt.
#
# Usage:
#   sudo ./install.sh            # full install
#   sudo ./install.sh --no-ir    # skip IR/LIRC setup
#   sudo ./install.sh --no-cec   # skip HDMI-CEC setup
#
# After running, edit ~/.config/avr-tv-bridge/config.env (created from the
# template on first run) with your MQTT credentials and device IPs, run the
# Vizio pairing helper once, then: systemctl --user? No — see notes below.
set -euo pipefail

# ---- resolve paths / user -------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/avr-tv-bridge"
# The unprivileged user who will own the bridge + config (not root).
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
CONFIG_DIR="$RUN_HOME/.config/avr-tv-bridge"

DO_IR=1; DO_CEC=1
for arg in "$@"; do
  case "$arg" in
    --no-ir)  DO_IR=0 ;;
    --no-cec) DO_CEC=0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo (needs to install packages + systemd units)." >&2
  exit 1
fi

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# ---- 1. system packages ---------------------------------------------------
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
PKGS=(python3 python3-venv python3-pip netcat-openbsd)
[[ $DO_CEC -eq 1 ]] && PKGS+=(v4l-utils)          # provides cec-ctl
[[ $DO_IR  -eq 1 ]] && PKGS+=(lirc)
apt-get install -y -qq "${PKGS[@]}"

# ---- 2. bridge code + venv ------------------------------------------------
log "Installing bridge into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -r "$REPO_DIR/src" "$INSTALL_DIR/"
# Ship the setup/diagnostic tooling alongside the code so `harmonia` can find it.
cp -r "$REPO_DIR/setup" "$INSTALL_DIR/"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" -q install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" -q install -r "$REPO_DIR/requirements.txt"
chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"

# ---- 2b. harmonia CLI -----------------------------------------------------
log "Installing 'harmonia' command to /usr/local/bin"
install -m 755 "$REPO_DIR/setup/harmonia" /usr/local/bin/harmonia

# ---- 2b2. keyboard power hotkeys (Meta+F11 On / Meta+F12 Off) --------------
# Runs as the unprivileged user (writes ~/.config + ~/.local/share). Live
# registration needs a running session, so during install it just writes the
# config; it activates at next login (or run `harmonia hotkeys` in-session).
if [[ -f "$REPO_DIR/setup/register_hotkeys.sh" ]]; then
  log "Registering keyboard power hotkeys for $RUN_USER"
  sudo -u "$RUN_USER" HOME="$RUN_HOME" INSTALL_DIR="$INSTALL_DIR" \
    bash "$REPO_DIR/setup/register_hotkeys.sh" || \
    echo "   -> hotkey registration skipped (run 'harmonia hotkeys' in-session)"
fi

# ---- 2c. Chrome D-pad extension (force-install via managed policy) ---------
# Google Chrome ignores --load-extension, so we pack the extension to a signed
# .crx with a STABLE id (pinned by the "key" in its manifest) and force-install
# it via a managed policy pointing at a local update manifest. This gives Netflix
# / Sportsnet / TSN spatial (D-pad) navigation. Skipped gracefully if Chrome
# isn't present.
EXT_SRC="$REPO_DIR/chrome-extension"
EXT_DIR="/opt/harmonia-dpad-ext"
CHROME_BIN="$(command -v google-chrome || command -v google-chrome-stable || true)"
EXT_ID="jjfoinfehejcchijlfaikgcdmhmnjmhh"   # derived from the manifest "key"
if [[ -n "$CHROME_BIN" && -f "$EXT_SRC/manifest.json" ]]; then
  log "Packing + force-installing the D-pad Chrome extension"
  mkdir -p "$EXT_DIR/src"
  cp "$EXT_SRC"/*.json "$EXT_SRC"/*.js "$EXT_SRC"/*.css "$EXT_DIR/src/" 2>/dev/null || true
  chmod -R a+rX "$EXT_DIR/src"
  # First run: generate the signing key (kept out of git; root-only).
  if [[ ! -f "$EXT_DIR/signing-key.pem" ]]; then
    openssl genrsa 2048 > "$EXT_DIR/signing-key.pem" 2>/dev/null
    chmod 600 "$EXT_DIR/signing-key.pem"
  fi
  # Pack in a writable temp dir, then move the crx into place.
  PACK_TMP="$(mktemp -d)"; cp -r "$EXT_DIR/src" "$PACK_TMP/src"; chmod -R a+rX "$PACK_TMP/src"
  "$CHROME_BIN" --pack-extension="$PACK_TMP/src" \
    --pack-extension-key="$EXT_DIR/signing-key.pem" --no-message-box >/dev/null 2>&1 || true
  if [[ -f "$PACK_TMP/src.crx" ]]; then
    cp "$PACK_TMP/src.crx" "$EXT_DIR/harmonia-dpad.crx"; chmod a+r "$EXT_DIR/harmonia-dpad.crx"
    cat > "$EXT_DIR/update.xml" <<XML
<?xml version='1.0' encoding='UTF-8'?>
<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>
  <app appid='$EXT_ID'>
    <updatecheck codebase='file://$EXT_DIR/harmonia-dpad.crx' version='1.0.0' />
  </app>
</gupdate>
XML
    chmod a+r "$EXT_DIR/update.xml"
    mkdir -p /etc/opt/chrome/policies/managed
    cat > /etc/opt/chrome/policies/managed/harmonia-dpad.json <<JSON
{
  "ExtensionInstallForcelist": [ "$EXT_ID;file://$EXT_DIR/update.xml" ],
  "ExtensionInstallAllowlist": [ "$EXT_ID" ]
}
JSON
    chmod a+r /etc/opt/chrome/policies/managed/harmonia-dpad.json
    echo "   -> D-pad extension $EXT_ID force-installed via policy."
  else
    echo "   -> WARN: crx pack failed; skipping D-pad extension." >&2
  fi
  rm -rf "$PACK_TMP"
else
  echo "   -> Chrome not found or no extension source; skipping D-pad extension."
fi

# ---- 3. config scaffolding ------------------------------------------------
log "Setting up config in $CONFIG_DIR"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 700 "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/config.env" ]]; then
  install -o "$RUN_USER" -g "$RUN_USER" -m 600 \
    "$REPO_DIR/config.env.example" "$CONFIG_DIR/config.env"
  echo "   -> created config.env from template; EDIT IT with your settings."
else
  echo "   -> config.env already exists; leaving it untouched."
fi

# ---- 4. HDMI-CEC service --------------------------------------------------
if [[ $DO_CEC -eq 1 ]]; then
  log "Installing HDMI-CEC playback service"
  install -m 644 "$REPO_DIR/systemd/cec-playback.service" \
    /etc/systemd/system/cec-playback.service
  systemctl daemon-reload
  systemctl enable cec-playback.service
  # Start now if a CEC device is present (won't hang: unit waits for /dev/cec0)
  if [[ -e /dev/cec0 ]]; then systemctl restart cec-playback.service || true; fi
fi

# ---- 5. IR / LIRC (USB-UIRT) ---------------------------------------------
if [[ $DO_IR -eq 1 ]]; then
  log "Configuring LIRC for USB-UIRT"
  install -m 644 "$REPO_DIR/lirc/lirc_options.conf" /etc/lirc/lirc_options.conf
  install -m 644 "$REPO_DIR/src/drivers/vizio/lirc/vizio.lircd.conf" \
    /etc/lirc/lircd.conf.d/vizio.lircd.conf
  install -m 644 "$REPO_DIR/src/drivers/s3box/lirc/s3box.lircd.conf" \
    /etc/lirc/lircd.conf.d/s3box.lircd.conf
  # Remove the distro's default remote stub so it doesn't shadow ours.
  [[ -f /etc/lirc/lircd.conf.d/devinput.lircd.conf ]] && \
    mv /etc/lirc/lircd.conf.d/devinput.lircd.conf \
       /etc/lirc/lircd.conf.d/devinput.lircd.conf.disabled || true
  systemctl enable lircd.socket lircd.service || true
  systemctl restart lircd.socket lircd.service || true
fi

# ---- 6. bridge service ----------------------------------------------------
log "Installing bridge systemd service (running as $RUN_USER)"
# Render the unit with the correct User= and home path.
sed -e "s|^EnvironmentFile=.*|EnvironmentFile=$CONFIG_DIR/config.env|" \
    "$REPO_DIR/systemd/avr-tv-bridge.service" > /etc/systemd/system/avr-tv-bridge.service
# Insert User= if not present
if ! grep -q '^User=' /etc/systemd/system/avr-tv-bridge.service; then
  sed -i "/^\[Service\]/a User=$RUN_USER" /etc/systemd/system/avr-tv-bridge.service
fi
systemctl daemon-reload
systemctl enable avr-tv-bridge.service

# ---- 6b. IR bridge service (root) -----------------------------------------
# Exposes the USB-UIRT IR transceiver to Home Assistant (receive/send/learn).
# Runs as root (not $RUN_USER): learning stops/starts lircd, opens the serial
# device, and writes /etc/lirc configs. Only installed if LIRC is present.
if command -v irsend >/dev/null 2>&1; then
  log "Installing IR bridge systemd service (running as root)"
  install -d -m 755 /var/lib/avr-tv-bridge-ir
  sed -e "s|^EnvironmentFile=.*|EnvironmentFile=$CONFIG_DIR/config.env|" \
      -e "s|^Environment=IREXEC_USER=.*|Environment=IREXEC_USER=$RUN_USER|" \
      -e "s|^Environment=IREXEC_UID=.*|Environment=IREXEC_UID=$(id -u "$RUN_USER")|" \
      "$REPO_DIR/systemd/avr-tv-bridge-ir.service" \
      > /etc/systemd/system/avr-tv-bridge-ir.service
  systemctl daemon-reload
  systemctl enable avr-tv-bridge-ir.service
else
  log "LIRC (irsend) not found — skipping IR bridge service"
fi

cat <<EOF

$(log "Install complete.")
Finish setup with the guided wizard (run as $RUN_USER, NOT root):

  harmonia setup

It discovers your receiver/TV on the network, auto-detects the speaker layout,
pairs devices that support pairing, and writes your config. Then:

  harmonia doctor            # verify everything is healthy
  harmonia start             # start the bridge
  harmonia logs -f           # watch logs

Other commands:  harmonia status | list | driver new <role> <name> | help

(Prefer to configure by hand? Edit $CONFIG_DIR/config.env, run
 'harmonia pair [host] [--driver <name>] [--save]', then 'harmonia start'.)
Devices appear automatically in Home Assistant via MQTT discovery.
EOF
