# Harmonia

A self-hosted media-center integration for a Linux HTPC running **KDE Plasma
(Wayland)** and **Home Assistant**. It bridges an AV receiver and/or a TV into
Home Assistant over MQTT, and turns the box into a remote-controllable media
center: launch apps, navigate them with the TV remote, control playback, match
the PC's audio output to your speaker layout, and pop up a camera feed on motion.

It's built around a **driver model** so it isn't tied to specific hardware — you
declare what you have in config and the bridge composes itself:

| Piece            | Protocol used                    | Works with |
| ---------------- | -------------------------------- | ---------- |
| AV receiver      | Denon/Marantz telnet control     | Most modern Denon/Marantz AVRs (or `none`) |
| TV / display     | Vizio SmartCast REST, or HDMI-CEC | Vizio SmartCast TVs; any CEC TV (or `none`) |
| Audio channels   | PipeWire profile + upmix          | Any PipeWire HDMI sink (2.0 / 5.1 / 7.1) |
| Remote D-pad     | HDMI-CEC user-control passthrough | Any TV that forwards remote keys over CEC |
| Remote buttons   | Infrared via LIRC                | Any IR remote (USB-UIRT or other LIRC device) |
| App control      | KWin (via `kdotool`) + `ydotool` | KDE Plasma Wayland |
| Camera popup     | any `mpv`-playable stream        | Frigate/go2rtc, or any RTSP/HTTP source |

> **Reference hardware:** Denon AVR-X1800H, Vizio P65Q9-H1, Ubuntu 26.04 / KDE Plasma 6,
> a USB-UIRT, and Frigate.

## Driver model (mix-and-match hardware)

Devices are selected by config, not code. Each role has a driver, and `none`
omits the role entirely:

```
HARMONIA_RECEIVER=denon    # denon | cec | null | none
HARMONIA_DISPLAY=vizio     # vizio | cec  | null | none
SPEAKER_CHANNELS=8         # 2 = stereo/TV-direct, 6 = 5.1, 8 = 7.1
```

Supported setups out of the box:

- **Receiver + TV** (the reference setup): Denon AVR + Vizio TV, audio 7.1.
- **TV-direct (no receiver):** `HARMONIA_RECEIVER=none` and give the display
  volume/mute caps (`VIZIO_CAPS=power,input,volume,mute`); the TV becomes the
  audio authority and `SPEAKER_CHANNELS` caps the layout the PC outputs.
- **CEC-only TV:** `HARMONIA_DISPLAY=cec` for TVs without a network API (power
  works; input/volume are scaffolded per-TV).

**Adding hardware** is a drop-in plugin — scaffold it, don't hand-write it:

```
python3 setup/harmonia_driver.py new receiver yamaha   # creates src/drivers/yamaha/
python3 setup/harmonia_driver.py list                  # see registered drivers + their hooks
```

The scaffold subclasses `AVReceiver`/`Display` (see `drivers/base.py`), registers
itself with `@register_receiver/@register_display`, and stubs `from_env`,
`get_state`, the commands, and an optional `discover()` hook with placeholder methods.
Any `*_driver.py` in `src/drivers/` **auto-loads** — no `__init__.py` edit, no
bridge changes. `denon_driver.py` / `vizio_driver.py` are the reference
implementations. Optional per-driver hooks: `discover(ssdp_results)` (makes it
show up in the setup wizard automatically) and `selftest(env)` (feeds
`harmonia doctor`); both have safe defaults.

## The `harmonia` command

`install.sh` puts a single `harmonia` command on your PATH that fronts
everything (it wraps the venv python so the tools always have their deps):

```
harmonia setup                     # guided discovery + config wizard
harmonia doctor                    # read-only health check
harmonia status                    # concise up/down of all three services
harmonia start|stop|restart [tgt]  # all services, or one: bridge|launcher|cecnav
harmonia logs [-f] [tgt]           # journal for a service (default: bridge)
harmonia config list               # current config.env (secrets masked)
harmonia config get <KEY>          # print one value (scripting-friendly)
harmonia config set <KEY> <VALUE>  # edit config.env in place (keeps a .bak)
harmonia list                      # registered device drivers + their hooks
harmonia driver new <role> <name>  # scaffold a new driver plugin
harmonia pair <tv_ip>              # pair a Vizio SmartCast TV
```

Lifecycle commands span the whole system — the **bridge** is a system service,
the **launcher** and **cecnav** are user services, and `harmonia` targets each
with the right privilege (re-invoking `sudo` only for the system unit). Run it as
your normal user, not root.

The underlying tools also run standalone if you prefer:
`setup/harmonia_setup.py`, `harmonia_doctor.py`, `harmonia_config.py`,
`harmonia_driver.py`.

## Features

Everything appears under a single **"Media Center"** device in Home Assistant:

