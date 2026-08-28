"""Home Assistant notification toasts on the TV.

Plasma Bigscreen ships no org.freedesktop.Notifications server, so notify-send /
the usual notification path does nothing (the name has no owner). This renders
our own toast: a Wayland layer-shell overlay (QML via qml6) shown in a screen
corner with a title, body text, and an optional icon/image, auto-dismissing after
a timeout. Same layer-shell approach as the camera popup — floats above
fullscreen apps, never steals focus, works under Bigscreen.

Driven over MQTT: Home Assistant publishes a JSON payload to a topic (default
avr-tv-bridge/notify) and the launcher service calls show() here. Only what HA
explicitly sends appears — there's no mirroring of HA's internal notifications.

Payload (all fields optional except one of title/body):
  {"title": "Doorbell", "body": "Someone at the door",
   "image": "http://homeassistant:8123/local/snap.jpg", "timeout": 8}
timeout is in seconds. A bare string payload is treated as the body.

Each notification is an independent short-lived qml6 process, so multiple can be
shown without shared state; the layer-shell compositor stacks them.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading

from session_env import graphical_env

log = logging.getLogger("notify_popup")

_QML_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "notify_popup.qml")


class NotifyPopup:
    def __init__(self):
        self.qml_bin = os.environ.get("CAM_POPUP_QML_BIN", "qml6")
        self.width = int(os.environ.get("NOTIFY_WIDTH", "560"))
        self.height = int(os.environ.get("NOTIFY_HEIGHT", "150"))
        self.corner = os.environ.get("NOTIFY_CORNER", "top-left")
        self.margin = int(os.environ.get("NOTIFY_MARGIN", "16"))
        self.default_timeout = float(os.environ.get("NOTIFY_TIMEOUT", "8"))
        self.anim_ms = int(os.environ.get("NOTIFY_ANIM_MS", "260"))
        # Styling (Breeze-ish defaults; all overridable via NOTIFY_* env).
        self.accent = os.environ.get("NOTIFY_ACCENT", "#3daee9")
        self.bg_color = os.environ.get("NOTIFY_BG", "#ee1b1e20")
        self.title_color = os.environ.get("NOTIFY_TITLE_COLOR", "#ffffff")
        self.body_color = os.environ.get("NOTIFY_BODY_COLOR", "#c8ccd0")
        self.radius = int(os.environ.get("NOTIFY_RADIUS", "12"))
        self.title_size = int(os.environ.get("NOTIFY_TITLE_SIZE", "24"))
        self.body_size = int(os.environ.get("NOTIFY_BODY_SIZE", "18"))
        self.font = os.environ.get("NOTIFY_FONT", "Noto Sans")
        self.show_progress = os.environ.get("NOTIFY_PROGRESS", "1") not in ("0", "false", "False")
        self.show_shadow = os.environ.get("NOTIFY_SHADOW", "1") not in ("0", "false", "False")
        self._env: dict[str, str] = {}

    def show_json(self, raw: str):
        """Parse an MQTT payload (JSON object, or a bare string = body) and show.

        Recognized JSON keys: title, body/message, image/icon, timeout (sec), and
        per-notification style overrides: accent, bg, title_color, body_color,
        corner, radius, title_size, body_size, font, width, height, progress
        (bool), shadow (bool). Any omitted key uses the NOTIFY_* config default.
        """
        raw = (raw or "").strip()
        if not raw:
            return
        title = body = image = ""
        timeout = self.default_timeout
        opts: dict = {}
        if raw[:1] == "{":
            try:
                d = json.loads(raw)
                title = str(d.get("title", "") or "")
                body = str(d.get("body", d.get("message", "")) or "")
                image = str(d.get("image", d.get("icon", "")) or "")
                if d.get("timeout") is not None:
                    timeout = float(d["timeout"])
                # collect any style overrides present
                for k in ("accent", "bg", "title_color", "body_color", "corner",
                          "radius", "title_size", "body_size", "font",
                          "width", "height", "progress", "shadow"):
                    if k in d:
                        opts[k] = d[k]
            except (ValueError, TypeError) as e:
                log.warning("bad notify JSON (%s) — showing raw as body", e)
                body = raw
        else:
            body = raw
        if not title and not body:
            return
        self.show(title, body, image, timeout, opts)

    def show(self, title: str = "", body: str = "", image: str = "",
             timeout: float | None = None, opts: dict | None = None):
        """Show a toast. Returns a handle (the QML subprocess.Popen) so the
        caller can dismiss it early via dismiss(); returns None on failure. The
        toast still self-dismisses after `timeout` regardless."""
        if timeout is None:
            timeout = self.default_timeout
        self._env = graphical_env()
        qml_path = self._render(title, body, image, timeout, opts or {})
        if not qml_path:
            return None
        cmd = [self.qml_bin, qml_path]
        log.info("Notify: %r / %r%s", title, body,
                 " +image" if image else "")
        try:
            proc = subprocess.Popen(
                cmd, env=self._env, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.error("failed to show notification (%s missing?): %s",
                      self.qml_bin, e)
            self._cleanup(qml_path)
            return None
        # Clean up the temp QML file after the toast lifetime (+slack), without
        # blocking the caller.
        life = timeout + (2 * self.anim_ms / 1000.0) + 2.0
        t = threading.Timer(life, self._cleanup, args=(qml_path,))
        t.daemon = True
        t.start()
        return proc

    @staticmethod
    def dismiss(proc):
        """Dismiss a toast early by terminating its QML process. Safe to call
        with None or an already-exited process. Used to clear the idle-off
        warning the moment input cancels the countdown, so the popup doesn't
        linger for its full timeout after the power-off is aborted."""
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass

    def _render(self, title, body, image, timeout, opts: dict) -> str | None:
        try:
            tmpl = open(_QML_TEMPLATE).read()
        except OSError as e:
            log.error("cannot read notify QML template: %s", e)
            return None
        # base64 the text so arbitrary characters/newlines survive substitution.
        def b64(s: str) -> str:
            return base64.b64encode(s.encode("utf-8")).decode("ascii")
        # per-notification override or instance default
        def o(key, default):
            return opts[key] if key in opts else default
        def obool(key, default):
            v = opts.get(key, default)
            return "true" if (v in (True, 1, "1", "true", "True")) else "false"
        subst = {
            "%TITLE_B64%": b64(title),
            "%BODY_B64%": b64(body),
            "%IMAGE_URL%": image,
            "%WIDTH%": str(o("width", self.width)),
            "%HEIGHT%": str(o("height", self.height)),
            "%MARGIN%": str(self.margin),
            "%CORNER%": str(o("corner", self.corner)),
            "%TIMEOUT_MS%": str(int(timeout * 1000)),
            "%ANIM_MS%": str(self.anim_ms),
            "%ACCENT%": str(o("accent", self.accent)),
            "%BG_COLOR%": str(o("bg", self.bg_color)),
            "%TITLE_COLOR%": str(o("title_color", self.title_color)),
            "%BODY_COLOR%": str(o("body_color", self.body_color)),
            "%RADIUS%": str(o("radius", self.radius)),
            "%TITLE_SIZE%": str(o("title_size", self.title_size)),
            "%BODY_SIZE%": str(o("body_size", self.body_size)),
            "%FONT%": str(o("font", self.font)),
            "%SHOW_PROGRESS%": obool("progress", self.show_progress),
            "%SHOW_SHADOW%": obool("shadow", self.show_shadow),
        }
        for k, v in subst.items():
            tmpl = tmpl.replace(k, v)
        runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        path = f"{runtime}/notify-{os.getpid()}-{id(tmpl) & 0xffffff}.qml"
        try:
            with open(path, "w") as f:
                f.write(tmpl)
        except OSError as e:
            log.error("cannot write notify QML: %s", e)
            return None
        return path

    @staticmethod
    def _cleanup(path: str):
        try:
            os.unlink(path)
        except OSError:
            pass
