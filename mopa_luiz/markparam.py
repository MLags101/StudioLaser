"""EZCAD2's pen library (MarkParam.lib): controller timing and the operator's saved parameter sets."""

from __future__ import annotations

import configparser
from dataclasses import dataclass, replace
import math
from pathlib import Path

from .layers import LayerSpec

# [Default] keys -> DeviceProfile.delays keys; all values are microseconds.
TIMING = {
    "STARTTC": "delay_laser_on",
    "LASEROFFTC": "delay_laser_off",
    "POLYTC": "delay_polygon",
    "m_nMinJumpDelayTCUs": "delay_jump_short",
    "m_nMaxJumpDelayTCUs": "delay_jump_long",
}
SKIP = {"LASERMODE"}


@dataclass(frozen=True)
class PenSet:
    """One named parameter set as EZCAD stored it."""

    name: str
    passes: int
    speed_mm_s: float
    power_percent: float
    frequency_khz: float
    pulse_width_ns: float

    def layer(self, base: LayerSpec) -> LayerSpec:
        """These laser numbers on top of ``base``, which supplies the operation and hatch settings
        (EZCAD keeps hatch spacing on each drawing object, not in the pen library)."""
        return replace(base, power_percent=self.power_percent, speed_mm_s=self.speed_mm_s,
                       frequency_khz=self.frequency_khz, pulse_width_ns=self.pulse_width_ns, passes=self.passes)


@dataclass(frozen=True)
class MarkParamLibrary:
    delays: dict[str, float]
    jump_speed: float | None
    sets: tuple[PenSet, ...]


def read_markparam(path: Path) -> MarkParamLibrary:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        parser.read_string(path.read_bytes().decode("gbk", errors="replace"))
    except configparser.Error as exc:
        raise ValueError(f"{path.name} is not an EZCAD parameter library: {exc}") from exc
    if "Default" not in parser:
        raise ValueError(f"{path.name} has no [Default] parameter set; it is not an EZCAD MarkParam.lib")

    def number(section, key):
        try:
            value = float(section[key])
        except (KeyError, ValueError):
            return None
        return value if math.isfinite(value) else None

    default = parser["Default"]
    delays = {}
    for key, dest in TIMING.items():
        value = number(default, key)
        if value is not None and abs(value) <= 32767:
            delays[dest] = value
    jump = number(default, "JUMPSPEED")
    sets = []
    for name in parser.sections():
        if name in SKIP or name == "Default":
            continue
        section = parser[name]
        values = [number(section, key) for key in ("LOOP", "MARKSPEED", "POWERRATIO", "FREQ", "QPULSEWIDTH")]
        if any(value is None for value in values):
            continue
        loops, speed, power, freq_hz, pulse = values
        if not (1 <= loops <= 999 and 0 < speed <= 15000 and 0 <= power <= 100 and freq_hz > 0 and pulse > 0):
            continue
        sets.append(PenSet(name, int(loops), speed, power, freq_hz / 1000, pulse))
    return MarkParamLibrary(delays, jump if jump and 0 < jump <= 15000 else None, tuple(sets))


def guess_kind(name: str) -> str:
    """Map an operator's set name to a PCB operation when the name says what it is."""
    key = name.casefold()
    if "smr" in key or "mask" in key:
        return "mask"
    if "clean" in key:
        return "cleanup"
    if "silk" in key or "legend" in key:
        return "silk"
    if key == "pcben":
        return "copper_fill"
    return ""
