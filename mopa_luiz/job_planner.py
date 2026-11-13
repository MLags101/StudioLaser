"""Preflight the exact immutable galvo instructions before any mark begins."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

from .device_profile import DeviceProfile
from .layers import HATCH_PATTERNS, LayerSpec, RASTER_ENGRAVE
from .raster import contour_hatch, hatch_polylines, radial_hatch
from .sources import frequency_advice
from .workspace import Job, Paths

GalvoPaths = tuple[tuple[tuple[int, int], ...], ...]
BATCH_POINTS = 4000
SUBDIVIDE_MM = 4.0          # longest straight run when a software field map bends lines
PATH_OVERHEAD_S = 0.0006    # laser on/off and jump settle per path, order of magnitude


@dataclass(frozen=True)
class MarkBatch:
    paths: GalvoPaths
    power: float
    speed: float
    frequency: float
    pulse_width: float
    timeout: float
    seconds: float = 0.0
    name: str = ""


@dataclass(frozen=True)
class MarkPlan:
    batches: tuple[MarkBatch, ...]
    paths: int
    points: int
    preview: Paths
    seconds: float = 0.0
    operations: tuple[tuple[str, float], ...] = ()   # (operation name, estimated seconds)
    notes: tuple[str, ...] = ()


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def partition(paths, target=BATCH_POINTS):
    """Break at pen-up boundaries. A long contour stays in one driver list."""
    batch = []
    points = 0
    for path in paths:
        if batch and points + len(path) > target:
            yield tuple(batch)
            batch = []
            points = 0
        batch.append(path)
        points += len(path)
    if batch:
        yield tuple(batch)


def layer_issues(layer: LayerSpec, profile: DeviceProfile) -> list[tuple[str, str]]:
    """(severity, message) pairs for one operation. "error" blocks marking; "warning" informs."""
    issues = []
    numbers = (layer.power_percent, layer.frequency_khz, layer.pulse_width_ns, layer.speed_mm_s,
               layer.raster_pitch_mm, layer.raster_angle_deg, layer.raster_angle_step_deg)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in numbers):
        return [("error", "Every laser setting must be a finite number")]
    if not 0 <= layer.power_percent <= 100:
        issues.append(("error", "Power must be 0–100 %"))
    elif layer.power_percent == 0:
        issues.append(("error", "Power is 0 %. Apply a preset or set the power."))
    if layer.frequency_khz <= 0:
        issues.append(("error", "Frequency must be above 0 kHz"))
    elif not 1 <= round(20000 / layer.frequency_khz) <= 65535:
        issues.append(("error", "Frequency is outside the controller's range"))
    if not isinstance(layer.passes, int) or not 1 <= layer.passes <= 999:
        issues.append(("error", "Passes must be 1–999"))
    units = layer.speed_mm_s * profile.units_per_mm / 1000
    if layer.speed_mm_s <= 0 or layer.speed_mm_s > 15000 or not 1 <= int(units) <= 65535:
        issues.append(("error", "Speed is outside the range this field supports"))
    if layer.operation == RASTER_ENGRAVE and layer.raster_pitch_mm < 0.001:
        issues.append(("error", "Hatch spacing must be at least 0.001 mm"))
    if layer.operation == RASTER_ENGRAVE and layer.pattern not in HATCH_PATTERNS:
        issues.append(("error", "Choose a supported hatch pattern"))
    if profile.limits:
        if layer.pulse_width_ns not in profile.pulse_widths:
            issues.append(("error", f"{layer.pulse_width_ns:g} ns is not a pulse width of {profile.source or 'this source'}"))
        elif layer.frequency_khz > 0:
            severity, message = frequency_advice(profile.limits, layer.pulse_width_ns, layer.frequency_khz)
            if severity:
                issues.append((severity, message))
    if profile.max_hz and layer.frequency_khz > 0 and not profile.min_hz <= layer.frequency_khz * 1000 <= profile.max_hz:
        issues.append(("error", f"Frequency must be {profile.min_hz / 1000:g}–{profile.max_hz / 1000:g} kHz"))
    return issues


def subdivide(path, limit=SUBDIVIDE_MM):
    out = [path[0]]
    for a, b in zip(path, path[1:]):
        pieces = max(1, math.ceil(math.dist(a, b) / limit))
        for i in range(1, pieces):
            t = i / pieces
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        out.append(b)
    return tuple(out)


def toolpaths(artwork, job: Job, notes: list[str] | None = None) -> Paths:
    """World-space toolpaths for one operation, in marking order."""
    layer = artwork.layer
    source = artwork.fills if layer.operation == RASTER_ENGRAVE else artwork.paths
    world = job.placement.paths(source)
    if any(len(path) < 2 for path in world):
        raise ValueError(f"{artwork.name}: incomplete path")
    if layer.operation != RASTER_ENGRAVE:
        return world * layer.passes
    if layer.pattern == "contour":
        paths, fallback = contour_hatch(world, layer.raster_pitch_mm, layer.raster_angle_deg)
        if fallback and notes is not None:
            notes.append(f"{artwork.name}: narrow regions use clipped parallel hatch.")
        return tuple(tuple(path) for path in paths) * layer.passes
    if layer.pattern == "radial":
        if not job.coupon:
            raise ValueError(f"{artwork.name}: radial spokes are for annular test coupons only")
        return tuple(radial_hatch(world, layer.raster_pitch_mm)) * layer.passes
    result = hatch_polylines(
        world, angle_deg=layer.raster_angle_deg, pitch_mm=layer.raster_pitch_mm,
        passes=layer.passes, angle_step_deg=layer.raster_angle_step_deg,
        closure_tol_mm=1e-6, crosshatch=layer.pattern == "crosshatch", by_island=True,
    )
    return tuple(tuple(path) for path in result.segments)


def compile_mark(job: Job, profile: DeviceProfile, *, live: bool = False) -> MarkPlan:
    profile.validate(live=live)
    batches = []
    preview = []
    operations = []
    notes = []
    position = None
    for artwork in job.objects:
        layer = artwork.layer
        if not layer.emits_when_marking():
            continue
        if job.coupon and layer.frequency_khz != 37:
            raise ValueError(f"{artwork.name}: tuning coupons require 37 kHz")
        errors = [message for severity, message in layer_issues(layer, profile) if severity == "error"]
        if errors:
            raise ValueError(f"{artwork.name}: {errors[0]}")
        paths = toolpaths(artwork, job, notes)
        if not paths:
            raise ValueError(f"{artwork.name}: no toolpaths. Check the operation, artwork and hatch spacing.")
        for path in paths:
            for point in path:
                try:
                    profile.command_point(point)
                except ValueError as exc:
                    raise ValueError(f"{artwork.name}: {exc}") from None
        total = 0.0
        for batch in partition(paths):
            sent = tuple(subdivide(path) for path in batch) if profile.curved else batch
            try:
                mapped = tuple(tuple(map(profile.galvo_point, path)) for path in sent)
            except ValueError as exc:
                raise ValueError(f"{artwork.name}: {exc}") from None
            distance = sum(math.dist(a, b) for path in batch for a, b in zip(path, path[1:]))
            jumps = 0.0
            for path in batch:
                if position is not None:
                    jumps += math.dist(position, path[0])
                position = path[-1]
            seconds = distance / layer.speed_mm_s + jumps / profile.jump_speed + len(batch) * PATH_OVERHEAD_S
            timeout = max(20, 10 + 3 * (seconds + len(batch) * 0.08))
            batches.append(MarkBatch(mapped, layer.power_percent, layer.speed_mm_s, layer.frequency_khz,
                                     layer.pulse_width_ns, timeout, seconds, artwork.name))
            total += seconds
        operations.append((artwork.name, total))
        preview.extend(paths)
    if not batches:
        raise ValueError("This job has no enabled operations. Tick at least one operation to run.")
    return MarkPlan(tuple(batches), len(preview), sum(map(len, preview)), tuple(preview),
                    sum(seconds for _, seconds in operations), tuple(operations), tuple(notes))


def compile_frame(paths: Paths, profile: DeviceProfile) -> GalvoPaths:
    if not paths:
        raise ValueError("There is no outline or target to frame")
    if any(len(path) < 2 for path in paths):
        raise ValueError("Framing contains an incomplete path")
    if profile.curved:
        paths = tuple(subdivide(path) for path in paths)
    return tuple(tuple(profile.galvo_point(point, red_light=True) for point in path) for path in paths)


class Arming:
    def __init__(self, seconds=30, clock=time.monotonic):
        self.seconds = seconds
        self.clock = clock
        self.clear()

    def clear(self):
        self._plan = None
        self._expires = 0

    @property
    def active(self):
        return self._plan is not None and self.clock() < self._expires

    @property
    def remaining(self) -> float:
        return max(0.0, self._expires - self.clock()) if self._plan is not None else 0.0

    def set(self, key, plan):
        self._plan = (key, plan)
        self._expires = self.clock() + self.seconds

    def take(self, key):
        plan = self._plan[1] if self.active and self._plan[0] == key else None
        self.clear()
        if plan is None:
            raise PermissionError("Arm this validated job again before starting")
        return plan
