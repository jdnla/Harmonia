# Optional IR example: ESP32-S3-BOX-3

This is an **optional example**, not a requirement. Harmonia's IR bridge works
with any LIRC-compatible remote or IR device; skip this page if you do not use
an ESP32-S3-BOX-3.

If you use an ESPHome device with an IR receiver and transmitter, it can
exchange NEC codes with the USB-UIRT. The repo ships an example LIRC profile
for an ESP32-S3-BOX-3. In that example, the device has an IR receiver on GPIO38
and an IR transmitter on GPIO39. When it is physically pointed at the USB-UIRT,
the two can exchange NEC codes:

- **ESPHome device → Harmonia (not verified):** call the device's
  `send_ir` action (for example `esphome.<device>_send_ir`) with `address:
  0x7C63` (31843 decimal), `command: <example key code>`, and `repeats: 1+`.
  The device self-receives its own transmission, but the SENSOR-dock emitter is
  weak; the USB-UIRT may not decode it reliably. The LIRC receive-side bit
  order also needs testing before relying on `<remote>/<KEY>` events from this
  direction.
- **Harmonia → ESPHome device:** send `<remote>/<KEY>` to
  `avr-tv-bridge/ir/send/set` (or press a button for that remote in HA). The
  device's NEC decoder sees LIRC/USB-UIRT transmissions **bit-reversed**, so its
  `on_nec` handler matches address `0xC63E` (reversed `0x7C63`) and the reversed
  key code, then fires an `esphome.ir_received` HA event with the remote name
  and key. For the included example, `KEY_OK` (`0x0005`) arrives as `0xA000`.

The unique address keeps this device's traffic from also driving the TV. The
included example key map is in `src/drivers/s3box/lirc/s3box.lircd.conf`;
install it with `install.sh` (or copy it to `/etc/lirc/lircd.conf.d/`) and
restart `lircd` plus the IR bridge. The ESPHome side is the device's IR
configuration; the send service and the receive event are independent of this
repo's deployment.

The example is **one-way, Harmonia → the ESPHome device**. The reverse direction
is not implemented because the SENSOR-dock emitter is weak; the send service
remains available for testing.

**Power and bit order (example hardware).** The SENSOR dock's IR
receiver/emitter are dead until GPIO44 is driven (the `enable_ir` switch,
inverted, default on); the ESPHome config must include it. The example device's
NEC decoder sees the USB-UIRT/LIRC transmissions bit-reversed: for example,
Vizio `0x20DF/0x58A7` arrives as `0xFB04/0xE51A`, and `0x52AD` arrives as
`0xB54A`. Map the reversed codes (an ESPHome config can include both standard
and reversed handlers) or normalize the bit order. The SENSOR-dock emitter is
weak, roughly 1–1.3 m; the reverse direction needs the USB-UIRT close to the
device.

## Deploying the optional S3BOX profile on the Harmonia host

The S3BOX LIRC profile is committed as an example; install it only if you use
that remote. On the Harmonia host:

```bash
cd <your clone>
git fetch origin
git checkout main
git pull --ff-only

sudo install -m 644 src/drivers/s3box/lirc/s3box.lircd.conf /etc/lirc/lircd.conf.d/
sudo install -m 644 src/ir_bridge.py /opt/avr-tv-bridge/src/ir_bridge.py
sudo systemctl restart lircd.socket lircd.service
sudo systemctl restart avr-tv-bridge-ir.service
systemctl --user restart irexec.service

irsend LIST "" ""          # expect <remote> and S3BOX
irsend LIST S3BOX ""       # expect the 13 S3BOX keys
```

`sudo ./install.sh` is idempotent and installs the same files (plus the
services), but it also re-runs package/venv setup; the targeted copy above is
enough for this change.

**Test:** from the Harmonia host, run `irsend SEND_ONCE S3BOX KEY_OK`. The
example device's `IR last RX` diagnostic should show `addr=0xC63E cmd=0xA000`,
and Home Assistant should receive `esphome.ir_received` with `remote: S3BOX`,
`key: KEY_OK`. The IR bridge's received-event entity gains the S3BOX keys
after the IR bridge reconnects and republishes discovery.

The ESPHome side can also be checked before the Harmonia host is updated: call
the device's `send_ir` action with address `0xC63E` (50750), command `0xA000`
(40960), and `repeats: 1`. The device self-receives the transmission and its
S3BOX `KEY_OK` handler fires.

> **Aim matters.** The example device's IR LED and receiver are on the front of
> the unit; the USB-UIRT must be in direct line of sight. If no burst is
> captured, verify the USB-UIRT is the active LIRC device and that
> `mode2`/`lircd` see a known physical remote.
