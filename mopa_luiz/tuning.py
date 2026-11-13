"""One-coupon PCB preset trials and deterministic feedback suggestions."""

from dataclasses import dataclass, replace
import math
from uuid import uuid4

from .layers import RASTER_ENGRAVE
from .workspace import COPPER_FILL, MASK, SILK, Artwork, Job, rectangle

FREQUENCY_KHZ = 37.0
KINDS = {COPPER_FILL: "Trace isolation", SILK: "Silkscreen marking", MASK: "Solder mask removal"}
FIELDS = {
    "passes": "Passes",
    "speed_mm_s": "Speed (mm/s)",
    "power_percent": "Power (%)",
    "raster_pitch_mm": "Hatch spacing (mm)",
}
RANGES = {
    "passes": (1, 50),
    "speed_mm_s": (1, 15000),
    "power_percent": (0.1, 100),
    "raster_pitch_mm": (0.001, 5),
}


@dataclass(frozen=True)
class Feedback:
    result: str
    damage: int
    isolation: str = "unknown"
    depth: int | None = None
    pitting: bool = False


@dataclass(frozen=True)
class Advice:
    reason: str
    field: str = ""
    value: float | int = 0
    accepted: bool = False


def trial_settings(layer):
    return tuple(getattr(layer, field) for field in FIELDS)


def suggest(kind, layer, feedback, bounds, tried=()):
    """Give one bounded change or explain why the operator should review the coupon."""
    if feedback.damage >= 4 or feedback.pitting:
        return Advice("Review the stock, focus and test setup before making another pass.")
    if kind == COPPER_FILL:
        if (feedback.result == "clear" and feedback.isolation == "short"
                or feedback.result == "clear" and feedback.depth not in (None, 100)
                or feedback.result in {"striped", "uniform"} and feedback.depth == 100):
            return Advice("The observations conflict. Inspect the channel and check isolation again.")
        if feedback.result == "clear" and feedback.isolation == "unknown" and feedback.damage == 1:
            return Advice("Check isolation with a meter before accepting the preset.")
    if feedback.result == "unknown":
        return Advice("Inspect the coupon again before changing settings.")
    complete = feedback.result == "clear" and (kind != COPPER_FILL or feedback.isolation == "isolated")
    if complete and feedback.damage == 1:
        return Advice("The coupon meets the acceptance checks. Save this preset or confirm it on fresh scrap.",
                      accepted=True)
    if len(tried) >= 6:
        return Advice("Six trials have been reviewed. Check the stock, focus and limits before continuing.")
    if feedback.damage > 1:
        reason = "Visible damage calls for less exposure."
        changes = (("passes", -1), ("speed_mm_s", 1.1), ("power_percent", 0.9))
    elif feedback.result == "striped":
        reason = "Regular bands suggest a gap between neighboring hatch lines."
        changes = (("raster_pitch_mm", 0.9), ("passes", 1), ("speed_mm_s", 0.9), ("power_percent", 1.1))
    else:
        reason = "The result is uniformly incomplete. Try a small increase in exposure."
        changes = (("passes", 1), ("speed_mm_s", 0.9), ("power_percent", 1.1))
    for field, step in changes:
        old = getattr(layer, field)
        lower, upper = bounds[field]
        raw = old + step if field == "passes" else old * step
        decimals = {"speed_mm_s": 0, "power_percent": 1, "raster_pitch_mm": 3}
        value = int(raw) if field == "passes" else round(raw, decimals[field])
        value = min(upper, max(lower, value))
        if (value == old or (value - old) * (raw - old) <= 0
                or trial_settings(replace(layer, **{field: value})) in tried):
            continue
        return Advice(reason, field, value)
    return Advice("The tested settings and your adjustment bounds leave no new suggestion.")


def coupon_job(kind, layer, *, channel_width=0.25):
    from shapely import BufferCapStyle, union_all
    from shapely.geometry import LineString, Point

    from .patterns import text_paths
    from .pcb.board import contours

    if not math.isfinite(channel_width) or not 0.05 <= channel_width <= 1.0:
        raise ValueError("Coupon channel width must be 0.05–1 mm")
    if layer.pattern == "radial":
        outer = Point(0, 0).buffer(3 + channel_width, quad_segs=64)
        inner = Point(0, 0).buffer(3, quad_segs=64)
        paths = contours(outer.difference(inner))
    elif kind == COPPER_FILL:
        paths = (rectangle(-3 - channel_width, -2 - channel_width, 3 + channel_width, 2 + channel_width),
                 rectangle(-3, -2, 3, 2))
    elif kind == SILK:
        letters = text_paths("PCB", 2, 0, -0.5, anchor="center")
        lines = (*letters, ((-2.7, -1.6), (2.7, -1.6)))
        paths = contours(union_all([LineString(line).buffer(0.10, cap_style=BufferCapStyle.flat) for line in lines]))
    elif kind == MASK:
        paths = (rectangle(-1.5, -1, 1.5, 1),)
    else:
        raise ValueError("Choose a PCB operation to tune")
    layer = replace(layer, frequency_khz=FREQUENCY_KHZ, operation=RASTER_ENGRAVE, output=True)
    return Job(f"{KINDS[kind]} coupon", (Artwork(KINDS[kind], paths, paths, layer, kind),),
               job_id=uuid4().hex, coupon=True)
