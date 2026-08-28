"""Media transport control (play/pause/next/prev) with state reporting.

Prefers MPRIS via `playerctl` (works for Chrome/Netflix, browsers, and any
MPRIS-aware app — gives real Playing/Paused state). Falls back to injecting the
multimedia key via ydotool for apps that don't expose MPRIS (e.g. the snap
Plex HTPC), which respond to the key when focused.
"""
from __future__ import annotations

import logging
import os
import subprocess

try:
    import plex_source
except ImportError:  # plex source is optional
    plex_source = None

log = logging.getLogger("media_control")

XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
YDOTOOL = os.environ.get("YDOTOOL_BIN", "/usr/local/bin/ydotool")
YDOTOOL_SOCKET = os.environ.get("YDOTOOL_SOCKET", f"{XDG_RUNTIME_DIR}/.ydotool_socket")

# Multimedia keycodes (linux/input-event-codes.h) for the ydotool fallback.
_KEYS = {
    "playpause": 164,   # KEY_PLAYPAUSE
    "play": 200,        # KEY_PLAY
    "pause": 201,       # KEY_PAUSE
    "next": 163,        # KEY_NEXTSONG
    "previous": 165,    # KEY_PREVIOUSSONG
    "stop": 166,        # KEY_STOPCD
}

# playerctl subcommands per action.
_PCTL = {
    "playpause": "play-pause",
    "play": "play",
    "pause": "pause",
    "next": "next",
    "previous": "previous",
    "stop": "stop",
}


