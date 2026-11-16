# IR bridge (`ir_bridge.py`) — USB-UIRT ⟷ Home Assistant

This is the full reference for the infrared bridge: how it exposes the host's IR transceiver to Home Assistant for **receiving** remote presses,
**sending** (blasting) commands, and **learning** brand-new codes. It is written
so a future maintainer (or future-you) can understand and extend it without
re-deriving everything.

> **Not to be confused with CEC D-pad nav.** `cec_nav.py` handles the TV remote's
> arrows/OK/Back over **HDMI-CEC** and injects keystrokes into the focused app.
> This IR bridge is a separate path over **LIRC / the USB-UIRT**, for turning any
> IR remote into an HA trigger and for blasting IR *out* to other devices. The
> two don't overlap. See the README "Input paths" section.

## Hardware & stack

- **Transceiver:** a **USB-UIRT** (FTDI-based IR receiver + blaster) on the media
  PC, at `/dev/serial/by-id/usb-FTDI_USB-UIRT-if00-port0`.
- **Driver:** **LIRC** (`lircd`) with the `usb_uirt_raw` driver. `lircd.service`
  runs continuously and is **socket-activated** via `lircd.socket`; its control
  socket is `/var/run/lirc/lircd` (mode `srw-rw-rw-`, i.e. world read/write).
- **Existing consumers:** `irexec.service` (a **user** service for `jdnla`,
  `Restart=on-failure`) reads the socket and runs `~/buttonpress.py` to drive the
  app launcher from the remote's number buttons. The IR bridge reads the **same
  socket** independently — LIRC allows multiple socket readers, so the bridge and
  irexec coexist without conflict.
- **Loaded remote(s):** at time of writing, **`Vizio-TV`** (learned NEC remote, address `0x20DF`, 17 buttons: D-pad + OK + Back + Menu + digits 0–9), defined in `/etc/lirc/lircd.conf.d/vizio.lircd.conf`, and **`S3BOX`** (dedicated NEC remote, address `0x7C63`, D-pad/OK/Back/Menu/play-pause/volume/mute/power/home), defined in `/etc/lirc/lircd.conf.d/s3box.lircd.conf` and used for the ESP32-S3-BOX-3 link. List what lircd knows with `irsend LIST "" ""` and per-remote keys with `irsend LIST <remote> ""`.

## The service: `avr-tv-bridge-ir.service` (runs as **root**)

