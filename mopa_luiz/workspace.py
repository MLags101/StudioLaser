"""Desktop jobs, board registration and the versioned project format."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import cached_property
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

    def invert(self, point: Point) -> Point:
        """Machine millimetres back to design millimetres (the inverse of ``apply``)."""
        x, y = point[0] - self.x, point[1] - self.y
        angle = math.radians(self.angle)
        c, s = math.cos(angle), math.sin(angle)
        x, y = (x * c + y * s) / self.scale, (-x * s + y * c) / self.scale
        return (-x if self.mirror else x, y)

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

    @cached_property
    def hull(self) -> tuple[Point, ...]:
        """Convex hull of every point in the job, in design coordinates, computed once per job.

        Moving, turning or scaling the job moves its hull with it, so the placed
        bounds come from a few dozen hull points instead of every point of the board.
        """
        groups = [self.boundary, self.retained]
        for artwork in self.objects:
            groups += [artwork.paths, artwork.fills]
        return convex_hull([point for group in groups for path in group for point in path])

    def bounds(self, *, placed: bool = True):
        """(x0, y0, x1, y1) of everything in the job, or None when empty."""
        points = self.hull
        if not points:
            return None
        if placed:
            points = [self.placement.apply(point) for point in points]
        xs, ys = zip(*points)
        return min(xs), min(ys), max(xs), max(ys)


def convex_hull(points) -> tuple[Point, ...]:
    """Andrew's monotone chain; collinear points dropped."""
    unique = sorted(set(points))
    if len(unique) <= 2:
        return tuple(unique)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


def nearest_on_paths(paths: Paths, point: Point) -> tuple[Point, float, Point]:
    """Closest point on any segment of ``paths``, its distance, and that segment's unit direction."""
    px, py = point
    best = None
    for path in paths:
        for (ax, ay), (bx, by) in zip(path, path[1:]):
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            if length2 == 0:
                continue
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
            qx, qy = ax + t * dx, ay + t * dy
            distance = math.hypot(px - qx, py - qy)
            if best is None or distance < best[1]:
                length = math.sqrt(length2)
                best = ((qx, qy), distance, (dx / length, dy / length))
    if best is None:
        raise ValueError("The board has no outline to register on")
    return best


def edge_run(paths: Paths, point: Point, *, max_turn: float = 35.0) -> tuple[Point, ...]:
    """The straight or gently curved stretch of outline containing ``point``, ending at corners.

    Walks both ways from the segment nearest ``point`` until the direction has
    turned by more than ``max_turn`` degrees, so a point picked on one edge is
    never matched to the neighbouring edge across a corner.
    """
    best = None
    for path_index, path in enumerate(paths):
        for index, (a, b) in enumerate(zip(path, path[1:])):
            distance = nearest_on_paths(((a, b),), point)[1] if a != b else math.inf
            if best is None or distance < best[0]:
                best = (distance, path_index, index)
    if best is None:
        raise ValueError("The board has no outline to register on")
    _, path_index, index = best
    path = paths[path_index]
    closed = len(path) > 3 and math.dist(path[0], path[-1]) < 1e-6
    segments = [(a, b) for a, b in zip(path, path[1:]) if a != b]
    index = min(index, len(segments) - 1)

    def heading(segment):
        (ax, ay), (bx, by) = segment
        return math.degrees(math.atan2(by - ay, bx - ax))

    def turned(start, current):
        return abs((heading(current) - heading(start) + 180) % 360 - 180)

    count = len(segments)
    picked = segments[index]
    forward, backward = [picked], []
    for step in range(1, count):
        position = index + step
        if position >= count and not closed:
            break
        segment = segments[position % count]
        if turned(picked, segment) > max_turn:
            break
        forward.append(segment)
    for step in range(1, count - len(forward) + 1):
        position = index - step
        if position < 0 and not closed:
            break
        segment = segments[position % count]
        if turned(picked, segment) > max_turn:
            break
        backward.append(segment)
    run = list(reversed(backward)) + forward
    return tuple([run[0][0]] + [segment[1] for segment in run])


