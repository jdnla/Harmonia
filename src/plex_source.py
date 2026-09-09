"""Plex 'now playing' source — richer metadata than MPRIS for the Plex HTPC.

The snap Plex HTPC exposes NO MPRIS, so MPRIS can tell us nothing about what
Plex is playing. But the Plex Media Server has a REST API that does: query
`/status/sessions` and it returns the real episode/movie title, show name,
poster art, live position, and duration for every active playback session.

This module asks the PMS what is playing ON THIS MACHINE (matched by the
player's LAN address) and returns a normalized snapshot the bridge can publish.
Stdlib only (urllib + ElementTree) — no plexapi dependency in the venv.

Config (config.env):
  PLEX_BASE_URL      e.g. http://192.0.2.7:32400   (required to enable)
  PLEX_TOKEN         X-Plex-Token for the server      (required; SECRET)
  PLEX_PLAYER_ADDRESS  LAN IP of this HTPC's Plex client, to pick our own
                     session out of all sessions. Default: auto (this host's
                     primary IP). Set explicitly if auto-detect is wrong.
  PLEX_MACHINE_ID    optional: match the player by its machineIdentifier
                     instead of address (more robust if the IP varies).

Everything is best-effort: any failure returns None so the caller falls back to
MPRIS. The token is read from the environment and never logged.
"""
from __future__ import annotations

import logging
import os
import socket
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

log = logging.getLogger("plex_source")

PLEX_BASE_URL = os.environ.get("PLEX_BASE_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_PLAYER_ADDRESS = os.environ.get("PLEX_PLAYER_ADDRESS", "")
PLEX_MACHINE_ID = os.environ.get("PLEX_MACHINE_ID", "")
_TIMEOUT = float(os.environ.get("PLEX_TIMEOUT", "4"))


def enabled() -> bool:
    return bool(PLEX_BASE_URL and PLEX_TOKEN)


def _this_host_ip() -> str:
    """Best-effort primary LAN IP of this machine (to match our Plex session)."""
    if PLEX_PLAYER_ADDRESS:
        return PLEX_PLAYER_ADDRESS
    try:
        # No traffic actually sent; just picks the outbound interface.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.7", 80))  # PMS host; any LAN addr works
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def _fetch_sessions_xml() -> ET.Element | None:
    url = f"{PLEX_BASE_URL}/status/sessions"
    req = urllib.request.Request(url, headers={
        "X-Plex-Token": PLEX_TOKEN,
        "Accept": "application/xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
        return ET.fromstring(data)
    except (urllib.error.URLError, ET.ParseError, OSError) as e:
        log.debug("plex sessions fetch failed: %s", e)
        return None


def _art_url(node: ET.Element) -> str:
    """Absolute, tokenized poster URL. Prefer the show/grandparent art for TV so
    the notification shows the series poster; fall back to the item's own thumb."""
    for attr in ("grandparentThumb", "parentThumb", "thumb", "art"):
        rel = node.get(attr)
        if rel:
            sep = "&" if "?" in rel else "?"
            return f"{PLEX_BASE_URL}{rel}{sep}X-Plex-Token={PLEX_TOKEN}"
    return ""


def _title_for(node: ET.Element) -> tuple[str, str]:
    """Return (primary_title, secondary_line) for display.

    TV episode: primary = show name (grandparentTitle), secondary = 'S1·E3 —
    Episode Title'. Movie/other: primary = title, secondary = year/artist-ish.
    """
    typ = node.get("type", "")
    title = node.get("title", "") or ""
    if typ == "episode":
        show = node.get("grandparentTitle", "") or ""
        s = node.get("parentIndex")
        e = node.get("index")
        se = ""
        if s and e:
            se = f"S{s}·E{e} — "
        elif e:
            se = f"E{e} — "
        return (show or title, f"{se}{title}")
    if typ == "track":
        # music: title + artist/album
        artist = node.get("grandparentTitle", "") or node.get("originalTitle", "")
        return (title, artist)
    # movie / clip / other
    year = node.get("year", "")
    return (title, str(year) if year else "")


def now_playing() -> dict | None:
    """Snapshot of what Plex is playing on THIS machine, or None.

    Returns a dict shaped like:
      {"source": "plex", "state": "playing"|"paused",
       "title": <primary>, "subtitle": <secondary>,
       "art": <url>, "position": <sec>, "duration": <sec>,
       "media_type": "tvshow"|"movie"|"music"|"video"}
    None when Plex isn't configured, unreachable, or nothing is playing here.
    """
    if not enabled():
        return None
    root = _fetch_sessions_xml()
    if root is None:
        return None
    my_ip = _this_host_ip()
    # Each session is a <Video>/<Track>/<Photo> with a nested <Player>.
    for node in list(root):
        player = node.find("Player")
        if player is None:
            continue
        # Match the session running on OUR client.
        if PLEX_MACHINE_ID:
            if player.get("machineIdentifier", "") != PLEX_MACHINE_ID:
                continue
        elif my_ip:
            if player.get("address", "") != my_ip:
                continue
        # else: no way to disambiguate — take the first (single-client setups)
        state = (player.get("state", "") or "").lower()  # playing/paused/buffering
        if state == "buffering":
            state = "playing"
        if state not in ("playing", "paused"):
            continue
        primary, secondary = _title_for(node)
        typ = node.get("type", "")
        media_type = {"episode": "tvshow", "movie": "movie",
                      "track": "music"}.get(typ, "video")
        try:
            pos = int(node.get("viewOffset", "0")) // 1000
        except ValueError:
            pos = 0
        try:
            dur = int(node.get("duration", "0")) // 1000
        except ValueError:
            dur = 0
        return {
            "source": "plex",
            "state": state,
            "title": primary,
            "subtitle": secondary,
            "art": _art_url(node),
            "position": pos,
            "duration": dur,
            "media_type": media_type,
        }
    return None
