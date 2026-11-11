"""Laser-parameter sanity policy for layers.

Every layer that can emit is checked by `validate_layer_settings` (through
`layers.validate_layers`) when the desktop job planner compiles a mark:
  - Power must lie within the laser's electrical 0..100% range.
  - Speed must be positive.
  - Frequency must lie within the machine profile's MINPWMFREQ / MAXPWMFREQ bounds.
  - Pulse width must land on the machine profile's PULSEWIDTHS table,
    defaulting to the JPT M7 table (within rounding).

Arming, field limits and controller ranges are enforced by `job_planner`.
There is no artificial cap on commanded power; the operator selects the
working power via per-layer settings.
"""

from __future__ import annotations

import math

from .cli import MarkConfig, nearest_pulse_width, pulse_width_table


def validate_layer_settings(
    *,
    operation: str,
    power_percent: float,
    frequency_khz: float,
    pulse_width_ns: float,
    speed_mm_s: float,
    cfg: MarkConfig | None,
) -> list[str]:
    """Check a layer's parameters for sanity.

    Returns a list of error strings; empty list means OK. `disabled` and
    `frame_only` layers skip validation since they cannot emit.
    """
    errors: list[str] = []
    if operation in {"disabled", "frame_only"}:
        return errors
    if not math.isfinite(power_percent) or not 0 <= power_percent <= 100:
        errors.append(f"layer power {power_percent} out of 0..100")
    if not math.isfinite(speed_mm_s) or speed_mm_s <= 0:
        errors.append("layer speed must be > 0 mm/s")
    if not math.isfinite(frequency_khz):
        errors.append("layer frequency must be finite")
    elif cfg is not None:
        min_freq_hz = cfg.get_int("MINPWMFREQ", 1000) or 1000
        max_freq_hz = cfg.get_int("MAXPWMFREQ", 4000000) or 4000000
        requested_freq_hz = frequency_khz * 1000.0
        if not min_freq_hz <= requested_freq_hz <= max_freq_hz:
            errors.append(
                f"layer frequency {requested_freq_hz:.0f} Hz outside machine profile "
                f"{min_freq_hz}..{max_freq_hz} Hz"
            )
    table = pulse_width_table(cfg)
    if not math.isfinite(pulse_width_ns):
        errors.append("pulse width must be finite")
    elif pulse_width_ns not in table:
        snapped = nearest_pulse_width(pulse_width_ns, table)
        if abs(snapped - pulse_width_ns) > 0.5:
            errors.append(
                f"layer pulse width {pulse_width_ns} ns not in machine pulse-width table"
            )
    return errors
