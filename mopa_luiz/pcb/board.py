"""Prepare both sides of a CNC-cut board in the original fabrication coordinates."""

from dataclasses import dataclass
import math
from pathlib import Path
import re

import gerbonara
from gerbonara import graphic_primitives as gp
from gerbonara.cam import FileSettings
from shapely import affinity, union_all
from shapely.geometry import GeometryCollection, Polygon
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


def prepare_board(
    inputs: dict[str, list[Path]], *, mode: InversionMode,
    isolation_width: float = 0.25, flip_axis: str = "vertical", kerf_mm: float = 0.0,
) -> list[BoardSide]:
    if mode not in {InversionMode.ISOLATION, InversionMode.FULL_INVERT}:
        raise ValueError("Choose Isolation or Full Clear for the PCB")
    if not math.isfinite(isolation_width) or isolation_width <= 0:
        raise ValueError("Isolation width must be positive and finite")
    if not math.isfinite(kerf_mm) or kerf_mm < 0:
        raise ValueError("Kerf must be zero or positive and finite")
    profiles = inputs.get("outline", [])
    if len(profiles) != 1:
        raise ValueError("Select one closed board outline")
    profile = read_outline(profiles[0])
    drills, references = read_drills(inputs.get("drill", []), profile.bounds)
    board = profile.difference(drills)
    if board.is_empty:
        raise ValueError("The outline and drill files leave no board area")
    xmin, ymin, xmax, ymax = profile.bounds
    origin = ((xmin + xmax) / 2, (ymin + ymax) / 2)
    if flip_axis not in {"vertical", "horizontal"}:
        raise ValueError("Choose a vertical or horizontal flip axis")

    def layer_file(role):
        paths = inputs.get(role, [])
        if len(paths) > 1:
            raise ValueError(f"Select one {role.replace('_', ' ')} file")
        return paths[0] if paths else None

    def drawn(path):
        return board.intersection(parse_gerber_file(str(path)).geometry) if path else GeometryCollection()

    sides = []
    for side in ("top", "bottom"):
        copper_file, mask_file, silk_file = (layer_file(f"{side}_{kind}") for kind in ("copper", "mask", "silk"))
        if not (copper_file or mask_file or silk_file):
            continue
        # Half the kerf comes off every edge of a lasered region, so the finite
        # spot width leaves the retained material at its drawn size.
        mask_openings = drawn(mask_file).buffer(-kerf_mm / 2) if mask_file else GeometryCollection()
        silkscreen = drawn(silk_file)
        copper = removal = GeometryCollection()
        if copper_file:
            layer = parse_gerber_file(str(copper_file))
            polarity = tuple(v.casefold() for v in layer.raw_gerber.file_attrs.get(".FilePolarity", ("Positive",)))
            if polarity == ("negative",):
                copper = board.difference(layer.geometry)
            elif polarity == ("positive",):
                copper = board.intersection(layer.geometry)
            else:
                raise ValueError(f"{copper_file.name}: unknown copper file polarity")
            removal = invert_layer(
                copper, mode, BoundaryMode.OUTLINE, isolation_width_mm=isolation_width, outline_geometry=board,
            )
            if kerf_mm:
                removal = removal.buffer(-kerf_mm / 2)
        boundary = board
        outline = profile
        marks = references
        if side == "bottom":
            x_scale = -1 if flip_axis == "vertical" else 1
            y_scale = -1 if flip_axis == "horizontal" else 1
            outline, boundary, copper, removal, mask_openings, silkscreen = (
                affinity.scale(geometry, x_scale, y_scale, origin=origin)
                for geometry in (outline, boundary, copper, removal, mask_openings, silkscreen)
            )
            ox, oy = origin
            marks = tuple((ox + x_scale * (x - ox), oy + y_scale * (y - oy)) for x, y in references)
        sides.append(BoardSide(side, outline, boundary, copper, removal, mask_openings, silkscreen, marks, origin))
    if not sides:
        raise ValueError("Select at least one copper, mask or silkscreen layer")
    return sides