def _have_mpris_player() -> bool:
    """True if at least one MPRIS player is registered on the session bus."""
    try:
        r = subprocess.run(["playerctl", "-l"], capture_output=True,
                           text=True, timeout=4)
        return r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _inject_key(action: str) -> bool:
    code = _KEYS.get(action)
    if code is None:
        return False
    env = dict(os.environ)
    env["YDOTOOL_SOCKET"] = YDOTOOL_SOCKET
    try:
        subprocess.run([YDOTOOL, "key", f"{code}:1", f"{code}:0"],
                       env=env, timeout=4,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("key inject %s failed: %s", action, e)
        return False


def _active_mpris_player() -> str | None:
    """Return an MPRIS player that is currently Playing (preferred) or Paused,
    so we route play/pause to whatever is actually the media in use rather than
    just whichever window has focus. None if no MPRIS player has media loaded."""
    try:
        r = subprocess.run(["playerctl", "-l"], capture_output=True,
                           text=True, timeout=4)
        players = [p for p in r.stdout.splitlines() if p.strip()]
    except (OSError, subprocess.SubprocessError):
        return None
    playing = paused = None
    for p in players:
        try:
            s = subprocess.run(["playerctl", "-p", p, "status"],
                               capture_output=True, text=True, timeout=3)
            st = s.stdout.strip().lower()
        except (OSError, subprocess.SubprocessError):
            continue
        if st == "playing" and playing is None:
            playing = p
        elif st == "paused" and paused is None:
            paused = p
    return playing or paused


def control(action: str) -> str:
    """Perform a transport action, routing to whatever is actually playing.

    Preference: an MPRIS player that has media (Playing/Paused) — this targets
    the real media in use regardless of window focus. If there's no MPRIS media
    (e.g. only Plex, which doesn't expose MPRIS), inject the media key to the
    focused window instead. Returns which path was used.
    """
    action = action.lower()
    player = _active_mpris_player()
    if player and action in _PCTL:
        try:
            r = subprocess.run(["playerctl", "-p", player, _PCTL[action]],
                               capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                log.info("media %s via MPRIS (%s)", action, player)
                return "mpris"
            log.info("playerctl -p %s %s rc=%s (%s) — falling back to key",
                     player, action, r.returncode, r.stderr.strip())
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("playerctl %s failed: %s — falling back", action, e)
    # Fallback: inject the media key to the focused window (for Plex etc.)
    if _inject_key(action):
        log.info("media %s via key injection", action)
        return "key"
    log.warning("media %s: no control path worked", action)
    return "failed"


def status() -> str:
    """Return 'playing' / 'paused' / 'stopped' / 'idle' (best effort).

    Prefers MPRIS via playerctl (Chrome/Netflix/browsers). MPRIS is blind to the
    snap Plex HTPC (it registers NO MPRIS player), so when MPRIS reports nothing
    we fall back to the Plex Media Server API (plex_source) — this is what lets
    idle-off correctly DEFER while Plex is playing instead of powering off
    mid-movie. Returns 'idle' only when neither source sees active media.
    """
    try:
        r = subprocess.run(["playerctl", "status"], capture_output=True,
                           text=True, timeout=4)
        if r.returncode == 0:
            st = r.stdout.strip().lower()
            if st:
                return st
    except (OSError, subprocess.SubprocessError):
        pass
    # MPRIS saw nothing — ask Plex (this host's session) if it's configured.
    st = _plex_status()
    return st or "idle"


def _plex_status() -> str | None:
    """'playing'/'paused' from the Plex server for THIS host, or None."""
    if plex_source is None or not plex_source.enabled():
        return None
    try:
        np = plex_source.now_playing()
    except Exception as e:  # never let a Plex hiccup break idle logic
        log.debug("plex status check failed: %s", e)
        return None
    if np and np.get("state") in ("playing", "paused"):
        return np["state"]
    return None


def _meta(field: str) -> str:
    try:
        r = subprocess.run(["playerctl", "metadata", field],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def now_playing() -> str:
    """Human-friendly 'now playing' string from MPRIS metadata.

    Combines artist + title when both exist (e.g. "Artist — Title"), else just
    the title. NOTE: some sites (Netflix) only report the app name as the title
    and populate nothing else, so for those this is just "Netflix".
    """
    title = _meta("xesam:title")
    artist = _meta("xesam:artist")
    if artist and title and artist.lower() != title.lower():
        return f"{artist} — {title}"
    return title


def metadata() -> dict:
    """Full metadata dict for the HA sensor attributes (title/artist/album)."""
    return {
        "title": _meta("xesam:title"),
        "artist": _meta("xesam:artist"),
        "album": _meta("xesam:album"),
    }


def _mpris_position() -> int:
    """Current playback position in whole seconds (0 if unavailable)."""
    try:
        r = subprocess.run(["playerctl", "position"], capture_output=True,
                           text=True, timeout=3)
        if r.returncode == 0 and r.stdout.strip():
            return int(float(r.stdout.strip()))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return 0


def _mpris_duration() -> int:
    """Track length in whole seconds from mpris:length (microseconds) (0 if n/a)."""
    raw = _meta("mpris:length")
    try:
        return int(raw) // 1_000_000 if raw else 0
    except ValueError:
        return 0


def now_playing_snapshot() -> dict:
    """Unified 'now playing' snapshot from the best available source.

    Source selection: if Plex is configured AND is actively playing on this
    machine (its REST API is far richer than MPRIS — real show/episode title,
    poster art, position), use Plex. Otherwise fall back to MPRIS (Netflix and
    other Chrome web-apps, browsers). Returns a normalized dict:

      {"source": "plex"|"mpris"|"none",
       "state": "playing"|"paused"|"idle",
       "title": str, "subtitle": str, "art": str,
       "position": int_sec, "duration": int_sec,
       "media_type": "tvshow"|"movie"|"music"|"video"|""}

    Always returns a dict (state 'idle' when nothing is playing).
    """
    # 1. Plex (richest) — only when it's actually the active playback here.
    if plex_source is not None and plex_source.enabled():
        try:
            snap = plex_source.now_playing()
        except Exception as e:  # never let Plex errors break status
            log.debug("plex now_playing failed: %s", e)
            snap = None
        if snap:
            return snap

    # 2. MPRIS (Netflix / Chrome apps / browsers).
    st = status()  # playing/paused/stopped/idle
    if st in ("playing", "paused"):
        title = _meta("xesam:title")
        artist = _meta("xesam:artist")
        # Netflix reports only "Netflix" as title with empty artist/album; keep
        # what we have — the notification layer decides how to present it.
        subtitle = artist if (artist and artist.lower() != title.lower()) else ""
        return {
            "source": "mpris",
            "state": st,
            "title": title,
            "subtitle": subtitle,
            "art": _meta("mpris:artUrl"),
            "position": _mpris_position(),
            "duration": _mpris_duration(),
            "media_type": "video",
        }

    # 3. Nothing playing we can see.
    return {
        "source": "none", "state": "idle", "title": "", "subtitle": "",
        "art": "", "position": 0, "duration": 0, "media_type": "",
    }
