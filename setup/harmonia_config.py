#!/usr/bin/env python3
"""harmonia config — view and edit config.env from the CLI.

Subcommands:
  list                 Print all current KEY=VALUE pairs (secrets masked).
  get <KEY>            Print one value (raw, unmasked — for scripting).
  set <KEY> <VALUE>    Set/replace a value in place (a .bak is kept).
  unset <KEY>          Remove a key (comment it out; keeps a .bak).

Values are written via harmonia_common.merge_env, which preserves comments/order
and backs up the previous file. After a `set`, restart the affected service:
  harmonia restart            (all)   or   harmonia restart bridge
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import harmonia_common as hc  # noqa: E402

# Keys whose values should be masked in `list` output.
_SECRET_HINTS = ("PASS", "TOKEN", "SECRET", "KEY")


def _is_secret(key: str) -> bool:
    return any(h in key.upper() for h in _SECRET_HINTS)


def cmd_list():
    env = hc.read_env_file()
    if not env:
        hc.warn(f"no config at {hc.CONFIG_ENV} — run 'harmonia setup'")
        return 1
    for k in sorted(env):
        v = env[k]
        if _is_secret(k) and v:
            v = "••••••"
        print(f"  {k}={v}")
    return 0


def cmd_get(key: str):
    env = hc.read_env_file()
    if key not in env:
        hc.bad(f"{key} not set")
        return 1
    print(env[key])  # raw, unmasked, no decoration (scripting-friendly)
    return 0


def cmd_set(key: str, value: str):
    if not key.replace("_", "").isalnum():
        hc.bad(f"invalid key: {key!r}")
        return 2
    template = os.path.join(os.path.dirname(HERE), "config.env.example")
    hc.merge_env({key: value}, template=template)
    shown = "••••••" if _is_secret(key) else value
    hc.ok(f"set {key}={shown}  (backup: {hc.CONFIG_ENV}.bak)")
    hc.info("apply with: harmonia restart   (or restart the affected service)")
    return 0


def cmd_unset(key: str):
    path = hc.CONFIG_ENV
    if not os.path.exists(path):
        hc.bad("no config file"); return 1
    import shutil
    shutil.copy2(path, path + ".bak")
    out = []
    hit = False
    with open(path) as f:
        for line in f:
            if line.lstrip().startswith("#"):
                out.append(line); continue
            k = line.split("=", 1)[0].strip()
            if k == key:
                out.append(f"# {line.rstrip()}  # unset by harmonia config\n")
                hit = True
            else:
                out.append(line)
    if not hit:
        hc.warn(f"{key} not present"); return 1
    with open(path, "w") as f:
        f.writelines(out)
    hc.ok(f"unset {key}  (backup: {path}.bak)")
    return 0


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__); return 0
    sub, rest = args[0], args[1:]
    if sub == "list":
        return cmd_list()
    if sub == "get":
        if len(rest) != 1:
            hc.bad("usage: harmonia config get <KEY>"); return 2
        return cmd_get(rest[0])
    if sub == "set":
        if len(rest) < 2:
            hc.bad("usage: harmonia config set <KEY> <VALUE>"); return 2
        return cmd_set(rest[0], " ".join(rest[1:]))
    if sub == "unset":
        if len(rest) != 1:
            hc.bad("usage: harmonia config unset <KEY>"); return 2
        return cmd_unset(rest[0])
    hc.bad(f"unknown config subcommand: {sub}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
