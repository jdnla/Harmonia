"""Match PipeWire's HDMI output channel layout to the AV receiver.

Problem this solves: the media PC's PipeWire default sink defaults to
`hdmi-stereo`, so even genuine 5.1/7.1 content is downmixed to 2.0 before it
leaves the PC and the Denon has to fake surround with its upmixer. This helper
selects the receiver's maximum supported HDMI surround profile whenever the
receiver is present, and falls back to stereo when it's gone — so the sink
tracks the AVR automatically.

Design (agreed with the user):
  * Target = the highest surround profile the sound card advertises as
    `available: yes`. Availability is driven by the receiver's EDID over HDMI,
    so it flips to yes when the AVR is powered + HDMI-negotiated and to no when
    it's off or the input is switched away. That IS the "channel configuration
    changed" signal, self-contained on the PC.
  * Optional Denon telnet cross-check (power=ON) to avoid asserting surround
    during a transient EDID flap.
  * PipeWire channelmix upmix is enabled separately (WirePlumber drop-in, see
    install) so stereo content fills every speaker instead of leaving the
    surround channels silent. The user's Denon has center=NO / sub=NO, so the
    receiver folds the phantom center into the front L/R in hardware — we just
    feed it a standard 7.1 LPCM stream.

Runs inside the launcher service (needs the graphical-session env to reach the
user's PipeWire/pulse socket). Enable/tune via env (CHANNEL_SYNC_*). Exposes an
HA on/off toggle through the launcher, same pattern as IdleOff.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time

from session_env import graphical_env

log = logging.getLogger("channel_sync")

# Preference order, best (most channels) first. We pick the first profile in
# this list that the card reports as available. `-extra1` is the HDMI 2 output
# (the port this PC uses for the AVR); the plain variants are HDMI 1. We list
# both so the helper still works if the active HDMI port changes.
_PROFILE_PREFERENCE = (
    "output:hdmi-surround71-extra1",  # 7.1 (8ch) — drives FL FR C LFE SL SR SBL SBR
    "output:hdmi-surround71",
    "output:hdmi-surround-extra1",    # 5.1 (6ch)
    "output:hdmi-surround",
    "output:hdmi-stereo-extra1",      # 2.0 fallback
    "output:hdmi-stereo",
)

# The stereo profiles we fall back to when the receiver is absent. First
# available wins.
_STEREO_FALLBACK = ("output:hdmi-stereo-extra1", "output:hdmi-stereo")

# Nominal channel count each profile drives — used to cap the target at the
# user's declared speaker layout (a 5.1 setup shouldn't be pushed to 7.1).
_PROFILE_CHANNELS = {
    "output:hdmi-surround71-extra1": 8,
    "output:hdmi-surround71": 8,
    "output:hdmi-surround-extra1": 6,
    "output:hdmi-surround": 6,
    "output:hdmi-stereo-extra1": 2,
    "output:hdmi-stereo": 2,
}


class ChannelSync:
    def __init__(self,
                 card_hint: str = "hdmi",
                 poll_secs: float = 15.0,
                 enabled: bool = True,
                 denon=None,
                 denon_crosscheck: bool = True,
                 prefer_surround: bool = True,
                 max_channels: int = 8):
        """
        card_hint: substring to find the HDMI card in `pactl list cards short`.
        poll_secs: how often to re-check EDID availability.
        denon: optional device client for a power=ON cross-check (any driver with
            get_state()). Named `denon` for back-compat.
        denon_crosscheck: only assert surround if the device also reports power ON.
        prefer_surround: master switch — if False, always force stereo (lets the
            receiver/TV do its own upmix, i.e. the pre-existing behavior).
        max_channels: cap the target at the declared speaker layout (2/6/8). A
            5.1 setup passes 6 so it never selects a 7.1 profile; a TV-direct
            stereo setup passes 2. Driven by HARMONIA speaker-layout config.
        """
        self.card_hint = card_hint
        self.poll_secs = poll_secs
        self._enabled = enabled
        self.denon = denon
        self.denon_crosscheck = denon_crosscheck
        self.prefer_surround = prefer_surround
        self.max_channels = max_channels
        self._env = graphical_env()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._card: str | None = None
        self._last_applied: str | None = None

    # ---- HA toggle ----
    def set_enabled(self, on: bool):
        self._enabled = on
        log.info("channel-sync %s", "enabled" if on else "disabled")
        if on:
            # Re-evaluate immediately on enable.
            self._tick(force=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ---- lifecycle ----
    def start(self):
        self._card = self._find_card()
        if not self._card:
            log.warning("no HDMI card found (hint %r) — channel-sync inactive",
                        self.card_hint)
            return
        log.info("channel-sync watching card %s (poll %.0fs)",
                 self._card, self.poll_secs)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---- pactl helpers ----
    def _pactl(self, *args, read: bool = True) -> str:
        try:
            out = subprocess.run(["pactl", *args], env=self._env,
                                 capture_output=True, text=True, timeout=10)
            if out.returncode != 0:
                log.debug("pactl %s rc=%s err=%s", args, out.returncode,
                          out.stderr.strip())
            return out.stdout if read else ""
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("pactl %s failed: %s", args, e)
            return ""

    def _find_card(self) -> str | None:
        # Pick the HDMI card, avoiding the iec958-only (S/PDIF) card.
        short = self._pactl("list", "cards", "short")
        best = None
        for line in short.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1]
            if self.card_hint in name or "hdmi" in name.lower():
                return name
            if "pci" in name and "iec958" not in name:
                best = best or name
        return best

    def _available_profiles(self) -> set[str]:
        """Parse `pactl list cards` for profiles reporting available: yes."""
        text = self._pactl("list", "cards")
        avail: set[str] = set()
        # Only parse the block for our card.
        blocks = re.split(r"\nCard #", text)
        target = None
        for b in blocks:
            if self._card and self._card in b:
                target = b
                break
        scan = target if target is not None else text
        # Lines look like: "\toutput:hdmi-surround71-extra1: ... available: yes)"
        for m in re.finditer(r"^\s+(output:[\w\-]+):.*?available:\s*(yes|no)",
                             scan, re.MULTILINE):
            if m.group(2) == "yes":
                avail.add(m.group(1))
        return avail

    def _active_profile(self) -> str | None:
        text = self._pactl("list", "cards")
        blocks = re.split(r"\nCard #", text)
        scan = text
        for b in blocks:
            if self._card and self._card in b:
                scan = b
                break
        m = re.search(r"Active Profile:\s*(\S+)", scan)
        return m.group(1) if m else None

    def _denon_on(self) -> bool:
        if not (self.denon_crosscheck and self.denon is not None):
            return True  # no cross-check -> trust EDID
        try:
            st = self.denon.get_state()
            # If we can't read it, don't block on the cross-check.
            if not st.get("available"):
                return True
            return st.get("power") == "ON"
        except Exception as e:
            log.debug("denon cross-check failed: %s", e)
            return True

    # ---- core ----
    def _choose_target(self, avail: set[str]) -> str | None:
        """Return the profile we WANT active, or None if we can't decide."""
        if not self.prefer_surround:
            for p in _STEREO_FALLBACK:
                if p in avail:
                    return p
            return None
        # Receiver present (a surround profile is available) AND (optionally)
        # the device confirms power ON -> pick the richest surround profile that
        # fits within the declared speaker layout (max_channels cap: a 5.1 setup
        # won't select a 7.1 profile).
        surround_avail = [p for p in _PROFILE_PREFERENCE
                          if p in avail and "surround" in p
                          and _PROFILE_CHANNELS.get(p, 99) <= self.max_channels]
        if surround_avail and self._denon_on():
            return surround_avail[0]
        # Otherwise fall back to stereo.
        for p in _STEREO_FALLBACK:
            if p in avail:
                return p
        # Last resort: the first available anything.
        for p in _PROFILE_PREFERENCE:
            if p in avail:
                return p
        return None

    def _tick(self, force: bool = False):
        if not self._enabled or not self._card:
            return
        avail = self._available_profiles()
        if not avail:
            log.debug("no available profiles parsed — skipping")
            return
        target = self._choose_target(avail)
        if not target:
            return
        active = self._active_profile()
        if target == active and not force:
            self._last_applied = active
            return
        if target == active:
            return
        log.info("channel-sync: %s -> %s (available: %s)",
                 active, target, sorted(avail))
        self._pactl("set-card-profile", self._card, target, read=False)
        # brief settle, then confirm
        time.sleep(0.5)
        now = self._active_profile()
        if now == target:
            self._last_applied = target
            log.info("channel-sync: applied %s", target)
        else:
            log.warning("channel-sync: tried %s but active is %s", target, now)

    def _run(self):
        # Small initial delay so the session/PipeWire is fully up.
        if self._stop.wait(3):
            return
        self._tick(force=True)
        while not self._stop.wait(self.poll_secs):
            try:
                self._tick()
            except Exception as e:
                log.exception("channel-sync tick failed: %s", e)
