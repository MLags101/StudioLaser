"""Gerber parser module for converting RS-274X files into 2D Shapely geometries."""

import os
import math
import re
from dataclasses import dataclass
from typing import Optional, Tuple, List, Union

import gerbonara
import gerbonara.graphic_primitives as gp
from shapely.geometry import box, Polygon, MultiPolygon, LineString, Point
from shapely.ops import linemerge, unary_union
from shapely.validation import make_valid
import shapely.affinity as affinity


@dataclass
class LayerMetadata:
    filename: str
    filepath: str
    layer_name: str
    layer_type: str  # '{top,bottom}_{copper,mask,silk}', 'inner_copper', 'outline', 'other'
    is_bottom: bool = False
    is_outline: bool = False
    width_mm: float = 0.0
    height_mm: float = 0.0
    bounds: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # (xmin, ymin, xmax, ymax)


@dataclass
class ParsedGerberLayer:
    metadata: LayerMetadata
    geometry: Union[Polygon, MultiPolygon]
    raw_gerber: Optional[gerbonara.GerberFile] = None


# Anchored role patterns: (regex on the lower-case file name, layer type).
# KiCad names end in the layer name, Protel/Altium and Eagle use the extension.
_SEP = r"(?:^|[-_. ])"
ROLE_PATTERNS = tuple((re.compile(pattern), role) for pattern, role in (
    (_SEP + r"edge[._]?cuts(?:\.\w+)?$|" + _SEP + r"(?:outline|profile)\.\w+$|\.(?:gko|gm1|gml|oln)$", "outline"),
    (_SEP + r"f[._]cu(?:\.\w+)?$|" + _SEP + r"copper_top\.\w+$|\.(?:gtl|cmp|top)$", "top_copper"),
    (_SEP + r"b[._]cu(?:\.\w+)?$|" + _SEP + r"copper_bottom\.\w+$|\.(?:gbl|sol|bot)$", "bottom_copper"),
    (_SEP + r"in\d+[._]cu(?:\.\w+)?$|\.g\d+$", "inner_copper"),
    (_SEP + r"f[._]mask(?:\.\w+)?$|" + _SEP + r"soldermask_top\.\w+$|\.(?:gts|stc|smt)$", "top_mask"),
    (_SEP + r"b[._]mask(?:\.\w+)?$|" + _SEP + r"soldermask_bottom\.\w+$|\.(?:gbs|sts|smb)$", "bottom_mask"),
    (_SEP + r"f[._]silk(?:s|screen)(?:\.\w+)?$|" + _SEP + r"silkscreen_top\.\w+$|\.(?:gto|plc|sst)$", "top_silk"),
    (_SEP + r"b[._]silk(?:s|screen)(?:\.\w+)?$|" + _SEP + r"silkscreen_bottom\.\w+$|\.(?:gbo|pls|ssb)$", "bottom_silk"),
))


def role_flags(layer_type: str) -> Tuple[str, bool, bool]:
    return layer_type, layer_type.startswith("bottom_"), layer_type == "outline"


def detect_layer_type(filepath: str) -> Tuple[str, bool, bool]:
    """
    Detect layer type from the KiCad, Protel/Altium or Eagle file name.
    Returns (layer_type, is_bottom, is_outline).
    """
    filename = os.path.basename(filepath).lower()
    for pattern, role in ROLE_PATTERNS:
        if pattern.search(filename):
            return role_flags(role)
    return role_flags("other")


