"""Prepare both sides of a CNC-cut board in the original fabrication coordinates."""

from dataclasses import dataclass
import math
from pathlib import Path
import re
from uuid import uuid4

import gerbonara
from gerbonara import graphic_primitives as gp
from gerbonara.cam import FileSettings
from shapely import affinity, union_all
from shapely.geometry import GeometryCollection, Point as ShapelyPoint, Polygon, box
from shapely.geometry.base import BaseGeometry
from .gerber_parser import layer_type_for_file, parse_gerber_file, primitive_to_shapely, read_outline
from .inverter import BoundaryMode, InversionMode, invert_layer

Point = tuple[float, float]
Paths = tuple[tuple[Point, ...], ...]
Bounds = tuple[float, float, float, float]
ROLES = ("top_copper", "bottom_copper", "top_mask", "bottom_mask", "top_silk", "bottom_silk",
         "outline", "drill", "other")
# Number formats tried when an Excellon file does not state its own:
# (unit, (integer, decimal) digits, which zeros are suppressed).
DRILL_FORMATS = tuple(
    (unit, digits, zeros)
    for unit, digits in ((gerbonara.Inch, (2, 4)), (gerbonara.MM, (3, 3)), (gerbonara.MM, (3, 2)))
    for zeros in ("leading", "trailing")
)


def detect_role(path: Path) -> str:
    if path.suffix.lower() in {".drl", ".xln", ".exc"}:
        return "drill"
    gerber = gerbonara.GerberFile.open(path)
    return layer_type_for_file(gerber, path)[0]


def polygons(geometry):
    if isinstance(geometry, Polygon) and not geometry.is_empty:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from polygons(part)


def contours(geometry: BaseGeometry) -> Paths:
    return tuple(tuple(ring.coords) for part in polygons(geometry)
                 for ring in (part.exterior, *part.interiors))


def polygon_region(rings: Paths) -> BaseGeometry:
    """Build an even-odd region from the exterior and interior rings of a fill."""
    result = GeometryCollection()
    for ring in rings:
        polygon = Polygon(ring)
        if not polygon.is_valid:
            raise ValueError("Fill contains an invalid closed contour")
        result = result.symmetric_difference(polygon)
    return result


def drill_shapes(drill, path: Path) -> tuple[list[BaseGeometry], list[Point]]:
    cutouts = []
    centers = []
    for obj in drill.objects:
        for primitive in obj.to_primitives(gerbonara.MM):
            geometry = primitive_to_shapely(primitive)
            if geometry.is_empty:
                raise ValueError(f"{path.name}: drill or slot has no area")
            cutouts.append(geometry)
            if isinstance(primitive, gp.Circle):
                centers.append((primitive.x, primitive.y))
    return cutouts, centers


def parse_drill(text: str, path: Path, bounds: Bounds | None) -> tuple[list[BaseGeometry], list[Point]]:
    try:
        drill = gerbonara.ExcellonFile.from_string(text, filename=path)
        if drill.import_settings.unit is not None:
            return drill_shapes(drill, path)
    except SyntaxError as exc:
        if "number format" not in str(exc):
            raise
    # The file does not say how its coordinates are written. Keep the readings
    # that put every hole on the board; exactly one distinct reading must remain.
    fits = {}
    if bounds is not None:
        xmin, ymin, xmax, ymax = bounds
        for unit, digits, zeros in DRILL_FORMATS:
            settings = FileSettings(unit=unit, number_format=digits, zeros=zeros)
            try:
                drill = gerbonara.ExcellonFile.from_string(text, settings=settings, filename=path)
                shapes = drill_shapes(drill, path)
            except (SyntaxError, ValueError):
                continue
            spots = [(geometry.centroid, geometry.area) for geometry in shapes[0]]
            if all(xmin - 0.5 <= p.x <= xmax + 0.5 and ymin - 0.5 <= p.y <= ymax + 0.5 for p, _ in spots):
                fits.setdefault(tuple((round(p.x, 3), round(p.y, 3), round(area, 4)) for p, area in spots), shapes)
    if len(fits) != 1:
        reason = ("there is no board outline to check it against" if bounds is None
                  else "several formats fit the board" if fits
                  else "no common format puts every hole on the board")
        raise ValueError(f"{path.name}: the drill format could not be determined ({reason}); "
                         "export the drill file with a units and number-format header")
    return next(iter(fits.values()))


def read_drills(paths: list[Path], bounds: Bounds | None = None) -> tuple[BaseGeometry, tuple[Point, ...]]:
    """Read drills and slots in mm. `bounds` (xmin, ymin, xmax, ymax of the
    outline) selects the number format of files that do not declare one."""
    cutouts = []
    centers = []
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        # KiCad's alternate slot mode uses a canned G85 cycle. Gerbonara 1.5
        # accepts the equivalent route sequence and handles the units/tool.
        coordinate = r"[XY][+-]?(?:\d+(?:\.\d*)?|\.\d+)"
        slot = re.compile(rf"^\s*({coordinate}{coordinate})\s*G85\s*({coordinate}{coordinate})\s*$")
        lines = []
        for line in text.splitlines():
            match = slot.fullmatch(line)
            if match:
                start, end = match.groups()
                lines.extend((f"G00{start}", "M15", f"G01{end}", "M16", "G05"))
            else:
                lines.append(line)
        shapes, holes = parse_drill("\n".join(lines), path, bounds)
        cutouts.extend(shapes)
        centers.extend(holes)
    return union_all(cutouts), tuple(dict.fromkeys(centers))


