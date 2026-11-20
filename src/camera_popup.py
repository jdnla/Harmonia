"""Camera auto-popup on person/object detection.

Shows a small camera-stream overlay in a screen corner when a trigger fires
(e.g. Frigate person detection), and closes it a short grace period after the
trigger clears. Also exposes a manual toggle (a remote button) and an HA on/off
switch.

Implementation: a **Wayland layer-shell** overlay (a small QML app run with
`qml6`, using org.kde.layershell + QtMultimedia). This is the reliable approach
on KDE Plasma Bigscreen, whose shell forces normal windows to maximized and
non-movable — a layer-shell surface self-positions in the corner, is immune to
that window management, renders ABOVE fullscreen apps (so it shows over Plex),
and never takes keyboard focus (the foreground app keeps it; you can still switch
apps underneath). It works the same in the normal Plasma desktop.

An earlier mpv-based version needed KWin scripting to place the window and to
lower fullscreen apps; none of that worked under Bigscreen .
The layer-shell overlay handles placement, layering, focus, and animation
natively via the QML file (campopup.qml), so this module just launches/kills it.

Stream URL is anything QtMultimedia/ffmpeg can play. A go2rtc HTTP endpoint
avoids embedding camera credentials, e.g.:
  http://<go2rtc-host>:1984/api/stream.mp4?src=<stream-name>
Prefer a lower-res "sub" stream — the 4K main stream is HEVC and shows decode
glitches when joined mid-GOP; the h264 sub stream is clean and plenty for a
corner popup.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading

from session_env import graphical_env

log = logging.getLogger("camera_popup")

# The QML overlay lives next to this file.
_QML_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "campopup.qml")


class CameraPopup:
    def __init__(self,
                 stream_url: str,
                 window_title: str = "CameraPopup",
                 grace_secs: float = 10.0,
                 width: int = 640, height: int = 360,
                 corner: str = "top-right", margin: int = 16,
                 hwdec: str = "no"):
        """
        stream_url  : any QtMultimedia-playable URL (e.g. go2rtc HTTP MP4).
        window_title: label (kept for API compatibility / logging).
        grace_secs  : seconds to keep the overlay after the count hits 0.
        width/height: overlay size in logical px.
        corner      : top-right | top-left | bottom-right | bottom-left.
        margin      : gap from the screen edges, in px.
        hwdec       : accepted for API compatibility (QtMultimedia picks its own
                      decode path); not used.
        """
        self.stream_url = stream_url
        self.window_title = window_title
        self.grace_secs = grace_secs
        self.width = width
        self.height = height
        self.corner = corner
        self.margin = margin
        # Entrance animation: "slide" (default), "fade", or "none".
        self.anim = os.environ.get("CAM_POPUP_ANIM", "slide").strip().lower()
        try:
            self.anim_ms = int(os.environ.get("CAM_POPUP_ANIM_MS", "260"))
        except ValueError:
            self.anim_ms = 260
        # Which qml runtime to use.
        self.qml_bin = os.environ.get("CAM_POPUP_QML_BIN", "qml6")
        # Optional instant still-snapshot shown while the live video connects
        # (the h264 stream takes ~3-5s to first frame; a JPEG loads in ~0.1s, so
        # this removes the initial black gap). e.g. a Frigate latest.jpg or a
        # go2rtc frame.jpeg URL. Empty => no placeholder (starts black).
        self.snapshot_url = os.environ.get("CAM_POPUP_SNAPSHOT_URL", "")
        # ffmpeg stream-probe size (bytes) for the video player. Smaller = the
        # decoder commits sooner (shaves ~0.7s off first-frame on the fragmented
        # mp4 stream). 4096 is a good low value; 0/negative keeps the default.
        try:
            self.probe_size = int(os.environ.get("CAM_POPUP_PROBE_SIZE", "4096"))
        except ValueError:
            self.probe_size = 4096

        self._enabled = True
        self._proc: subprocess.Popen | None = None
        self._close_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._env: dict[str, str] = {}

    # ---- enable/disable (HA toggle) ----
    def set_enabled(self, on: bool):
        self._enabled = on
        log.info("%s auto-popup %s", self.window_title,
                 "enabled" if on else "disabled")
        if not on:
            self.close()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def toggle(self):
        """Manual toggle (e.g. a remote button): close if showing, else open.
        Bypasses the enable flag/grace timer — an explicit user action."""
        with self._lock:
            showing = self._is_open()
        if showing:
            self.close()
        else:
            self._cancel_close_timer()
            self.open()

    # ---- detection-count events ----
    def on_count(self, count: int):
        """Called with the current detection count from the trigger topic."""
        if not self._enabled:
            return
        if count > 0:
            self._cancel_close_timer()
            self.open()
        else:
            self._schedule_close()

    # ---- window control ----
    def _is_open(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _render_qml(self) -> str | None:
        """Write a concrete QML file from the template with our settings baked
        in, and return its path (caller cleans it up). Returns None on error."""
        try:
            tmpl = open(_QML_TEMPLATE).read()
        except OSError as e:
            log.error("cannot read QML template %s: %s", _QML_TEMPLATE, e)
            return None
        subst = {
            "%STREAM_URL%": self.stream_url,
            "%SNAPSHOT_URL%": self.snapshot_url,
            "%WIDTH%": str(self.width),
            "%HEIGHT%": str(self.height),
            "%MARGIN%": str(self.margin),
            "%CORNER%": self.corner,
            "%ANIM%": self.anim,
            "%ANIM_MS%": str(self.anim_ms),
            "%PROBE_SIZE%": str(self.probe_size),
        }
        for k, v in subst.items():
            tmpl = tmpl.replace(k, v)
        runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        path = f"{runtime}/campopup-{os.getpid()}.qml"
        try:
            with open(path, "w") as f:
                f.write(tmpl)
        except OSError as e:
            log.error("cannot write QML file: %s", e)
            return None
        return path

    def open(self):
        with self._lock:
            if self._is_open():
                return  # already showing
            self._env = graphical_env()
            qml_path = self._render_qml()
            if not qml_path:
                return
            self._qml_path = qml_path
            cmd = [self.qml_bin, qml_path]
            log.info("Opening %s overlay", self.window_title)
            try:
                self._proc = subprocess.Popen(
                    cmd, env=self._env, start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                log.error("Failed to open %s overlay (%s missing?): %s",
                          self.window_title, self.qml_bin, e)
                self._proc = None
                self._cleanup_qml()

    def close(self):
        with self._lock:
            self._cancel_close_timer_locked()
            if self._is_open():
                log.info("Closing %s overlay", self.window_title)
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            self._proc = None
        self._cleanup_qml()

    def _cleanup_qml(self):
        path = getattr(self, "_qml_path", "")
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
            self._qml_path = ""

    def _schedule_close(self):
        with self._lock:
            self._cancel_close_timer_locked()
            if not self._is_open():
                return
            log.info("Count 0 — closing %s overlay in %.0fs",
                     self.window_title, self.grace_secs)
            self._close_timer = threading.Timer(self.grace_secs, self.close)
            self._close_timer.daemon = True
            self._close_timer.start()

    def _cancel_close_timer(self):
        with self._lock:
            self._cancel_close_timer_locked()

    def _cancel_close_timer_locked(self):
        if self._close_timer is not None:
            self._close_timer.cancel()
            self._close_timer = None
