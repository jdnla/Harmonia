# TODO / future work

## After KDE 6.7 / Bigscreen is native: migrate CEC nav to plasma-remotecontrollers
Once Plasma 6.7 + Bigscreen (and `plasma-remotecontrollers`) are packaged in the
release archive, plan to let the native KDE stack own the CEC-input layer and
rebuild our extras on top of it, rather than maintaining our own CEC reader.

What plasma-remotecontrollers does (github.com/KDE/plasma-remotecontrollers):
CEC (libcec) + evdev + SDL gamepad + Wiimote → uinput keystrokes, with an
editable KConfig keymap. It's the input layer Bigscreen expects.

Migration plan:
  - Install packaged plasma-remotecontrollers; let it handle CEC→keystroke +
    gamepad/Wiimote. Configure its KConfig keymap for this Vizio remote
    (ui-cmds: up=0x01 down=0x02 left=0x03 right=0x04 select=0x00 back=0x0d).
  - DISABLE our `avr-tv-bridge-cecnav.service` (only ONE thing may read
    /dev/cec0 — running both = double input).
  - RE-HOME the bits of cec_nav.py that remotecontrollers does NOT do, so we
    don't lose them:
      * CEC adapter SELF-HEAL — cec_nav runs `cec-ctl --playback` to reclaim the
        logical address before reading (fixed the post-26.04 "cec-follower:
        missing logical address(es)" breakage). Verify remotecontrollers handles
        an unconfigured adapter; if not, keep a small helper/oneshot that claims
        the playback role (our cec-playback.service already does this at boot —
        may be enough).
      * DEBOUNCE for this TV's duplicate CEC emissions (CEC_NAV_DEBOUNCE_MS=120) —
        check if remotecontrollers double-fires; if so, tune its config or add a
        debounce.
  - Everything else in the project stays untouched (it's independent of CEC nav):
    AV/TV bridge, app launcher, media transport, camera popup, display wake, HA.
GAIN: native/maintained input layer + gamepad/Wiimote. COST: one source build
until packaged; re-home self-heal + debounce. Current cec_nav.py is fine until
then — keep it.

## Plasma Bigscreen — clean up the source build before the 6.7 apt package
We source-built + installed plasma-bigscreen v6.6.91 into /usr (outside apt's
tracking). When Plasma 6.7 + the packaged `plasma-bigscreen` reach the release
archive and you upgrade, dpkg will overwrite files at matching paths but may
leave OUR files orphaned wherever the package's layout differs — risking a
mix of stale 6.6.91 + new 6.7 files.

BEFORE the 6.7 upgrade, remove the source build cleanly:
    ~/bigscreen-build/uninstall-bigscreen.sh     (on the media PC)
It deletes exactly the 681 files from install_manifest.txt + the shell/session
packages. Then upgrade normally; `apt install plasma-bigscreen` comes in clean.
(Same script is the "remove it entirely" button if Bigscreen isn't wanted.)
Build recipe + manifest are saved in ~/bigscreen-build/ and in memory.

## Camera popup — window placement (DONE 2026-07-27, commit 901ec3c)
RESOLVED. Placement is done by a one-shot KWin script (loadScript+start) that
sets frameGeometry + keepAbove + skip taskbar/pager/switcher on the mpv window
matching the popup caption, with a windowAdded hook for late maps and a
verify/retry loop (src/camera_popup.py `_place_window_kwin`). Compositor-level,
so it works identically under the normal Plasma desktop and Plasma Bigscreen
(same kwin_wayland). Default size 640x360 = 1/9 of 1920x1080, top-right, 16px
margin. Why earlier approaches failed: mpv cant self-position on Wayland;
kdotool windowmove drops the X offset; mpv --geometry size is halved at Scale 2
and its +X+Y is ignored; kwinrulesrc rules dont reload live. Setting
w.frameGeometry from a KWin script is the reliable path.

## App launcher
- **Plasma Bigscreen support** — get a 10-foot TV home-screen shell going.
  BLOCKED on Ubuntu 25.10: plasma-bigscreen is NOT packaged in Ubuntu at any KDE
  version (only in KDE neon, a separate distro). Upgrading KDE within Ubuntu
  does NOT add it (25.10 already ships current KDE: Plasma 6.4.5 / KF6 6.17).
  Options were: risky KDE-neon repo mixing, or source build. `plasma-nano`
  (Bigscreen's underlying shell) IS packaged in Ubuntu, so a nano-based build
  is the lightest path. **DECISION: revisit after upgrading the box to Ubuntu
  26.04 LTS (user is installing it) — check if bigscreen/nano is packaged there
  first.** When trying: install as a SEPARATE SDDM session so the normal Plasma
  desktop (where cecnav/ydotoold/launcher run) stays intact.
- **Netflix as a proper Firefox PWA** — currently uses Chrome `--app`. Getting
  `firefoxpwa` working is finicky with snap-packaged Firefox; revisit if a
  Firefox-based PWA is preferred.
- **More apps** — extend the `APPS` registry in `src/launcher.py`
  (Prime, YouTube, Kodi, sports streams, etc.).

## After the Ubuntu 26.04 reinstall
- The media stack lives on the desktop machine; a reinstall wipes it. Re-run
  the installers to restore: `install.sh` (system bridge + CEC + IR) and
  `install-launcher.sh` (launcher + ydotool-from-source + ydotoold + cecnav +
  irexec). Repo is safe on Gitea (jordan/avr-tv-bridge). Re-pair the Vizio
  (setup/pair_vizio.py) and re-add the MQTT creds to config.env. Confirm jordan
  is in the `video` group (for cec-follower) and `input`/`dialout` groups.

## AVR / TV bridge
- **Verify Denon volume display offset** (`DENON_VOLUME_OFFSET`, currently 2).
  It was set from a single data point (HA 45 → screen 47); confirm it's
  constant across the range (check 40/50/60) rather than assuming.
- **Live-test power/input + the "Media Center On" scene** — the power on/off,
  input switching, and scene paths are written but not yet verified against the
  real TV+AVR (deferred while the TV was in use).

## Home Assistant hygiene
- The native HEOS/Denon integration also controls this AVR and a broken
  `denon_avr_quick_select_buttons` automation errors on AVR state changes
  (`'quick_select' is undefined`). Decide whether the native integration or
  this bridge owns the AVR, and fix/remove the automation.