@dataclass(frozen=True)
class BoardSide:
    side: str
    outline: BaseGeometry
    boundary: BaseGeometry
    retained: BaseGeometry
    removal: BaseGeometry
    mask_openings: BaseGeometry  # board area with no solder mask
    silkscreen: BaseGeometry
    references: tuple[Point, ...]
    origin: Point
    flip: str = ""   # the mirror applied to this side: "" for the top, else the flip axis
    inferred: bool = False
    drawn_copper: bool = False


def file_polarity(layer, default):
    values = layer.raw_gerber.file_attrs.get(".FilePolarity", (default,))
    if len(values) != 1 or values[0].casefold() not in {"positive", "negative"}:
        raise ValueError("Unknown Gerber file polarity")
    return values[0].casefold()


def prepare_board(
    inputs: dict[str, list[Path]], *, mode: InversionMode,
    isolation_width: float = 0.25, flip_axis: str = "vertical", kerf_mm: float = 0.0,
    allow_inferred: bool = False, work_area: BaseGeometry | None = None,
) -> list[BoardSide]:
    if mode not in {InversionMode.ISOLATION, InversionMode.FULL_INVERT, InversionMode.POSITIVE}:
        raise ValueError("Choose how to use the copper image")
    if not math.isfinite(isolation_width) or isolation_width <= 0:
        raise ValueError("Isolation width must be positive and finite")
    if not math.isfinite(kerf_mm) or kerf_mm < 0:
        raise ValueError("Kerf must be zero or positive and finite")
    profiles = inputs.get("outline", [])
    if len(profiles) > 1 or (not profiles and not allow_inferred and work_area is None):
        raise ValueError("Select one closed board outline")
    layers = {}
    for role in ROLES[:6]:
        paths = inputs.get(role, [])
        if len(paths) > 1:
            raise ValueError(f"Select one {role.replace('_', ' ')} file")
        if paths:
            layers[role] = parse_gerber_file(str(paths[0]))
    if not layers:
        raise ValueError("Select at least one copper, mask or silkscreen layer")
    inferred = not profiles and work_area is None
    if inferred:
        paired = len({role.split("_")[0] for role in layers}) > 1
        needs_outline = any(
            (role.endswith("_copper")
             and (mode == InversionMode.FULL_INVERT or file_polarity(layer, "positive") == "negative"))
            or (role.endswith("_mask") and file_polarity(layer, "negative") == "positive")
            for role, layer in layers.items()
        )
        if paired or needs_outline:
            raise ValueError("Add a real board outline for two sides, full clear, negative copper, or positive solder mask")
        copper = next((layer.geometry for role, layer in layers.items() if role.endswith("_copper")), None)
        extent = union_all([layer.geometry for layer in layers.values()])
        if extent.is_empty:
            raise ValueError("The selected layers have no drawable area")
        margin = max(0.01, isolation_width) if copper is not None and mode == InversionMode.ISOLATION else 0.0
        x0, y0, x1, y1 = extent.bounds
        profile = box(x0 - margin, y0 - margin, x1 + margin, y1 + margin)
    else:
        profile = read_outline(profiles[0]) if profiles else work_area
    if profile is None or profile.is_empty:
        raise ValueError("The board work area is empty")
    drills, references = read_drills(inputs.get("drill", []), None if inferred else profile.bounds)
    board = profile.difference(drills)
    if board.is_empty:
        raise ValueError("The outline and drill files leave no board area")
    xmin, ymin, xmax, ymax = profile.bounds
    origin = ((xmin + xmax) / 2, (ymin + ymax) / 2)
    if flip_axis not in {"vertical", "horizontal"}:
        raise ValueError("Choose a vertical or horizontal flip axis")

    sides = []
    for side in ("top", "bottom"):
        copper_layer, mask, silk = (layers.get(f"{side}_{kind}") for kind in ("copper", "mask", "silk"))
        if not (copper_layer or mask or silk):
            continue
        # Half the kerf comes off every edge of a lasered region, so the finite
        # spot width leaves the retained material at its drawn size.
        mask_openings = GeometryCollection()
        if mask:
            openings = (board.difference(mask.geometry) if file_polarity(mask, "negative") == "positive"
                        else board.intersection(mask.geometry))
            mask_openings = openings.buffer(-kerf_mm / 2) if kerf_mm else openings
        if silk and file_polarity(silk, "positive") == "negative":
            raise ValueError("Negative silkscreen requires a positive artwork export")
        silkscreen = board.intersection(silk.geometry) if silk else GeometryCollection()
        copper = removal = GeometryCollection()
        if copper_layer:
            if mode == InversionMode.POSITIVE:
                removal = board.intersection(copper_layer.geometry)
            else:
                copper = (board.difference(copper_layer.geometry) if file_polarity(copper_layer, "positive") == "negative"
                          else board.intersection(copper_layer.geometry))
                if copper.is_empty:
                    raise ValueError(f"No retained {side} copper overlaps the work area. Check the fabrication origin.")
                removal = invert_layer(
                    copper, mode, BoundaryMode.OUTLINE, isolation_width_mm=isolation_width, outline_geometry=board,
                )
                if kerf_mm:
                    removal = removal.buffer(-kerf_mm / 2)
            if removal.is_empty and (allow_inferred or work_area is not None):
                raise ValueError(f"No {side} copper tool area remains. Check the outline, isolation width and kerf.")
        boundary = board
        outline = profile
        marks = tuple(point for point in references if profile.covers(ShapelyPoint(point)))
        if side == "bottom":
            x_scale = -1 if flip_axis == "vertical" else 1
            y_scale = -1 if flip_axis == "horizontal" else 1
            outline, boundary, copper, removal, mask_openings, silkscreen = (
                affinity.scale(geometry, x_scale, y_scale, origin=origin)
                for geometry in (outline, boundary, copper, removal, mask_openings, silkscreen)
            )
            ox, oy = origin
            marks = tuple((ox + x_scale * (x - ox), oy + y_scale * (y - oy)) for x, y in marks)
        sides.append(BoardSide(side, outline, boundary, copper, removal, mask_openings, silkscreen, marks, origin,
                               flip_axis if side == "bottom" else "", inferred, mode == InversionMode.POSITIVE))
    if not sides:
        raise ValueError("Select at least one copper, mask or silkscreen layer")
    return sides


