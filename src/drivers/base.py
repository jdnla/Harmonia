"""Abstract device roles for Harmonia.

Three roles, each an ABC that concrete drivers implement:

  * AVReceiver  — an audio/video receiver (Denon, Yamaha, Sonos, ...). Owns
                  master volume/mute and (usually) input switching + power.
  * Display     — a TV / projector (Vizio, LG, generic CEC, ...). Owns power and
                  input; may or may not own volume (only if audio runs through
                  the TV rather than a receiver).
  * AudioOutput — the PC-side audio sink management (PipeWire channel layout /
                  upmix). Not a physical device but the software that shapes what
                  we send downstream.

Design goals:
  * A setup may have ANY subset. TV-direct (no receiver) is a first-class case:
    receiver is None, and the Display provides volume/mute.
  * Every device reports a normalized state dict from get_state():
        {"available": bool, "power": "ON"/"OFF", "input": str,
         "volume": float(0-100), "mute": bool}
    Keys other than "available" are optional — include only what the device
    exposes. The bridge already tolerates missing keys.
  * Capabilities are declared, not guessed, so HA discovery only publishes
    entities the device actually supports.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


# ---- Capabilities -----------------------------------------------------------

# Canonical capability tokens a device may declare.
CAP_POWER = "power"
CAP_VOLUME = "volume"
CAP_MUTE = "mute"
CAP_INPUT = "input"

ALL_CAPS = (CAP_POWER, CAP_VOLUME, CAP_MUTE, CAP_INPUT)


@dataclass
class SpeakerLayout:
    """Describes a speaker configuration for AudioOutput / channel-sync.

    channels is the nominal stream channel count we send (2, 6, 8). The
    has_center / has_sub flags let the channel-sync + upmix logic reason about
    phantom-center folding etc. For a receiver that lacks a center speaker we
    still send a standard layout (e.g. 8ch/7.1) and let the receiver fold — but
    knowing the truth lets us log/label correctly and, for TV-direct, avoid
    sending channels the TV can't use.
    """
    channels: int = 2
    has_center: bool = False
    has_sub: bool = False
    name: str = "stereo"

    @property
    def is_surround(self) -> bool:
        return self.channels > 2


# ---- Discovery --------------------------------------------------------------

@dataclass
class DiscoveredDevice:
    """A candidate device a driver's discover() found on the network.

    The setup wizard shows these to the user and, on confirmation, writes the
    corresponding config (host + any env the driver needs). `env_updates` are the
    config.env key/values that select+point-at this device.
    """
    driver_name: str            # registry name, e.g. "denon"
    role: str                   # "receiver" | "display"
    host: str                   # IP / hostname
    label: str                  # human summary, e.g. "Denon AVR-X1800H"
    env_updates: dict = field(default_factory=dict)
    #: extra one-time step still needed (e.g. "pair"), or "" if ready to use.
    followup: str = ""


# ---- Device role ABCs -------------------------------------------------------

class Device(abc.ABC):
    """Common base: an addressable device with a normalized state read."""

    #: short stable id used in MQTT topics / registry keys, e.g. "denon".
    role_id: str = "device"

    #: capability tokens this instance exposes (subset of ALL_CAPS).
    capabilities: tuple[str, ...] = ()

    @abc.abstractmethod
    def get_state(self) -> dict:
        """Return the normalized state dict (see module docstring)."""

    def has(self, cap: str) -> bool:
        return cap in self.capabilities

    # ---- Optional plugin hooks (safe defaults so drivers need not implement) --
    @classmethod
    def discover(cls, ssdp_results: list, timeout: float = 3.0) -> list:
        """Return a list[DiscoveredDevice] this driver recognizes on the LAN.

        `ssdp_results` is a shared list of (ip, headers_text) the wizard collects
        once and passes to every driver, so each driver just filters it rather
        than re-scanning. Drivers with no network presence return []. Override in
        drivers that can be auto-found. Default: nothing found.
        """
        return []

    @classmethod
    def selftest(cls, env) -> tuple[bool, str]:
        """Optional connectivity self-test for `harmonia doctor`.

        Build from env (like from_env), attempt a state read, and return
        (ok, detail). Default delegates to from_env + get_state so most drivers
        get a working test for free.
        """
        try:
            inst = cls.from_env(env)  # type: ignore[attr-defined]
            st = inst.get_state()
            ok = bool(st.get("available"))
            return ok, ("reachable" if ok else "not reachable / no response")
        except Exception as e:  # pragma: no cover - defensive
            return False, f"error: {e}"

    # ---- Optional management hooks used by `harmonia device` ----------------
    # Drivers may implement any of these; the CLI reports a clear error for the
    # ones they do not. Keeping the hook names here documents the extension
    # point, so a new TV/receiver driver never requires a CLI edit.

    #: False when the driver knows input-name writes are rejected/unsafe.
    input_name_writes_safe = True
    #: Human-readable guidance shown when input_name_writes_safe is False.
    input_name_hint = ""

    @classmethod
    def pair(cls, env, host=None, token_file=None) -> dict:
        """Optional one-time pairing; return config.env updates on success."""
        raise NotImplementedError(f"{cls.__name__} does not support pairing")

    def get_profile(self) -> dict:
        """Optional diagnostics dictionary (firmware, audio, inputs, ...)."""
        raise NotImplementedError(f"{type(self).__name__} does not expose a profile")

    def list_named_inputs(self) -> list[tuple[str, str]]:
        """Optional [(hardware_name, friendly_name)] for display inputs."""
        raise NotImplementedError(
            f"{type(self).__name__} does not expose named inputs")

    def get_input_visibility(self) -> dict[str, str]:
        """Optional map of input CNAME -> driver visibility value."""
        raise NotImplementedError(
            f"{type(self).__name__} does not expose input visibility")

    @staticmethod
    def input_cname(hardware_name: str) -> str:
        """Map a hardware input name to the driver's internal key."""
        return hardware_name

    def set_input_name(self, hardware_name: str, name: str) -> bool:
        """Optional input rename; return True on verified success."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement input-name writes")

    def set_input_visibility(self, hardware_name: str, visible: bool) -> bool:
        """Optional input show/hide; return True on verified success."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement input visibility")

    def send_key_sequence(self, spec: str) -> bool:
        """Optional remote-key sequence; return True when accepted."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement key sequences")


class AVReceiver(Device):
    """An AV receiver / amplifier. Audio authority when present."""

    role_id = "receiver"
    capabilities = (CAP_POWER, CAP_VOLUME, CAP_MUTE, CAP_INPUT)

    # Volume is expressed on a 0-100 display scale to HA. Concrete drivers map
    # to/from their native scale internally.
    @property
    def max_volume(self) -> float:
        return 100.0

    @property
    def vol_offset(self) -> float:
        return 0.0

    @abc.abstractmethod
    def power(self, on: bool) -> None: ...

    @abc.abstractmethod
    def set_input(self, src: str) -> None: ...

    @abc.abstractmethod
    def set_volume(self, vol: float) -> None: ...

    def volume_up(self) -> None:  # optional; default no-op-ish
        raise NotImplementedError

    def volume_down(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def mute(self, on: bool) -> None: ...


class Display(Device):
    """A TV / projector. Power + input authority; volume only in TV-direct."""

    role_id = "display"
    capabilities = (CAP_POWER, CAP_INPUT)

    @abc.abstractmethod
    def power(self, on: bool) -> None: ...

    @abc.abstractmethod
    def set_input(self, src: str) -> None: ...

    # Volume methods are only meaningful when the display declares CAP_VOLUME
    # (TV-direct audio). Default implementations raise so misconfig is loud.
    def set_volume(self, vol: float) -> None:
        raise NotImplementedError("display has no volume control")

    def volume_up(self) -> None:
        raise NotImplementedError

    def volume_down(self) -> None:
        raise NotImplementedError

    def mute(self, on: bool) -> None:
        raise NotImplementedError("display has no mute control")


class AudioOutput(abc.ABC):
    """PC-side audio sink management (PipeWire channel layout + upmix).

    Not a network device — it manages the local sink so it matches the declared
    SpeakerLayout / downstream capabilities. channel_sync.py uses this to decide
    the target profile; a TV-direct setup may cap at what the TV accepts.
    """

    role_id = "audio"

    @abc.abstractmethod
    def target_layout(self) -> SpeakerLayout:
        """The speaker layout we should currently drive the sink at."""

    @abc.abstractmethod
    def is_downstream_present(self) -> bool:
        """True if the audio sink (AVR/TV) is connected/awake (e.g. EDID up)."""