@dataclass(frozen=True)
class EdgeRegistration:
    placement: Placement
    residuals: tuple[float, ...]   # mm, each measured point's distance from the fitted outline
    well_posed: bool               # False when the points cannot fix both position and rotation
    spacing_error: float = 0.0

    @property
    def worst(self) -> float:
        return max(self.residuals)


def fit_to_outline(outline: Paths, measured: list[Point], start: Placement, *, iterations: int = 80,
                   picked: list[Point] | None = None) -> EdgeRegistration:
    """Fit the board so each measured point lies on its outline (edge cuts).

    Each point only says "the edge passes here", not which spot on the edge it
    is, so the fit minimises distance to the outline (iterative closest point),
    starting from the current placement. Frame the outline first so that start
    is close. Three or more points spread over at least two edges are needed.
    """
    import numpy as np

    if len(measured) < 3:
        raise ValueError("Pick at least three points on the board edge, on at least two different edges")
    if not all(math.isfinite(v) for point in measured for v in point):
        raise ValueError("Registration coordinates must be finite")
    # With the design points the operator clicked, each measurement is matched only to its own edge.
    # Without them a point near a corner can be explained by either edge, giving a wrong exact fit.
    edges = [(edge_run(outline, point),) for point in picked] if picked else [outline] * len(measured)
    if len(edges) != len(measured):
        raise ValueError("Each measured point needs the edge it was picked on")
    placement = start
    for _ in range(iterations):
        # Gauss-Newton on point-to-edge distance: each point may slide freely along its edge,
        # which is what "the edge passes here" means, so it converges in a few steps.
        rows, residuals = [], []
        for edge, point in zip(edges, measured):
            closest, _, direction = nearest_on_paths(edge, placement.invert(point))
            wx, wy = placement.apply(closest)
            ex, ey = placement.apply((closest[0] + direction[0], closest[1] + direction[1]))
            length = math.hypot(ex - wx, ey - wy) or 1.0
            nx, ny = -(ey - wy) / length, (ex - wx) / length          # edge normal in machine space
            rx, ry = wx - placement.x, wy - placement.y
            rows.append([nx, ny, nx * -ry + ny * rx])
            residuals.append(nx * (point[0] - wx) + ny * (point[1] - wy))
        step, *_ = np.linalg.lstsq(np.asarray(rows), np.asarray(residuals), rcond=None)
        dx, dy, dtheta = (float(v) for v in step)
        # Turning about the placement origin also moves points; compose the small rotation exactly.
        placement = replace(placement, x=placement.x + dx, y=placement.y + dy,
                            angle=(placement.angle + math.degrees(dtheta) + 180) % 360 - 180)
        if math.hypot(dx, dy) < 1e-9 and abs(dtheta) < 1e-11:
            break
    hits = [nearest_on_paths(edge, placement.invert(point)) for edge, point in zip(edges, measured)]
    residuals = tuple(hit[1] * placement.scale for hit in hits)
    # Well posed when distances to the edges constrain x, y and rotation: the 3-column
    # Jacobian of point-to-edge distance must have full rank with a reasonable condition.
    centre = np.mean(np.asarray(measured, dtype=float), axis=0)
    radius = max(1e-6, float(np.sqrt(np.mean(np.sum((np.asarray(measured) - centre) ** 2, axis=1)))))
    rows = []
    for point, (_, _, (dx, dy)) in zip(measured, hits):
        angle = math.radians(placement.angle)
        c, s = math.cos(angle), math.sin(angle)
        if placement.mirror:
            dx = -dx
        nx, ny = -(dx * s + dy * c), dx * c - dy * s        # edge normal turned into machine space
        rx, ry = point[0] - centre[0], point[1] - centre[1]
        rows.append([nx, ny, (nx * -ry + ny * rx) / radius])
    singular = np.linalg.svd(np.asarray(rows), compute_uv=False)
    well_posed = len(singular) == 3 and singular[-1] > 0.15 * singular[0]
    return EdgeRegistration(placement, residuals, well_posed)


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
