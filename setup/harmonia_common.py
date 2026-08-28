"""Shared helpers for the Harmonia setup wizard and doctor.

Keeps SSDP discovery, config.env read/merge/write, dependency checks, and small
terminal helpers in one place so harmonia_setup.py and harmonia_doctor.py stay
focused on flow. No third-party deps (stdlib only) so it runs before the venv is
populated.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time

CONFIG_DIR = os.path.expanduser("~/.config/avr-tv-bridge")
CONFIG_ENV = os.path.join(CONFIG_DIR, "config.env")


# ---- terminal ---------------------------------------------------------------

def _tty() -> bool:
    return sys.stdout.isatty()


def c(text: str, color: str) -> str:
    if not _tty():
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "cyan": "36",
             "bold": "1", "dim": "2"}
    return f"\033[{codes.get(color,'0')}m{text}\033[0m"


def ok(msg): print(f"  {c('✓', 'green')} {msg}")
def warn(msg): print(f"  {c('!', 'yellow')} {msg}")
def bad(msg): print(f"  {c('✗', 'red')} {msg}")
def info(msg): print(f"  {c('·', 'dim')} {msg}")
def head(msg): print(f"\n{c(msg, 'bold')}")


def ask(prompt: str, default: str = "") -> str:
    sfx = f" [{default}]" if default else ""
    try:
        v = input(f"{prompt}{sfx}: ").strip()
    except EOFError:
        return default
    return v or default


def confirm(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    v = ask(f"{prompt} ({d})").lower()
    if not v:
        return default
    return v in ("y", "yes")


# ---- SSDP discovery ---------------------------------------------------------

def ssdp_scan(timeout: float = 3.0) -> list[tuple[str, str]]:
    """Broadcast an SSDP M-SEARCH and collect (ip, raw_headers) responses.

    Returns one entry per response line-block; multiple per device is fine —
    drivers dedupe. Stdlib-only.
    """
    msg = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        "HOST:239.255.255.250:1900",
        'MAN:"ssdp:discover"',
        "MX:2",
        "ST:ssdp:all",
        "", ""]).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(timeout)
    results: list[tuple[str, str]] = []
    try:
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                break
            except OSError:
                break
            results.append((addr[0], data.decode("utf-8", "ignore")))
    finally:
        s.close()
    return results


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


# ---- config.env read / merge / write ---------------------------------------

def read_env_file(path: str = CONFIG_ENV) -> dict:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if m and not line.lstrip().startswith("#"):
                out[m.group(1)] = m.group(2)
    return out


def merge_env(updates: dict, path: str = CONFIG_ENV,
              template: str | None = None) -> None:
    """Idempotently set KEY=VALUE lines in config.env.

    - Preserves comments/order for existing keys (rewrites the value in place).
    - Appends new keys under a '# --- added by harmonia-setup ---' section.
    - If the file doesn't exist and a template is given, starts from it.
    Reversible/safe: writes a .bak of the prior file first.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines: list[str] = []
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
        with open(path) as f:
            lines = f.read().splitlines()
    elif template and os.path.exists(template):
        with open(template) as f:
            lines = f.read().splitlines()

    remaining = dict(updates)
    for i, line in enumerate(lines):
        m = re.match(r"(\s*)([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m and not line.lstrip().startswith("#"):
            key = m.group(2)
            if key in remaining:
                lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        lines.append("")
        lines.append("# --- added by harmonia-setup ---")
        for k, v in remaining.items():
            lines.append(f"{k}={v}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---- dependency checks ------------------------------------------------------

def have_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def check_dependencies(need_cec: bool, need_ir: bool,
                       need_audio: bool) -> list[tuple[str, bool, str]]:
    """Return [(label, ok, hint)] for the tools each enabled feature needs."""
    checks: list[tuple[str, bool, str]] = []

    def add(label, present, hint):
        checks.append((label, present, hint))

    add("python3", have_cmd("python3"), "apt install python3")
    add("pip (venv)", have_cmd("pip") or have_cmd("pip3"),
        "apt install python3-pip python3-venv")
    if need_audio:
        add("pactl (PipeWire/Pulse)", have_cmd("pactl"),
            "apt install pipewire-pulse (or pulseaudio-utils)")
        add("wpctl (WirePlumber)", have_cmd("wpctl"),
            "apt install wireplumber  (needed for upmix drop-in)")
    if need_cec:
        add("cec-client (libcec)", have_cmd("cec-client"),
            "apt install cec-utils  (only if using HARMONIA_DISPLAY=cec)")
    if need_ir:
        add("irexec (LIRC)", have_cmd("irexec"),
            "apt install lirc  (only if NAV_MODE=ir)")
    return checks
