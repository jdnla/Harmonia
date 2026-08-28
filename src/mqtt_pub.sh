#!/usr/bin/env bash
# Publish an MQTT message using the bridge's config.env creds. Used by irexec so
# a remote button can send a command topic (e.g. the camera-popup toggle) without
# hardcoding credentials in the lircrc.
# Usage: mqtt_pub.sh <topic> [payload]
set -euo pipefail
CFG="${AVR_TV_BRIDGE_CONFIG:-$HOME/.config/avr-tv-bridge/config.env}"
# shellcheck disable=SC1090
[ -f "$CFG" ] && . "$CFG"
TOPIC="${1:?usage: mqtt_pub.sh <topic> [payload]}"
PAYLOAD="${2:-PRESS}"
exec mosquitto_pub -h "${MQTT_HOST:-127.0.0.1}" -p "${MQTT_PORT:-1883}" \
  ${MQTT_USER:+-u "$MQTT_USER"} ${MQTT_PASS:+-P "$MQTT_PASS"} \
  -t "$TOPIC" -m "$PAYLOAD"
