"""Import millimetre artwork while keeping vector strokes and filled regions distinct."""

from dataclasses import dataclass
from io import StringIO
import math
from pathlib import Path
import re
from xml.etree import ElementTree

from shapely import union_all
from shapely.geometry import GeometryCollection, LineString
from shapely.ops import polygonize
from svgelements import Close, Line, Move, Path as SvgPath, SVG, Shape

from .pcb.board import Paths, contours


class SvgSizeRequired(ValueError):
    """The SVG needs an explicit artwork width before it can be used."""


@dataclass(frozen=True)
class SvgArtwork:
    vectors: Paths
    fills: Paths


def flatten(segment, scale):
    def point(t):
        p = segment.point(t)
        return (float(p.x) * scale, -float(p.y) * scale)

    def subdivide(start, end, a, b, depth):
        fractions = (0.25, 0.5, 0.75)
        samples = [point(start + (end - start) * part) for part in fractions]
        error = max(math.dist(sample, (a[0] + (b[0] - a[0]) * part, a[1] + (b[1] - a[1]) * part))
                    for part, sample in zip(fractions, samples))
        if error <= 0.003:
            yield b
        elif depth >= 20:
            raise ValueError("SVG curve exceeds the flattening tolerance")
        else:
            mid = (start + end) / 2
            yield from subdivide(start, mid, a, samples[1], depth + 1)
            yield from subdivide(mid, end, samples[1], b, depth + 1)

    yield point(0)
    if isinstance(segment, (Line, Close)):
        yield point(1)
    else:
        yield from subdivide(0, 1, point(0), point(1), 0)


def subpaths(shape, scale):
    points = []
    for segment in SvgPath(shape):
        if isinstance(segment, Move):
            if len(points) >= 2:
                yield tuple(points)
            points = []
            continue
        vertices = list(flatten(segment, scale))
        points.extend(vertices[1:] if points else vertices)
    if len(points) >= 2:
        yield tuple(points)


def fill_shape(rings, rule):
    closed = [ring if ring[0] == ring[-1] else (*ring, ring[0]) for ring in rings if len(ring) >= 3]
    if not closed:
        return []
    faces = polygonize(union_all([LineString(ring) for ring in closed]))
    filled = []
    for face in faces:
        p = face.representative_point()
        winding = 0
        for ring in closed:
            for (ax, ay), (bx, by) in zip(ring, ring[1:]):
                if (ay <= p.y < by or by <= p.y < ay) and ax + (p.y - ay) * (bx - ax) / (by - ay) > p.x:
                    winding += 1 if ay < by else -1
        if (winding % 2 if rule == "evenodd" else winding):
            filled.append(face)
    return filled


def import_svg(path: Path, width_mm: float | None = None) -> SvgArtwork:
    text = path.read_text(encoding="utf-8-sig")
    root = ElementTree.fromstring(text)
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError("Select an SVG document")
    unsupported = {"text", "image", "mask", "clipPath", "filter", "pattern", "foreignObject"}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in unsupported:
            raise ValueError(f"Convert SVG {tag} to plain vector paths before importing")
        if any(key.rsplit("}", 1)[-1] == "href" and not val.startswith("#")
               for key, val in element.attrib.items()):
            raise ValueError("SVG external references are unsupported")
    physical = re.compile(r"\s*[+]?(?:\d+(?:\.\d*)?|\.\d+)\s*(?:mm|cm|in|pt|pc|px)\s*")
    absolute = all(physical.fullmatch(root.get(key, "")) for key in ("width", "height"))
    if not absolute and width_mm is None:
        raise SvgSizeRequired("Enter the finished artwork width in millimetres")
    if width_mm is not None and (not math.isfinite(width_mm) or width_mm <= 0):
        raise ValueError("Artwork width must be positive and finite")
    # svgelements applies <style> rules with simple element/.class/#id selectors
    # and inherits presentation attributes from ancestor groups.
    svg = SVG.parse(StringIO(text), ppi=96, reify=True, on_error="raise")
    shapes = [shape for shape in svg.elements()
              if isinstance(shape, Shape) and shape.values.get("visibility") not in {"hidden", "collapse"}
              and shape.values.get("display") != "none"]
    boxes = [bounds for shape in shapes if (bounds := shape.bbox()) is not None]
    if not boxes:
        raise ValueError("SVG contains no usable vector geometry")
    source_width = max(b[2] for b in boxes) - min(b[0] for b in boxes)
    if width_mm is not None and source_width <= 0:
        raise ValueError("SVG has no measurable artwork width")
    scale = width_mm / source_width if width_mm is not None else 25.4 / 96
    vectors = []
    fills = GeometryCollection()
    painted = []
    for shape in shapes:
        paths = list(subpaths(shape, scale))
        vectors.extend(paths)
        fill = shape.fill
        if fill is not None and fill.value is not None:
            faces = fill_shape(paths, shape.values.get("fill-rule", "nonzero"))
            if fill.red == fill.green == fill.blue == 255:
                # A white fill is a knock-out: it erases what was painted
                # under it and never marks anything itself.
                fills = union_all([fills, *painted]).difference(union_all(faces))
                painted = []
            else:
                painted.extend(faces)
    fills = union_all([fills, *painted])
    if not vectors or not all(math.isfinite(value) for path in vectors for point in path for value in point):
        raise ValueError("SVG contains no finite toolpath geometry")
    if absolute and width_mm is None:
        ox, oy = float(svg.width) * scale / 2, -float(svg.height) * scale / 2
    else:
        ox = (min(b[0] for b in boxes) + max(b[2] for b in boxes)) * scale / 2
        oy = -(min(b[1] for b in boxes) + max(b[3] for b in boxes)) * scale / 2
    def center(paths):
        return tuple(tuple((x - ox, y - oy) for x, y in path) for path in paths)

    return SvgArtwork(center(vectors), center(contours(fills)))