def primitive_to_shapely(prim, circle_resolution: int = 32) -> Optional[Union[Polygon, MultiPolygon]]:
    """Convert a gerbonara graphic primitive into a Shapely geometry."""
    if isinstance(prim, gp.Line):
        if math.isclose(prim.x1, prim.x2, abs_tol=1e-6) and math.isclose(prim.y1, prim.y2, abs_tol=1e-6):
            return Point(prim.x1, prim.y1).buffer(prim.width / 2.0, quad_segs=circle_resolution // 4)
        ls = LineString([(prim.x1, prim.y1), (prim.x2, prim.y2)])
        return ls.buffer(prim.width / 2.0, cap_style='round', quad_segs=circle_resolution // 4)

    elif isinstance(prim, gp.Circle):
        return Point(prim.x, prim.y).buffer(prim.r, quad_segs=circle_resolution // 4)

    elif isinstance(prim, gp.Rectangle):
        rect = box(prim.x - prim.w / 2.0, prim.y - prim.h / 2.0,
                   prim.x + prim.w / 2.0, prim.y + prim.h / 2.0)
        if not math.isclose(prim.rotation, 0.0, abs_tol=1e-4):
            rect = affinity.rotate(rect, -math.degrees(prim.rotation), origin='center')
        return rect

    elif isinstance(prim, gp.Arc):
        pts = arc_points(prim)
        if len(pts) >= 2:
            ls = LineString(pts)
            return ls.buffer(prim.width / 2.0, cap_style='round', quad_segs=circle_resolution // 4)
        raise ValueError("Gerber arc has fewer than two points")

    elif isinstance(prim, gp.ArcPoly):
        outline = []
        for first, last, (clockwise, center) in prim.segments:
            if clockwise is None:
                outline.append(first)
            else:
                outline.extend(list(gp.approximate_arc(
                    *center, *first, *last, clockwise, max_error=0.001,
                ))[:-1])
        if len(outline) >= 3:
            p = Polygon(outline)
            return p if p.is_valid else make_valid(p)
        raise ValueError("Gerber region has fewer than three points")

    raise ValueError(f"unsupported Gerber primitive {type(prim).__name__}")


def arc_points(primitive):
    return list(gp.approximate_arc(
        primitive.cx, primitive.cy, primitive.x1, primitive.y1,
        primitive.x2, primitive.y2, primitive.clockwise, max_error=0.001,
    ))


def layer_type_for_file(gf, filepath):
    function = tuple(value.lower() for value in gf.file_attrs.get(".FileFunction", ()))
    if function:
        side = "bottom" if "bot" in function else "top" if "top" in function else None
        kind = {"copper": "copper", "soldermask": "mask", "legend": "silk"}.get(function[0])
        if function[0] == "profile":
            return role_flags("outline")
        if kind == "copper" and side is None:
            return role_flags("inner_copper")
        return role_flags(f"{side}_{kind}" if kind and side else "other")
    return detect_layer_type(filepath)


def snap_endpoints(lines, tolerance=1e-3):
    """Join contour ends that differ only by arc-approximation float noise."""
    nodes = {}

    def snap(point):
        point = (float(point[0]), float(point[1]))
        cx, cy = (round(value / tolerance) for value in point)
        for key in ((cx + dx, cy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
            if key in nodes and math.dist(nodes[key], point) <= tolerance:
                return nodes[key]
        nodes[cx, cy] = point
        return point

    snapped = ([snap(line[0]), *line[1:-1], snap(line[-1])] for line in lines)
    return [line for line in snapped if len(line) > 2 or line[0] != line[-1]]


def read_outline(path):
    """Use routed centerlines and apply even-odd nesting to closed outlines."""
    lines = []
    regions = []
    for obj in gerbonara.GerberFile.open(path).objects:
        for primitive in obj.to_primitives(gerbonara.MM):
            if isinstance(primitive, gp.Line):
                lines.append([(primitive.x1, primitive.y1), (primitive.x2, primitive.y2)])
            elif isinstance(primitive, gp.Arc):
                lines.append(arc_points(primitive))
            elif isinstance(primitive, gp.ArcPoly):
                regions.append(primitive_to_shapely(primitive))
            else:
                raise ValueError(f"{path}: unsupported outline primitive {type(primitive).__name__}")
    lines = snap_endpoints(lines)
    if lines:
        merged = linemerge(lines)
        rings = (merged,) if isinstance(merged, LineString) else merged.geoms
        for ring in rings:
            if not ring.is_ring:
                raise ValueError(f"{path}: board profile has an open or intersecting contour")
            regions.append(Polygon(ring.coords))
    outline = Polygon()
    for region in regions:
        outline = outline.symmetric_difference(region)
    if outline.is_empty or not outline.is_valid:
        raise ValueError(f"{path}: a valid closed board profile is required")
    return outline


def parse_gerber_file(filepath: str, circle_resolution: int = 32) -> ParsedGerberLayer:
    """
    Parse a Gerber RS-274X file into a unified 2D Shapely geometry with proper
    polarity composite resolution.
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Gerber file not found: {filepath}")

    filename = os.path.basename(filepath)
    layer_name = os.path.splitext(filename)[0]
    gf = gerbonara.GerberFile.open(filepath)
    layer_type, is_bottom, is_outline = layer_type_for_file(gf, filepath)

    current_dark_geoms: List[Union[Polygon, MultiPolygon]] = []
    current_clear_geoms: List[Union[Polygon, MultiPolygon]] = []
    active_copper: Union[Polygon, MultiPolygon] = Polygon()

    # Process all objects sequentially, grouping runs of dark and clear primitives
    for index, obj in enumerate(gf.objects, start=1):
        try:
            prims = list(obj.to_primitives(gerbonara.MM))
            geometries = [(primitive, primitive_to_shapely(primitive, circle_resolution)) for primitive in prims]
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"{filename}, object {index}: {exc}") from exc

        for prim, geom in geometries:
            if geom is None or geom.is_empty:
                continue

            if prim.polarity_dark:
                # Switching from clear to dark -> apply clear differences first
                if current_clear_geoms:
                    clear_union = unary_union(current_clear_geoms)
                    active_copper = active_copper.difference(clear_union)
                    current_clear_geoms = []
                current_dark_geoms.append(geom)
            else:
                # Switching from dark to clear -> union darks first
                if current_dark_geoms:
                    dark_union = unary_union(current_dark_geoms)
                    active_copper = unary_union([active_copper, dark_union])
                    current_dark_geoms = []
                current_clear_geoms.append(geom)

    # Flush remaining queues
    if current_dark_geoms:
        dark_union = unary_union(current_dark_geoms)
        active_copper = unary_union([active_copper, dark_union])
    if current_clear_geoms:
        clear_union = unary_union(current_clear_geoms)
        active_copper = active_copper.difference(clear_union)

    if not active_copper.is_valid:
        active_copper = make_valid(active_copper)

    if active_copper.is_empty:
        bounds = (0.0, 0.0, 0.0, 0.0)
        width_mm = 0.0
        height_mm = 0.0
    else:
        bounds = active_copper.bounds
        width_mm = bounds[2] - bounds[0]
        height_mm = bounds[3] - bounds[1]

    metadata = LayerMetadata(
        filename=filename,
        filepath=filepath,
        layer_name=layer_name,
        layer_type=layer_type,
        is_bottom=is_bottom,
        is_outline=is_outline,
        width_mm=width_mm,
        height_mm=height_mm,
        bounds=bounds
    )

    return ParsedGerberLayer(
        metadata=metadata,
        geometry=active_copper,
        raw_gerber=gf
    )
