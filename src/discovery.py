"""Home Assistant MQTT Discovery payloads.

Publishing these (retained) to the homeassistant/ prefix makes the TV and AVR
appear automatically in HA as devices with entities — no YAML editing required.

We model each AV device with:
  - a media_player-style set of controls exposed as individual entities:
      switch  (power)
      number  (volume 0-100)
      switch  (mute)
      select  (input)
      sensor  (raw state / diagnostics)
Home Assistant's MQTT integration doesn't have a single "media_player via MQTT
discovery" schema that covers everything cleanly, so exposing discrete entities
(switch/number/select/sensor) is the most reliable, fully-featured approach.

Plus a stand-alone button for the "Media Center On" scene.
"""
from __future__ import annotations

import json

DISCOVERY_PREFIX = "homeassistant"
BASE = "avr-tv-bridge"

# Single Home Assistant device that ALL entities (hub scene/master controls, the
# Denon, the Vizio, and — from launcher_service — the launch buttons) attach to,
# so everything shows up under one "Media Center" device in HA. Entities keep
# their own unique_ids; only the device grouping is unified.
DEVICE_ID = f"{BASE}_mediacenter"
DEVICE_NAME = "Media Center"


def _shared_device() -> dict:
    return {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "model": "avr-tv-bridge",
        "manufacturer": "self-hosted",
    }


def _device_block(dev_id: str, name: str, model: str, manufacturer: str) -> dict:
    # All AV sub-devices now fold into the single shared Media Center device.
    return _shared_device()


def topic(kind: str, dev_id: str, entity: str) -> str:
    """Discovery config topic for an entity."""
    return f"{DISCOVERY_PREFIX}/{kind}/{BASE}_{dev_id}/{entity}/config"


def state_base(dev_id: str) -> str:
    return f"{BASE}/{dev_id}"


def build_entities(dev_id: str, dev_name: str, model: str, manufacturer: str,
                   inputs: list[str],
                   capabilities: tuple[str, ...] = ("power", "volume", "mute", "input"),
                   vol_max: float = 100, vol_step: float = 1,
                   label_prefix: str = "",
                   ) -> list[tuple[str, dict]]:
    """Return list of (config_topic, payload) for one AV device.

    `capabilities` selects which entities to expose. For a TV whose speakers are
    disabled (audio routed to an AVR), pass ("power", "input") so no dead volume
    slider appears in Home Assistant.

    `label_prefix` is prepended to each entity name (e.g. "AVR", "TV") so that,
    now that everything lives under one HA device, the Denon and Vizio entities
    don't both show up as an ambiguous bare "Power" / "Input".
    """
    dev = _device_block(dev_id, dev_name, model, manufacturer)
    sb = state_base(dev_id)
    avail = [{"topic": f"{BASE}/bridge/availability"},
             {"topic": f"{sb}/availability"}]
    out: list[tuple[str, dict]] = []
    pfx = f"{label_prefix} " if label_prefix else ""

    # Power switch
    if "power" in capabilities:
        out.append((topic("switch", dev_id, "power"), {
            "name": f"{pfx}Power",
            "unique_id": f"{BASE}_{dev_id}_power",
            "state_topic": f"{sb}/power/state",
            "command_topic": f"{sb}/power/set",
            "payload_on": "ON", "payload_off": "OFF",
            "availability": avail, "availability_mode": "all",
            "device": dev, "icon": "mdi:power",
        }))

    # Volume number (0-100)
    if "volume" in capabilities:
        out.append((topic("number", dev_id, "volume"), {
            "name": f"{pfx}Volume",
            "unique_id": f"{BASE}_{dev_id}_volume",
            "state_topic": f"{sb}/volume/state",
            "command_topic": f"{sb}/volume/set",
            "min": 0, "max": vol_max, "step": vol_step, "mode": "slider",
            "availability": avail, "availability_mode": "all",
            "device": dev, "icon": "mdi:volume-high",
        }))

    # Mute switch
    if "mute" in capabilities:
        out.append((topic("switch", dev_id, "mute"), {
            "name": f"{pfx}Mute",
            "unique_id": f"{BASE}_{dev_id}_mute",
            "state_topic": f"{sb}/mute/state",
            "command_topic": f"{sb}/mute/set",
            "payload_on": "ON", "payload_off": "OFF",
            "availability": avail, "availability_mode": "all",
            "device": dev, "icon": "mdi:volume-mute",
        }))

    # Input select
    if "input" in capabilities:
        out.append((topic("select", dev_id, "input"), {
            "name": f"{pfx}Input",
            "unique_id": f"{BASE}_{dev_id}_input",
            "state_topic": f"{sb}/input/state",
            "command_topic": f"{sb}/input/set",
            "options": inputs,
            "availability": avail, "availability_mode": "all",
            "device": dev, "icon": "mdi:import",
        }))

    # Diagnostic sensor (raw JSON state)
    out.append((topic("sensor", dev_id, "status"), {
        "name": f"{pfx}Status",
        "unique_id": f"{BASE}_{dev_id}_status",
        "state_topic": f"{sb}/power/state",
        "json_attributes_topic": f"{sb}/attributes",
        "availability": avail, "availability_mode": "all",
        "device": dev, "icon": "mdi:information-outline",
        "entity_category": "diagnostic",
    }))

    return out


