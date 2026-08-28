"""Per-app D-pad navigation profiles.

Most fullscreen apps we launch (Plex, the Chrome web-apps) implement proper
spatial navigation: the D-pad Up/Down/Left/Right moves an on-screen highlight and
OK activates it. For those, the default 1:1 arrow-key injection (see cec_nav.py's
KEYMAP) is exactly right, so they need NO profile.

Some apps don't. IPTVnator is an Electron/Angular app built for mouse + keyboard
shortcuts; several of its views have no arrow-key spatial navigation but *do*
honor Tab / Shift+Tab focus traversal (Chromium's built-in focus ring). For those
apps a profile remaps the D-pad onto whatever keys the app actually understands —
e.g. collapse the 2-D D-pad onto 1-D Tab traversal.

A profile is selected by matching the *focused* window's class (as reported by
kdotool getactivewindow getwindowclassname). If no profile matches, callers fall
back to the default keymap, so this module is purely additive and safe.

Data model
----------
Each profile maps a logical button name -> a list of "chords". A chord is a tuple
of Linux input keycodes pressed together (all pressed down in order, then released
in reverse) — so a single key is a 1-tuple and a modified key like Shift+Tab is
(SHIFT, TAB). A button mapped to an empty list is explicitly swallowed (injects
nothing). A button absent from the map falls through to the default keymap.

Logical button names match the CEC ui-cmd names used by cec_nav.py
(up/down/left/right/select/back/exit/root menu/contents menu/play/pause) so the
profile can be consulted with the same key string.

Used in-process by cec_nav.py. Also runnable as a CLI for other callers:
    python3 nav_profiles.py <window_class> <button>
prints space-separated ydotool `key` args (e.g. "42:1 15:1 15:0 42:0") when the
profile overrides that button, or nothing (exit 0) when it doesn't.
"""
from __future__ import annotations

import re
import sys

# Linux input event keycodes (linux/input-event-codes.h)
KEY_ESC = 1
KEY_ENTER = 28
KEY_TAB = 15
KEY_LEFTSHIFT = 42
KEY_SPACE = 57
KEY_BACKSPACE = 14

# A chord is a tuple of keycodes pressed simultaneously.
TAB = (KEY_TAB,)
SHIFT_TAB = (KEY_LEFTSHIFT, KEY_TAB)
ENTER = (KEY_ENTER,)
SPACE = (KEY_SPACE,)
ESC = (KEY_ESC,)

# Profile registry. First profile whose `match` matches the focused window class
# wins. Keep matches specific (anchored) so unrelated windows never pick up a
# profile by accident.
PROFILES: list[dict] = [
    {
        "name": "iptvnator",
        # kdotool reports this Electron app's class as exactly "iptvnator".
        "match": re.compile(r"(?i)^iptvnator$"),
        # IPTVnator lacks arrow-key spatial nav in several views but honors
        # Chromium Tab/Shift+Tab focus traversal. Collapse the D-pad onto it:
        #   Down / Right -> Tab (advance focus)
        #   Up   / Left  -> Shift+Tab (retreat focus)
        #   OK           -> Enter (activate focused control)
        #   Back         -> Esc (close dialogs / go back)
        # Arrow keys are still injected too? No — we REPLACE them, because the
        # app treats stray arrows inconsistently. If a specific IPTVnator view
        # turns out to want real arrows (e.g. the video player seek bar), add a
        # narrower profile or drop that button from this map to fall through.
        "map": {
            "down": [TAB],
            "right": [TAB],
            "up": [SHIFT_TAB],
            "left": [SHIFT_TAB],
            "select": [ENTER],
            "back": [ESC],
            "exit": [ESC],
        },
    },
]


def resolve(window_class: str) -> dict | None:
    """Return the profile matching this focused-window class, or None."""
    if not window_class:
        return None
    for prof in PROFILES:
        if prof["match"].search(window_class):
            return prof
    return None


def lookup(window_class: str, button: str) -> list[tuple[int, ...]] | None:
    """Return the chord list a profile maps `button` to, or None to fall through.

    Returns:
        None  -> no profile / button not overridden: caller uses its default.
        []    -> button explicitly swallowed (inject nothing).
        [..]  -> list of chords to inject in order.
    """
    prof = resolve(window_class)
    if prof is None:
        return None
    return prof["map"].get(button)  # None if this button isn't remapped


def chords_to_ydotool_args(chords: list[tuple[int, ...]]) -> list[str]:
    """Flatten chords into ydotool `key` args: press all down (in order), then
    release (in reverse). Chords are emitted back-to-back in one ydotool call so
    a Shift+Tab stays chorded.

        [(42,15)]      -> ["42:1","15:1","15:0","42:0"]
        [(15,)]        -> ["15:1","15:0"]
        [(42,15),(15,)]-> ["42:1","15:1","15:0","42:0","15:1","15:0"]
    """
    args: list[str] = []
    for chord in chords:
        for code in chord:
            args.append(f"{code}:1")
        for code in reversed(chord):
            args.append(f"{code}:0")
    return args


def _main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: nav_profiles.py <window_class> <button>\n")
        return 2
    window_class, button = argv[1], argv[2]
    chords = lookup(window_class, button)
    if not chords:  # None (fall through) or [] (swallow) -> print nothing
        return 0
    sys.stdout.write(" ".join(chords_to_ydotool_args(chords)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
