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

- **Transceiver:** an IR receiver/blaster supported by LIRC. The reference setup
  uses a **USB-UIRT** (FTDI-based) on the Harmonia host, at
  `/dev/serial/by-id/usb-FTDI_USB-UIRT-if00-port0`; adjust the device path for
  other hardware.
- **Driver:** **LIRC** (`lircd`) with the `usb_uirt_raw` driver. `lircd.service`
  runs continuously and is **socket-activated** via `lircd.socket`; its control
  socket is `/var/run/lirc/lircd` (mode `srw-rw-rw-`, i.e. world read/write).
- **Existing consumers:** `irexec.service` (a **user** service for the desktop
  user, `Restart=on-failure`) reads the socket and runs `~/buttonpress.py` to
  drive the app launcher from the remote's number buttons. The IR bridge reads
  the **same socket** independently — LIRC allows multiple socket readers, so
  the bridge and irexec coexist without conflict.
- **Loaded remote(s):** whatever LIRC remotes you install. This repo includes
  example profiles for a TV remote and, optionally, an ESP32-S3-BOX-3
  (`lirc/s3box.lircd.conf`). List what lircd knows with
  `irsend LIST "" ""` and per-remote keys with `irsend LIST <remote> ""`.

## The service: `avr-tv-bridge-ir.service` (runs as **root**)

Unit template: `systemd/avr-tv-bridge-ir.service`; installed by `install.sh`
section **6b** (skipped if `irsend` isn't present). `ExecStart` runs
`/opt/avr-tv-bridge/venv/bin/python /opt/avr-tv-bridge/src/ir_bridge.py`.

**Why root (unlike every other service here, which run as the desktop user).** Receiving
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
  automation trigger ("when `KEY_1` on `<remote>` → …"). This is the primary
  "read IR in HA" surface.
- **`sensor` "Last IR Code"** (diagnostic) — last `remote/KEY` seen, with attrs.
- **`sensor` "IR Learn Status"** (diagnostic) — progress/result of a learn.
- **`text` "IR Send (Remote/KEY)"** — type `<remote>/KEY_MENU` (or
  `… x3`) to fire a command directly. Publishes to `…/send/set`.
- **`text` "IR Learn (Remote/KEY)"** — type `<remote>/KEY_POWER`, submit, then
  press the physical remote at the receiver. Publishes to `…/learn/set`.
- **`button` per known key** (gated by `IR_EXPOSE_KEY_BUTTONS=1`) — one
  tap-to-send button each, e.g. `IR <remote> KEY_OK`, payload `Remote/KEY`.

> HA slugifies entity_ids from names and they are **not** always predictable
> (look them up in the HA entity registry). Always verify actual entity_ids in
> Settings → Entities / the registry before referencing them in a dashboard.

## How each path works

**Receive.** `LircReceiver` opens the lircd control socket and reads its line
protocol `"<code> <repeat> <symbol> <remote>"`. The first frame (`repeat == 0`)
publishes an `event`; holds update only the `last` readout. It reconnects on
lircd restart and is *pausable* (so a learn can stop lircd without the receiver
thrashing). Note lircd only emits a symbol for a **properly decoded** remote
(e.g. a protocol LIRC can decode) — learned RAW remotes (below) are send-only.

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
  -t avr-tv-bridge/ir/send/set -m '<remote>/KEY_OK'

# See the retained HA discovery configs:
mosquitto_sub -h <MQTT_HOST> -u <MQTT_USER> -P <MQTT_PASS> \
  -t 'homeassistant/+/avr-tv-bridge_ir/+/config' -v
```

### Troubleshooting

- **Nothing on receive:** lircd only emits for a decoded remote. Confirm with
  `irw` (press a button — you should see `… KEY_x <remote>`). If `irw` is
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

## Optional remote profiles

No particular remote or IR device is required: the bridge works with any LIRC
remote, and you can run it with none of the optional profiles installed. The
repo includes example profiles for a TV remote and, optionally, an
ESP32-S3-BOX-3 (`lirc/s3box.lircd.conf`). The S3BOX example link is documented
separately in [`IR-S3BOX-EXAMPLE.md`](IR-S3BOX-EXAMPLE.md); skip it unless you
use that hardware.