def prepare_jobs(inputs, presets, *, first_side="top", target=None, **settings):
    """Build the jobs shown in import review, using the target's fabrication space when adding."""
    from dataclasses import replace
    from ..workspace import job_from_board

    if target is not None:
        if not target.side or target.origin is None:
            raise ValueError("This job has no saved PCB origin. Reimport the board or create a separate job.")
        if any(role not in {f"{target.side}_copper", f"{target.side}_mask", f"{target.side}_silk"}
               for role in inputs):
            raise ValueError(f"Add only {target.side} copper, mask or silkscreen to this side. Use a new job for other files.")
        if not target.work_area:
            raise ValueError("This job does not record its usable board area. Reimport it to keep holes and slots excluded.")
        if target.inferred:
            copper_file = next(iter(inputs.get(f"{target.side}_copper", [])), None)
            negative = (copper_file is not None and
                        file_polarity(parse_gerber_file(str(copper_file)), "positive") == "negative")
            mask_file = next(iter(inputs.get(f"{target.side}_mask", [])), None)
            positive_mask = (mask_file is not None and
                             file_polarity(parse_gerber_file(str(mask_file)), "negative") == "positive")
            if positive_mask or (copper_file and (settings["mode"] == InversionMode.FULL_INVERT or negative)):
                raise ValueError("This operation needs a recorded board work area. Reimport with a real outline.")
        # Convert the saved usable area back to fabrication coordinates. It
        # includes cutouts and drill exclusions for newly imported boards.
        sx = -1 if target.flip == "vertical" else 1
        sy = -1 if target.flip == "horizontal" else 1
        ox, oy = target.origin
        rings = tuple(tuple((sx * x + ox, sy * y + oy) for x, y in ring)
                      for ring in target.work_area)
        settings.update(work_area=polygon_region(rings), flip_axis=target.flip or "vertical")
    sides = prepare_board(inputs, **settings)
    name = next(iter(inputs.get("outline", [])), None) or next(path for paths in inputs.values() for path in paths)
    name = name.stem
    for suffix in ("-Edge_Cuts", "_Edge_Cuts", "-outline", "_outline"):
        name = name.removesuffix(suffix)
    board_id = target.board_id if target else uuid4().hex
    jobs = [job_from_board(name, side, board_id, presets, first_side=first_side)
            for side in sorted(sides, key=lambda side: side.side != first_side)]
    if target is not None:
        new = jobs[0]
        # prepare_board centers on the usable area; keep the target's original
        # center even when holes at the edge changed the usable bounds.
        dx, dy = target.from_fabrication(new.origin)

        def shift(paths):
            return tuple(tuple((x + dx, y + dy) for x, y in path) for path in paths)

        additions = tuple(replace(obj, paths=shift(obj.paths), fills=shift(obj.fills)) for obj in new.objects)
        retained = contours(polygon_region(target.retained).union(polygon_region(shift(new.retained))))
        return [replace(target, objects=target.objects + additions, retained=retained)]
    return jobs