- **AV control** — receiver power / volume / mute / input, TV power / input, with
  real state feedback from the devices' network APIs.
- **"Media Center On/Off" scene** + **Master Power/Volume/Mute** — bring the whole
  chain up to your chosen default inputs, or shut it down, in one tap.
- **App launcher** — launch (or focus, fullscreen) configured apps from the TV
  remote's number buttons or HA buttons.
- **D-pad navigation** — the TV remote's arrows / OK / Back drive whatever app is
  on screen, via CEC. For web apps that lack spatial navigation (Netflix,
  Sportsnet, TSN), a bundled Chrome extension (`chrome-extension/`) adds a 2-D
  focus ring that moves to the nearest control per arrow key. Google Chrome
  ignores `--load-extension`, so `install.sh` packs it to a signed `.crx` with a
  pinned ID and force-installs it via a managed policy (Netflix's DRM player
  keeps its own arrow handling; the extension defers while it's playing).
- **Media transport** — Play/Pause / Next / Previous + a "Now Playing" sensor.
- **Camera auto-popup** — a small always-on-top overlay of a camera feed when a
  detection MQTT topic fires, auto-closing when it clears. Toggleable from HA.
- **Display wake** — wakes the PC's screen (DPMS) when the AV gear powers on.
- **Power-sync** — keeps the receiver in step with the TV (fixes CEC turning on
  one device but not the other).
- **IR remote (receive / send / learn)** — via the USB-UIRT + LIRC. Received
  presses become an HA `event` entity (automation triggers) + a "Last IR Code"
  sensor; known codes get tap-to-send buttons plus a generic "Remote/KEY" send
  box; and new codes can be **learned** on the fly (point a remote at the
  receiver, capture, replay). Lives under a nested "IR Blaster" device. Full
  details in [`docs/IR.md`](docs/IR.md).

## Components & services

The system splits along a privilege boundary:

1. **AV/TV bridge** (`src/bridge.py`) — a **system** service (runs as your user).
   Talks to the receiver, the TV, and the MQTT broker over the network. No GUI
   access needed.

2. **App launcher / media / camera** (`src/launcher_service.py`) — a **user**
   service running inside the KDE session (it needs the graphical environment to
   open/focus windows). Handles app launching, D-pad injection, media transport,
   the camera popup, and display wake. Alongside it run `avr-tv-bridge-cecnav`
   (CEC D-pad) and `ydotoold` (key injection), also user services.

3. **IR bridge** (`src/ir_bridge.py`) — a **system** service that runs as
   **root** (the only one that does). Exposes the USB-UIRT IR transceiver to HA
   for receiving remote presses, sending/blasting IR, and learning new codes. It
   runs as root because *learning* must stop/start `lircd`, open the serial
   device, and write `/etc/lirc` configs; receive/send alone are unprivileged.
   See [`docs/IR.md`](docs/IR.md). Optional — installed only if LIRC is present.

All publish their entities over MQTT discovery, so the whole thing shows up in HA
as one **"Media Center"** device (with IR under a nested "IR Blaster" sub-device)
even though separate processes do the work.

## Input paths (how the remote reaches the PC)

- **App launch** (e.g. number buttons) → **IR** (an LIRC device → `lircd` →
  `irexec`).
- **D-pad navigation** (arrows/OK/Back) → **CEC**. Many TVs forward remote key
  presses as CEC `USER_CONTROL` messages; `src/cec_nav.py` reads them (via
  `cec-follower`, no root) and injects keystrokes into the focused app with
  `ydotool`.

Using CEC for navigation and IR only for launch buttons avoids double-input on
setups where the TV forwards *some* keys over CEC, and sidesteps unreliable IR
repeat decoding. If your TV doesn't do CEC passthrough, D-pad can fall back to IR
(map the buttons in `lirc/irexec.lircrc` to `navkey.sh`).

> **ydotool is built from source.** Some distros package a `ydotool` whose `key`
> command emits no events; `setup/build_ydotool.sh` builds a working one to
> `/usr/local/bin`. The installer runs this and masks any packaged
> `ydotool.service` (it would fight the daemon over `/dev/uinput`).

## Install

Prerequisites: KDE Plasma (Wayland), an MQTT broker reachable from the HTPC (Home
Assistant's broker is fine), and the HTPC user in the `video`, `input`, and
`dialout` groups.

```bash
git clone <this-repo> avr-tv-bridge
cd avr-tv-bridge

# 1. System service: AV/TV bridge + HDMI-CEC + IR
sudo ./install.sh                 # --no-cec / --no-ir to skip hardware bits

# 2. User service: launcher + D-pad (CEC) + media + camera popup
./install-launcher.sh             # run as your user, NOT root
```

Both are idempotent and install/verify their dependencies (`python3`, `kdotool`,
`v4l-utils`, `lirc`, `playerctl`, `mpv`, source-built `ydotool`).

Then configure:

1. Copy and edit the config:
   ```bash
   cp config.env.example ~/.config/avr-tv-bridge/config.env   # installer does this too
   ```
   Set your MQTT credentials, the receiver/TV addresses, the scene's default inputs,
   and any optional features. Every setting is documented in the file.
2. If your TV needs pairing (Vizio SmartCast does), run its one-time pairing —
   a PIN appears on the TV:
   ```bash
   /opt/avr-tv-bridge/venv/bin/python setup/pair_vizio.py <TV_IP>
   ```
3. Start / check:
   ```bash
   sudo systemctl start avr-tv-bridge avr-tv-bridge-ir   # -ir only if LIRC present
   systemctl --user status avr-tv-bridge-launcher avr-tv-bridge-cecnav ydotoold irexec
   ```

### After a major OS upgrade

A Python major-version bump breaks the virtualenv (`ModuleNotFoundError`).
Rebuild it:

```bash
cd <your working clone> && make rebuild-venv
```

## Configuration highlights (config.env)

All settings live in `config.env` (see `config.env.example` for the full list):

- **Devices** — `RECEIVER_HOST` / `TV_HOST` and friends; the scene's default
  inputs (`SCENE_AVR_INPUT`, `SCENE_TV_INPUT`); the input option lists shown in
  HA.
- **TV without its own speakers** — if audio is routed to the receiver, the TV
  exposes only power+input by default. Set `VIZIO_CAPS=power,volume,mute,input`
  to use the TV's speakers instead.
- **Apps** — the launcher's app registry is in `src/launcher.py` (window-match
  patterns + launch command + fullscreen). Add your own apps there.
- **Camera popup** — set `CAM_POPUP_TRIGGER_TOPIC` (any topic publishing a count,
  e.g. Frigate's `frigate/<camera>/person`) and `CAM_POPUP_STREAM_URL` (any
  `mpv`-playable URL; a go2rtc HTTP endpoint avoids embedding camera creds).
  Window placement is best set with a KWin rule.
- **Receiver volume display offset** — `DENON_VOLUME_OFFSET` so HA matches the
  number on the receiver's on-screen display, if they differ.
- **Power-sync** — `SYNC_POWER=1` (default) keeps the receiver following the TV.
  Set `0` to run the TV with the receiver off.

## Home Assistant dashboard

`homeassistant-dashboard.yaml` is a phone-optimized example layout (needs the
HACS `button-card` and `card-mod`). Home Assistant derives entity_ids from the
device/entity names, so IDs look like `switch.media_center_master_power`,
`button.media_center_launch_plex`, `sensor.media_center_now_playing`, etc. — the
exact prefix depends on your device name at first discovery, and HA's slugs are
**inconsistent** across discovery batches. Check Settings → Entities and adjust
the card entity_ids to match yours.

The example install also has a hand-built **`media-remote`** dashboard (one view). Editing HA dashboards goes through HA's authenticated websocket API; use a short-lived token and never store it in the repository.

## Deploy changes

```bash
cd <your working clone>
# edit, then:
git push
make deploy                 # pull + sync into /opt + restart the system bridge
```

For the user-session services, re-run `install-launcher.sh` (or copy `src/` into
`/opt/avr-tv-bridge/src` and `systemctl --user restart` the launcher/cecnav).

## Repo layout

```
src/
  bridge.py denon.py vizio.py discovery.py     AV/TV bridge (system service)
  ir_bridge.py                                 IR receive/send/learn (root service)
  launcher.py launcher_service.py session_env.py launch_app.sh
  navkey.py navkey.sh                          IR D-pad key injection (fallback)
  cec_nav.py                                   CEC D-pad navigation (primary)
  media_control.py                             play/pause/next/prev + status
  camera_popup.py                              motion-triggered camera overlay
  display_wake.py                              DPMS wake
tests/    test_ir_bridge.py, test_chime.py, …  stdlib unittest
setup/    pair_vizio.py, build_ydotool.sh
systemd/  bridge, ir, cec-playback, launcher, cecnav, ydotoold units
lirc/     IR device driver options, remote config, irexec.lircrc
install.sh              system bridge + CEC + IR (+ root IR bridge, §6b)
install-launcher.sh     user launcher + CEC nav + ydotool + media + camera
config.env.example      all settings, documented
homeassistant-dashboard.yaml
Makefile                deploy / rebuild-venv / status / logs
```

## Adapting to different hardware

- **Different AVR/TV brand:** the network clients are isolated in `src/denon.py`
  and `src/vizio.py` behind a small interface (get state; set power/input/volume/
  mute). Swap in a client for your brand and wire it up in `src/bridge.py`.
- **No CEC passthrough on your TV:** map the D-pad buttons in `lirc/irexec.lircrc`
  to `navkey.sh` to drive navigation over IR instead.

Secrets (`config.env`, `*.token`) are gitignored.
