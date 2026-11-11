"""Desktop jobs, board registration and the versioned project format."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
from pathlib import Path
from uuid import uuid4

from .device_profile import write_json
from .layers import LayerSpec, RASTER_ENGRAVE, VECTOR_ENGRAVE, layer_from_dict

Point = tuple[float, float]
Paths = tuple[tuple[Point, ...], ...]

# PCB operation kinds, in the order they run on one side of a board.
COPPER_CONTOUR = "copper_contour"
COPPER_FILL = "copper_fill"
CLEANUP = "cleanup"
MASK = "mask"
SILK = "silk"
KINDS = (COPPER_CONTOUR, COPPER_FILL, CLEANUP, MASK, SILK)
KIND_LABELS = {
    COPPER_CONTOUR: "Copper contour",
    COPPER_FILL: "Copper fill",
    CLEANUP: "Cleanup pass",
    MASK: "Solder mask removal",
    SILK: "Silkscreen",
    "": "Engrave",
}


@dataclass(frozen=True)
class Placement:
    x: float = 0
    y: float = 0
    angle: float = 0
    scale: float = 1
    mirror: bool = False

    def apply(self, point: Point) -> Point:
        x, y = point
        if self.mirror:
            x = -x
        x, y = x * self.scale, y * self.scale
        angle = math.radians(self.angle)
        c, s = math.cos(angle), math.sin(angle)
        return (self.x + x * c - y * s, self.y + x * s + y * c)

    def paths(self, paths: Paths) -> Paths:
        return tuple(tuple(map(self.apply, path)) for path in paths)

    def validate(self) -> None:
        if not all(math.isfinite(v) for v in (self.x, self.y, self.angle, self.scale)):
            raise ValueError("Placement must be finite")
        if not 0.01 <= self.scale <= 100:
            raise ValueError("Scale must be between 0.01 and 100")


@dataclass(frozen=True)
class Registration:
    placement: Placement
    residuals: tuple[float, ...]   # mm per reference, after the fit
    spacing_error: float           # mm, worst pairwise distance mismatch (independent of the fit)

    @property
    def worst(self) -> float:
        return max(self.residuals)


def fit_placement(source: list[Point], target: list[Point], *,
                  allow_scale: bool = False, mirror: bool = False) -> Registration:
    """Least-squares rigid fit of design points onto measured machine points."""
    if len(source) != len(target) or len(source) < 2:
        raise ValueError("Registration needs at least two matched points")
    if not all(math.isfinite(v) for point in (*source, *target) for v in point):
        raise ValueError("Registration coordinates must be finite")
    src = [(-x, y) for x, y in source] if mirror else list(source)
    n = len(src)
    sx, sy = sum(p[0] for p in src) / n, sum(p[1] for p in src) / n
    tx, ty = sum(p[0] for p in target) / n, sum(p[1] for p in target) / n
    spread = sum((x - sx) ** 2 + (y - sy) ** 2 for x, y in src)
    if spread < 1e-6 or sum((x - tx) ** 2 + (y - ty) ** 2 for x, y in target) < 1e-6:
        raise ValueError("Choose distinct, well-separated registration points")
    dot = sum((a - sx) * (p - tx) + (b - sy) * (q - ty) for (a, b), (p, q) in zip(src, target))
    cross = sum((a - sx) * (q - ty) - (b - sy) * (p - tx) for (a, b), (p, q) in zip(src, target))
    angle = math.atan2(cross, dot)
    scale = math.hypot(dot, cross) / spread if allow_scale else 1.0
    c, s = math.cos(angle) * scale, math.sin(angle) * scale
    placement = Placement(
        tx - (sx * c - sy * s), ty - (sx * s + sy * c),
        (math.degrees(angle) + 180) % 360 - 180, scale, mirror,
    )
    placement.validate()
    residuals = tuple(math.dist(placement.apply(a), b) for a, b in zip(source, target))
    spacing = max(
        abs(math.dist(source[i], source[j]) - math.dist(target[i], target[j]))
        for i in range(n) for j in range(i + 1, n)
    )
    return Registration(placement, residuals, spacing)


def register(source: tuple[Point, Point], target: tuple[Point, Point]) -> tuple[Placement, float]:
    """Two-point fit kept for callers that only need the placement and spacing check."""
    result = fit_placement(list(source), list(target))
    return result.placement, result.spacing_error


def new_layer(name="Engrave", operation=VECTOR_ENGRAVE) -> LayerSpec:
    # An unapplied preset has zero power and requires an explicit live recipe.
    return LayerSpec(name, name, operation, 0, 30, 200, 1000)


@dataclass(frozen=True)
class Artwork:
    name: str
    paths: Paths
    fills: Paths
    layer: LayerSpec
    kind: str = ""


@dataclass(frozen=True)
class Job:
    name: str
    objects: tuple[Artwork, ...]
    placement: Placement = Placement()
    boundary: Paths = ()
    retained: Paths = ()
    references: tuple[Point, ...] = ()
    board_id: str = ""
    side: str = ""
    job_id: str = ""
    status: str = "ready"
    holes: bool = True    # references are drilled holes; False when they are board corners

    def frame(self) -> Paths:
        paths = self.boundary
        if not paths:
            box = self.bounds(placed=False)
            if box is None:
                return ()
            paths = (rectangle(*box),)
        return self.placement.paths(paths)

    def bounds(self, *, placed: bool = True):
        """(x0, y0, x1, y1) of everything in the job, or None when empty."""
        groups = [self.boundary, self.retained]
        for artwork in self.objects:
            groups += [artwork.paths, artwork.fills]
        if placed:
            groups = [self.placement.paths(group) for group in groups]
        points = [point for group in groups for path in group for point in path]
        if not points:
            return None
        xs, ys = zip(*points)
        return min(xs), min(ys), max(xs), max(ys)


def rectangle(x0, y0, x1, y1):
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))


def job_from_board(name, side, board_id, layers=None):
    """One job per board side, with one object per PCB operation in run order.

    ``layers`` maps an operation kind to the LayerSpec to start from.
    """
    from .pcb.board import contours
    from .presets import starting_layer

    layers = layers or {}
    origin = Placement(-side.origin[0], -side.origin[1])

    def layer(kind):
        return replace(layers[kind]) if kind in layers else starting_layer(kind)

    removal = origin.paths(contours(side.removal))
    objects = []
    if removal:
        objects += [
            Artwork(KIND_LABELS[COPPER_CONTOUR], removal, removal, layer(COPPER_CONTOUR), COPPER_CONTOUR),
            Artwork(KIND_LABELS[COPPER_FILL], removal, removal, layer(COPPER_FILL), COPPER_FILL),
            Artwork(KIND_LABELS[CLEANUP], removal, removal, layer(CLEANUP), CLEANUP),
        ]
    for kind, geometry in ((MASK, getattr(side, "mask_openings", None)), (SILK, getattr(side, "silkscreen", None))):
        shapes = origin.paths(contours(geometry)) if geometry is not None else ()
        if shapes:
            objects.append(Artwork(KIND_LABELS[kind], shapes, shapes, layer(kind), kind))
    if not objects:
        raise ValueError(f"The {side.side} side has nothing to engrave")
    references, holes = tuple(side.references), True
    if len(references) < 2:
        # No drill file: fall back to the corners of a rectangular board.
        x0, y0, x1, y1 = side.outline.bounds
        if side.outline.area > 0.98 * (x1 - x0) * (y1 - y0):
            references, holes = ((x0, y0), (x1, y0), (x1, y1), (x0, y1)), False
    return Job(
        f"{name} — {side.side.title()}", tuple(objects),
        boundary=origin.paths(contours(side.outline)),
        retained=origin.paths(contours(side.retained)),
        references=tuple(map(origin.apply, references)),
        board_id=board_id, side=side.side, job_id=uuid4().hex, holes=holes,
    )


def flipped_placement(top: Placement) -> Placement:
    """Where the other side lands if the board is flipped in place about its
    own centre. Either flip axis reverses the rotation. A starting point only:
    always register the second side on its holes."""
    return Placement(top.x, top.y, -top.angle)


def read_paths(raw) -> Paths:
    paths = []
    for path in raw:
        points = tuple((float(x), float(y)) for x, y in path)
        if len(points) < 2 or not all(math.isfinite(value) for point in points for value in point):
            raise ValueError("Project contains an invalid path")
        paths.append(points)
    return tuple(paths)


def save_project(path: Path, jobs: list[Job]) -> None:
    write_json(path, {"version": 2, "jobs": [asdict(job) for job in jobs]})


def read_jobs(data) -> list[Job]:
    if data.get("version") not in (1, 2):
        raise ValueError("Unsupported project version")
    jobs = []
    for raw in data["jobs"]:
        if not raw.get("objects"):
            raise ValueError(f"Project job {raw.get('name', '?')!r} has no operations")
        objects = tuple(
            Artwork(obj["name"], read_paths(obj["paths"]), read_paths(obj["fills"]),
                    layer_from_dict(obj["layer"]), str(obj.get("kind", "")))
            for obj in raw["objects"]
        )
        place = raw["placement"]
        placement = Placement(float(place["x"]), float(place["y"]), float(place["angle"]),
                              float(place.get("scale", 1)), bool(place.get("mirror", False)))
        placement.validate()
        references = tuple((float(x), float(y)) for x, y in raw.get("references", []))
        if not all(math.isfinite(value) for point in references for value in point):
            raise ValueError("Project contains an invalid reference point")
        status = raw.get("status", "ready")
        if status not in {"ready", "running", "interrupted", "complete"}:
            raise ValueError("Project contains an invalid job status")
        jobs.append(Job(raw["name"], objects, placement, read_paths(raw.get("boundary", [])),
                        read_paths(raw.get("retained", [])), references, raw.get("board_id", ""),
                        raw.get("side", ""), raw["job_id"], "interrupted" if status == "running" else status,
                        bool(raw.get("holes", True))))
    if len({job.job_id for job in jobs}) != len(jobs):
        raise ValueError("Project contains duplicate job IDs")
    return jobs


def open_project(path: Path) -> list[Job]:
    return read_jobs(json.loads(path.read_text(encoding="utf-8")))
