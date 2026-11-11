"""Laser source limits: valid MOPA pulse widths and their frequency windows.

A JPT MOPA source accepts only a fixed set of pulse widths. Each has a
cut-off frequency, below which the source limits peak power so average
power falls, and a maximum frequency.

The 60 W table is from the JPT YDFLP-60/80/100/120-M7-M-R user manual
(tables 4 and 5). Other wattages have different cut-offs; edit the table in
the machine profile if the source's own manual differs.
"""

from __future__ import annotations

# (pulse width ns, cut-off kHz, maximum kHz)
Limits = tuple[tuple[int, float, float], ...]

JPT_M7_60W: Limits = (
    (2, 1950, 4000), (4, 1350, 4000), (6, 975, 4000), (9, 600, 4000),
    (13, 412, 3000), (20, 225, 3000), (30, 187, 3000),
    (45, 150, 2000), (60, 135, 2000), (80, 112, 2000),
    (100, 105, 1000), (150, 57, 1000), (200, 45, 1000),
    (250, 42, 900), (350, 40, 600), (500, 30, 500),
)

SOURCES: dict[str, Limits] = {
    "JPT M7 60 W (YDFLP-60-M7)": JPT_M7_60W,
}
DEFAULT_SOURCE = "JPT M7 60 W (YDFLP-60-M7)"


def limits_for(limits: Limits, pulse_width_ns: float) -> tuple[float, float] | None:
    for width, cutoff, maximum in limits:
        if width == pulse_width_ns:
            return cutoff, maximum
    return None


def frequency_advice(limits: Limits, pulse_width_ns: float, frequency_khz: float) -> tuple[str, str]:
    """Return (severity, message); severity is "", "warning" or "error"."""
    window = limits_for(limits, pulse_width_ns)
    if window is None:
        return ("error", f"{pulse_width_ns:g} ns is not a pulse width this source accepts") if limits else ("", "")
    cutoff, maximum = window
    if frequency_khz > maximum:
        return "error", f"{pulse_width_ns:g} ns pulses run at {maximum:g} kHz or less on this source"
    if frequency_khz < cutoff:
        share = max(1, round(100 * frequency_khz / cutoff))
        return "warning", (
            f"Below the {cutoff:g} kHz cut-off for {pulse_width_ns:g} ns: full pulse energy, "
            f"but average power is about {share}% of the set value"
        )
    return "", ""


def parse_limits(raw) -> Limits:
    rows = []
    for row in raw:
        width, cutoff, maximum = row
        width, cutoff, maximum = int(width), float(cutoff), float(maximum)
        if not 0 < width <= 65535 or not 0 < cutoff <= maximum <= 4000:
            raise ValueError("Source limits need a pulse width and 0 < cut-off ≤ maximum ≤ 4000 kHz")
        rows.append((width, cutoff, maximum))
    if len({row[0] for row in rows}) != len(rows):
        raise ValueError("Source limits list a pulse width twice")
    return tuple(sorted(rows))
