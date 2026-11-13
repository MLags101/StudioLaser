"""Read one file (SVG, DXF, STL or a single Gerber layer) as an operation for a job.

A file read here can start a new job or be added to an existing one as another
operation. Gerber coordinates are fabrication millimetres: added to a board
side, they land where that side's own copper is, mirrored for the bottom.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from .layers import RASTER_ENGRAVE, VECTOR_ENGRAVE, LayerSpec
from .workspace import KIND_LABELS, MASK, SILK, Artwork, Job, Paths, new_layer

ARTWORK_SUFFIXES = {".svg", ".dxf", ".stl"}
GERBER_SUFFIXES = {
    ".gbr", ".ger", ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gko", ".gm1", ".gml",
    ".pho", ".art", ".cmp", ".sol", ".plc", ".pls", ".stc", ".sts",
}
LAYER_SUFFIXES = ARTWORK_SUFFIXES | GERBER_SUFFIXES
FILE_FILTER = ("Artwork and Gerber layers (*.svg *.dxf *.stl *.gbr *.ger *.g?? *.gm1 *.pho *.art *.cmp *.sol);;"
               "SVG (*.svg);;DXF (*.dxf);;Gerber (*.gbr *.ger *.g?? *.gm1 *.pho *.art);;All files (*)")


@dataclass(frozen=True)
class FileLayer:
    """One file's geometry. ``fabrication`` means the coordinates are absolute Gerber millimetres."""

    name: str
    vectors: Paths
    fills: Paths
    kind: str = ""
    fabrication: bool = False


def is_layer_file(path: Path) -> bool:
    return path.suffix.lower() in LAYER_SUFFIXES


def read_layer(path: Path, width: float | None = None) -> FileLayer:
    suffix = path.suffix.lower()
    if suffix == ".svg":
        from .svg_artwork import import_svg

        artwork = import_svg(path, width)
        return FileLayer(path.stem, artwork.vectors, artwork.fills)
    if suffix in {".dxf", ".stl"}:
        from .importers import import_geometry

        payload = "data:application/octet-stream;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        item = import_geometry(path.name, payload)
        vectors = tuple(tuple(map(tuple, line)) for line in item["polylines"])
        fills = tuple(line for line in vectors if len(line) > 3 and line[0] == line[-1])
        if suffix == ".stl":
            fills = ()   # A mesh projection is overlapping triangles; hatching it is meaningless.
        return FileLayer(path.stem, vectors, fills)
    return read_gerber_layer(path)


def read_gerber_layer(path: Path) -> FileLayer:
    """The dark (drawn) areas of one Gerber file, in fabrication millimetres."""
    from .pcb.board import contours
    from .pcb.gerber_parser import parse_gerber_file

    try:
        layer = parse_gerber_file(str(path))
    except Exception as exc:
        raise ValueError(f"{path.name} is not a readable Gerber file ({exc})") from exc
    shapes = contours(layer.geometry)
    if not shapes:
        raise ValueError(f"{path.name} has no drawn shapes")
    kind = {"silk": SILK, "mask": MASK}.get(layer.metadata.layer_type.rsplit("_", 1)[-1], "")
    return FileLayer(path.stem, shapes, shapes, kind, fabrication=True)


def centred(paths: Paths, centre) -> Paths:
    cx, cy = centre
    return tuple(tuple((x - cx, y - cy) for x, y in path) for path in paths)


def bounds_centre(*groups: Paths):
    points = [point for group in groups for path in group for point in path]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def layer_for(file_layer: FileLayer, defaults: dict[str, LayerSpec] | None = None) -> LayerSpec:
    """Settings a new operation starts with: the user's default preset for its kind, else zero power."""
    # Gerber shapes are areas, so they start as fill; drawings start as lines (switchable in Settings).
    operation = RASTER_ENGRAVE if file_layer.fabrication and file_layer.fills else VECTOR_ENGRAVE
    if file_layer.kind and defaults and file_layer.kind in defaults:
        base = replace(defaults[file_layer.kind])
    else:
        base = new_layer(file_layer.name, operation)
    return replace(base, layer_id=uuid4().hex[:8], name=file_layer.name, operation=operation, output=True)


def artwork_for(file_layer: FileLayer, job: Job | None, defaults=None) -> tuple[Artwork, str]:
    """The operation to add, in ``job``'s design coordinates (or centred for a new job), and a note.

    A Gerber added to a board side is placed by that side's recorded fabrication origin and flip.
    """
    vectors, fills, note = file_layer.vectors, file_layer.fills, ""
    if file_layer.fabrication and job is not None and job.side:
        if job.origin is not None:
            vectors = tuple(tuple(map(job.from_fabrication, path)) for path in vectors)
            fills = tuple(tuple(map(job.from_fabrication, path)) for path in fills)
        else:
            raise ValueError(f"{file_layer.name}: this board has no saved Gerber origin. Reimport it or create a separate job.")
    elif file_layer.fabrication:
        centre = bounds_centre(vectors, fills)
        vectors, fills = centred(vectors, centre), centred(fills, centre)
    label = file_layer.name if not file_layer.kind else f"{file_layer.name} ({KIND_LABELS[file_layer.kind]})"
    return Artwork(label, vectors, fills, layer_for(file_layer, defaults), file_layer.kind), note


def job_for(file_layer: FileLayer, defaults=None) -> Job:
    artwork, _ = artwork_for(file_layer, None, defaults)
    return Job(file_layer.name, (artwork,), job_id=uuid4().hex)