Unit template: `systemd/avr-tv-bridge-ir.service`; installed by `install.sh`
section **6b** (skipped if `irsend` isn't present). `ExecStart` runs
`/opt/avr-tv-bridge/venv/bin/python /opt/avr-tv-bridge/src/ir_bridge.py`.

**Why root (unlike every other service here, which run as `jdnla`).** Receiving
and sending alone would work unprivileged — the lircd socket is world-rw, so
`irsend` and socket reads need no privilege. But **learning** must:
  1. `systemctl stop lircd.socket lircd.service` to free the serial port,
  2. open the FTDI serial device directly for `mode2` raw capture,
  3. write a new config into `/etc/lirc/lircd.conf.d/`,
  4. `systemctl start lircd.socket lircd.service`.

Those need root, so the whole service runs as root rather than sprinkling a
sudoers rule. `install.sh` fills the unit's `EnvironmentFile` with the installing
user's `config.env` path (root can read the 600-mode file) and sets
`IREXEC_USER`/`IREXEC_UID` so the post-learn irexec bounce targets the right user
session.

## MQTT topics (base `avr-tv-bridge/ir`)

| Direction | Topic | Payload |
| --------- | ----- | ------- |
| availability | `avr-tv-bridge/ir/availability` | `online` / `offline` (LWT) |
| **receive** | `avr-tv-bridge/ir/event` | JSON `{"event_type": "<KEY>", "remote": "<remote>"}` — fired on the first frame of a press (repeat 0) |
| **receive** | `avr-tv-bridge/ir/last` (+ `/last/attr`) | `"<remote>/<KEY>"`, attrs `{remote,button,repeat,ts}` — updates on every frame incl. holds |
| **send** (cmd) | `avr-tv-bridge/ir/send/set` | JSON `{"remote","key","repeats"}` **or** shorthand `"Remote/KEY"` / `"Remote/KEY x3"` |
| **learn** (cmd) | `avr-tv-bridge/ir/learn/set` | same target format (repeats ignored) |
| learn status | `avr-tv-bridge/ir/learn/status` (+ `/attr`) | `arming`/`listening`/`learned`/`failed`/`error`, attrs `{state,detail,ts}` |

## Home Assistant entities (auto-discovered)

All under a dedicated **"IR Blaster"** device (`identifiers: avr-tv-bridge_ir`),
nested below the main **Media Center** device via `via_device`. Published
(retained) on connect and re-published after every successful learn.

- **`event` "IR Received"** — `event_types` = every key lircd knows. Use it as an
  automation trigger ("when `KEY_1` on `Vizio-TV` → …"). This is the primary
  "read IR in HA" surface.
- **`sensor` "Last IR Code"** (diagnostic) — last `remote/KEY` seen, with attrs.
- **`sensor` "IR Learn Status"** (diagnostic) — progress/result of a learn.
- **`text` "IR Send (Remote/KEY)"** — type `Vizio-TV/KEY_MENU` (or
  `… x3`) to fire a command directly. Publishes to `…/send/set`.
- **`text` "IR Learn (Remote/KEY)"** — type `Denon/KEY_POWER`, submit, then press
  the physical remote at the receiver. Publishes to `…/learn/set`.
- **`button` per known key** (gated by `IR_EXPOSE_KEY_BUTTONS=1`) — one
  tap-to-send button each, e.g. `IR Vizio-TV KEY_OK`, payload `Remote/KEY`.

> HA slugifies entity_ids from names and they are **not** always predictable
> (look them up in the HA entity registry). Always verify actual entity_ids in
> Settings → Entities / the registry before referencing them in a dashboard.

## How each path works

**Receive.** `LircReceiver` opens the lircd control socket and reads its line
protocol `"<code> <repeat> <symbol> <remote>"`. The first frame (`repeat == 0`)
publishes an `event`; holds update only the `last` readout. It reconnects on
lircd restart and is *pausable* (so a learn can stop lircd without the receiver
thrashing). Note lircd only emits a symbol for a **properly decoded** remote
(e.g. the NEC Vizio one) — learned RAW remotes (below) are send-only.

**Send.** `avr-tv-bridge/ir/send/set` → `irsend SEND_ONCE <remote> <key>` (the
key is repeated N times in one call for `repeats`). `IR_SEND_REPEAT_CAP` clamps
runaway repeats.

**Learn (protocol-agnostic raw capture).** On `…/learn/set`:
1. publish `arming` status, pause the receiver;
2. `systemctl stop lircd.socket lircd.service` (free the serial port);
3. `mode2 --driver usb_uirt_raw --device <dev>` captures one burst. `fold_mode2()`
   turns the `pulse/space` stream into a LIRC **raw_codes** duration list (starts
   on the first pulse, ends at the first long gap, trimmed to end on a pulse);
4. store to `IR_LEARNED_DIR/<remote>.json` (the **source of truth**) and
   regenerate `IR_LEARNED_CONF_DIR/learned-<remote>.lircd.conf` (`flags RAW_CODES`)
   from that JSON — the bridge never parses LIRC config back in;
5. `systemctl start lircd.socket lircd.service`, resume the receiver, **bounce
   `irexec`** (`su - <IREXEC_USER> -c 'XDG_RUNTIME_DIR=/run/user/<uid> systemctl
   --user restart irexec.service'`) so the physical remote reconnects;
6. re-enumerate remotes and re-publish discovery (the new key gets a button).

Raw capture is protocol-agnostic, so it learns **any** remote (not just NEC).
The trade-off: the learned code is send-only and replayed as raw timing.
The capture timeout is enforced with a reader thread and a bounded queue wait;
if no burst arrives, it returns within `IR_LEARN_TIMEOUT`, restarts lircd, and
reports `failed` instead of blocking forever.

**Caveat:** a learn briefly (up to `IR_LEARN_TIMEOUT`, default 20 s) stops lircd,
which pauses the physical remote during capture; the irexec bounce in step 5
restores it.

## Configuration (`config.env`, all defaulted)

```
UIRT_DEVICE=/dev/serial/by-id/usb-FTDI_USB-UIRT-if00-port0
UIRT_DRIVER=usb_uirt_raw
LIRC_SOCKET=/var/run/lirc/lircd
IR_LEARN_TIMEOUT=20            # seconds to wait for a press during learn
IR_EXPOSE_KEY_BUTTONS=1        # 0 = only the send/learn text boxes, no per-key buttons
IR_SEND_REPEAT_CAP=20          # max repeats per send request
IR_LEARNED_DIR=/var/lib/avr-tv-bridge-ir
IR_LEARNED_CONF_DIR=/etc/lirc/lircd.conf.d
```

`IREXEC_USER` / `IREXEC_UID` are set by `install.sh` on the systemd unit (not in
`config.env`), for the post-learn irexec bounce.

## Tests

`tests/test_ir_bridge.py` — 16 stdlib `unittest` cases covering the pure logic
(send/learn payload parse, `fold_mode2` burst folding, `LearnedStore` conf
regeneration, name sanitizing, `irsend LIST` parsing). Run on the box venv
(which has `paho`):

```bash
cd <your clone> && /opt/avr-tv-bridge/venv/bin/python tests/test_ir_bridge.py -v
```

## Verifying / operating

```bash
systemctl status avr-tv-bridge-ir.service
journalctl -u avr-tv-bridge-ir.service -f          # watch receive/send/learn

# Send a command by hand (needs MQTT creds from config.env):
mosquitto_pub -h <MQTT_HOST> -u <MQTT_USER> -P <MQTT_PASS> \
  -t avr-tv-bridge/ir/send/set -m 'Vizio-TV/KEY_OK'

# See the retained HA discovery configs:
mosquitto_sub -h <MQTT_HOST> -u <MQTT_USER> -P <MQTT_PASS> \
  -t 'homeassistant/+/avr-tv-bridge_ir/+/config' -v
```

### Troubleshooting

- **Nothing on receive:** lircd only emits for a decoded remote. Confirm with
  `irw` (press a button — you should see `… KEY_x Vizio-TV`). If `irw` is
  silent, it's a LIRC/hardware issue, not the bridge.
- **Send does nothing physically but logs `rc=0`:** `irsend` reached lircd fine;
  the blaster just isn't aimed at / in range of the target device, or the target
  is off and ignores that code.
- **Learn always fails "no IR captured":** the USB-UIRT didn't see the burst —
  aim the remote at it and press firmly within the timeout. Check `mode2` can
  open the device while lircd is stopped.
- **`uirt2_raw: checksum error` on lircd start:** the deployed
  `/etc/lirc/lirc_options.conf` is using `uirt2_raw` instead of
  `usb_uirt_raw`, or a stale `mode2` from a hung learn still owns the device.
  Stop `avr-tv-bridge-ir`, `pkill -f mode2`, verify
  `driver = usb_uirt_raw` and the by-id device path, power-cycle the USB-UIRT,
  then start lircd before the IR bridge.
- **Physical remote dead after a learn:** the irexec bounce (step 5) didn't take.
  `systemctl --user -M <user>@ restart irexec.service`, or just restart lircd +
  irexec.

## S3BOX / ESP32-S3-BOX-3 IR link

The ESP32-S3-BOX-3 (Home Assistant voice satellite in the room) has an IR
receiver on GPIO38 and an IR transmitter on GPIO39. It is physically pointed at
the USB-UIRT, so the two can exchange NEC codes:

- **BOX-3 → Harmonia (not verified):** call
  `esphome.esp32_s3_box_3_send_ir` with `address: 0x7C63` (31843 decimal),
  `command: <S3BOX key code>`, and `repeats: 1+`. The BOX-3 self-receives its
  own transmission, but the USB-UIRT has not decoded one at the current
  distance; the SENSOR-dock emitter is too weak. The LIRC receive-side bit
  order also still needs testing before relying on `S3BOX/<KEY>` events from
  this direction.
- **Harmonia → BOX-3:** send `S3BOX/<KEY>` to `avr-tv-bridge/ir/send/set`
  (or press an S3BOX button in HA). The BOX-3's NEC decoder sees LIRC/USB-UIRT
  transmissions **bit-reversed**, so its `on_nec` handler matches address
  `0xC63E` (reversed `0x7C63`) and the reversed key code, then fires an
  `esphome.ir_received` HA event with `remote: S3BOX` and the key name. For
  example, S3BOX `KEY_OK` (`0x0005`) arrives at the BOX-3 as `0xA000`.

The unique address keeps BOX-3 traffic from also driving the Vizio TV. The S3BOX
key map is in `lirc/s3box.lircd.conf`; install it with `install.sh` (or copy it
to `/etc/lirc/lircd.conf.d/`) and restart `lircd` plus the IR bridge. The BOX-3
side is in `esp32-s3-box-3.yaml`; the send service and the receive event are
independent of this repo's deployment.

The example is **one-way, Harmonia → BOX-3**,.
BOX-3 → Harmonia is not implemented because the SENSOR-dock emitter is too weak at the
current physical spacing; the send service remains available for testing.

**BOX-3 power and bit order.** The SENSOR dock's IR receiver/emitter are dead
until GPIO44 is driven (the `enable_ir` switch, inverted, default on); the
ESPHome config must include it. The BOX-3's NEC decoder sees the USB-UIRT/LIRC
transmissions bit-reversed: Vizio `0x20DF/0x58A7` arrives as `0xFB04/0xE51A`,
and `0x52AD` arrives as `0xB54A`. Map the reversed codes (the deployed ESPHome
config has both standard and reversed handlers) or normalize the bit order.
The SENSOR-dock emitter is weak, roughly 1–1.3 m; the BOX-3 → USB-UIRT direction
needs the USB-UIRT close to the BOX-3.

### Deploying the S3BOX remote on the host

The S3BOX LIRC remote is committed in this repo. On the host
(`192.0.2.6`):

```bash
cd <your clone>
git fetch origin
git checkout main
git pull --ff-only

sudo install -m 644 lirc/s3box.lircd.conf /etc/lirc/lircd.conf.d/
sudo install -m 644 src/ir_bridge.py /opt/avr-tv-bridge/src/ir_bridge.py
sudo systemctl restart lircd.socket lircd.service
sudo systemctl restart avr-tv-bridge-ir.service
systemctl --user restart irexec.service

irsend LIST "" ""          # expect Vizio-TV and S3BOX
irsend LIST S3BOX ""       # expect the 13 S3BOX keys
```

`sudo ./install.sh` is idempotent and installs the same files (plus the
services), but it also re-runs package/venv setup; the targeted copy above is
enough for this change.

**Test:** from the host, run `irsend SEND_ONCE S3BOX KEY_OK`. The BOX-3's
`IR last RX` diagnostic should show `addr=0xC63E cmd=0xA000`, and Home
Assistant should receive `esphome.ir_received` with `remote: S3BOX`,
`key: KEY_OK`. The HA `event.ir_blaster_ir_received` entity gains the S3BOX
keys after the IR bridge reconnects and republishes discovery.

The BOX-3 side can also be checked before the host is updated: call
`esphome.esp32_s3_box_3_send_ir` with address `0xC63E` (50750), command
`0xA000` (40960), and `repeats: 1`. The BOX-3 self-receives the transmission
and its S3BOX `KEY_OK` handler fires.

> **Aim matters.** The BOX-3's IR LED and receiver are on the front of the unit;
> the USB-UIRT must be in direct line of sight. If no burst is captured, verify the USB-UIRT is the active LIRC device and that `mode2`/`lircd` see a known physical remote.