def build_hub_entities(vol_max: float = 100, vol_step: float = 1,
                       audio_authority: str | None = "denon",
                       ) -> list[tuple[str, dict]]:
    """Entities on the 'Media Center Bridge' hub device — a single place to
    control the whole chain.

    - Media Center On / Off buttons (run the scene / power everything down)
    - Master Power switch (on = scene on, off = all off)
    - Master Volume + Master Mute, which proxy the audio-authority device (the
      receiver, or the TV in a TV-direct setup) by sharing its state/command
      topics — no extra backend logic needed. Pass audio_authority=None to omit
      them entirely (e.g. a display-only setup with no volume control).

    `audio_authority` is the MQTT device id whose volume/mute the master
    controls (defaults to "denon" so existing installs are unchanged).
    """
    dev = _shared_device()
    avail = [{"topic": f"{BASE}/bridge/availability"}]
    out: list[tuple[str, dict]] = []

    # Media Center On button
    out.append((topic("button", "hub", "media_center_on"), {
        "name": "Media Center On",
        "unique_id": f"{BASE}_scene_on",
        "command_topic": f"{BASE}/scene/media_center_on/set",
        "payload_press": "PRESS",
        "availability": avail, "device": dev, "icon": "mdi:television-play",
    }))

    # Media Center Off button
    out.append((topic("button", "hub", "media_center_off"), {
        "name": "Media Center Off",
        "unique_id": f"{BASE}_scene_off",
        "command_topic": f"{BASE}/scene/media_center_off/set",
        "payload_press": "PRESS",
        "availability": avail, "device": dev, "icon": "mdi:television-off",
    }))

    # Master Power switch (on -> scene on, off -> all off)
    out.append((topic("switch", "hub", "power"), {
        "name": "Master Power",
        "unique_id": f"{BASE}_hub_power",
        "state_topic": f"{BASE}/hub/power/state",
        "command_topic": f"{BASE}/hub/power/set",
        "payload_on": "ON", "payload_off": "OFF",
        "availability": avail, "device": dev, "icon": "mdi:power",
    }))

    # Master Volume + Mute — proxy the audio-authority device (shares its topics).
    # Omitted if there's no audio authority (e.g. display-only, no volume).
    if audio_authority:
        out.append((topic("number", "hub", "volume"), {
            "name": "Master Volume",
            "unique_id": f"{BASE}_hub_volume",
            "state_topic": f"{BASE}/{audio_authority}/volume/state",
            "command_topic": f"{BASE}/{audio_authority}/volume/set",
            "min": 0, "max": vol_max, "step": vol_step, "mode": "slider",
            "availability": avail, "device": dev, "icon": "mdi:volume-high",
        }))

        out.append((topic("switch", "hub", "mute"), {
            "name": "Master Mute",
            "unique_id": f"{BASE}_hub_mute",
            "state_topic": f"{BASE}/{audio_authority}/mute/state",
            "command_topic": f"{BASE}/{audio_authority}/mute/set",
            "payload_on": "ON", "payload_off": "OFF",
            "availability": avail, "device": dev, "icon": "mdi:volume-mute",
        }))

    return out


def dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))
