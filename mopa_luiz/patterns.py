"""Test and calibration patterns: squares, parameter matrices, kerf ladders, grids."""

from __future__ import annotations

from dataclasses import replace
import math
from uuid import uuid4

from .correction import grid_points
from .layers import LayerSpec, RASTER_ENGRAVE, VECTOR_ENGRAVE
from .workspace import Artwork, Job, Paths, rectangle

# Stroke glyphs on a 4 x 6 grid; enough for parameter labels.
_GLYPHS = {
    "0": (((0, 0), (4, 0), (4, 6), (0, 6), (0, 0)),),
    "1": (((1, 5), (2, 6), (2, 0)),),
    "2": (((0, 6), (4, 6), (4, 3), (0, 3), (0, 0), (4, 0)),),
    "3": (((0, 6), (4, 6), (4, 0), (0, 0)), ((1, 3), (4, 3))),
    "4": (((0, 6), (0, 3), (4, 3)), ((4, 6), (4, 0))),
    "5": (((4, 6), (0, 6), (0, 3), (4, 3), (4, 0), (0, 0)),),
    "6": (((4, 6), (0, 6), (0, 0), (4, 0), (4, 3), (0, 3)),),
    "7": (((0, 6), (4, 6), (2, 0)),),
    "8": (((0, 0), (4, 0), (4, 6), (0, 6), (0, 0)), ((0, 3), (4, 3))),
    "9": (((4, 3), (0, 3), (0, 6), (4, 6), (4, 0), (0, 0)),),
    ".": (((1.6, 0), (2.4, 0), (2.4, 0.8), (1.6, 0.8), (1.6, 0)),),
    "-": (((0.5, 3), (3.5, 3)),),
    "/": (((0.5, 0), (3.5, 6)),),
    "%": (((0, 0), (4, 6)), ((0, 5), (1, 5), (1, 6), (0, 6), (0, 5)), ((3, 0), (4, 0), (4, 1), (3, 1), (3, 0))),
    "A": (((0, 0), (2, 6), (4, 0)), ((1, 3), (3, 3))),
    "B": (((0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)), ((3, 3), (4, 2), (4, 1), (3, 0), (0, 0))),
    "C": (((4, 6), (0, 6), (0, 0), (4, 0)),),
    "D": (((0, 0), (0, 6), (3, 6), (4, 5), (4, 1), (3, 0), (0, 0)),),
    "E": (((4, 6), (0, 6), (0, 0), (4, 0)), ((0, 3), (3, 3))),
    "F": (((4, 6), (0, 6), (0, 0)), ((0, 3), (3, 3))),
    "G": (((4, 6), (0, 6), (0, 0), (4, 0), (4, 3), (2, 3)),),
    "H": (((0, 0), (0, 6)), ((4, 0), (4, 6)), ((0, 3), (4, 3))),
    "I": (((2, 0), (2, 6)),),
    "J": (((4, 6), (4, 0), (0, 0), (0, 2)),),
    "K": (((0, 0), (0, 6)), ((4, 6), (0, 3), (4, 0))),
    "L": (((0, 6), (0, 0), (4, 0)),),
    "M": (((0, 0), (0, 6), (2, 2), (4, 6), (4, 0)),),
    "N": (((0, 0), (0, 6), (4, 0), (4, 6)),),
    "O": (((0, 0), (4, 0), (4, 6), (0, 6), (0, 0)),),
    "P": (((0, 0), (0, 6), (4, 6), (4, 3), (0, 3)),),
    "Q": (((0, 0), (4, 0), (4, 6), (0, 6), (0, 0)), ((2, 2), (4, 0))),
    "R": (((0, 0), (0, 6), (4, 6), (4, 3), (0, 3)), ((2, 3), (4, 0))),
    "S": (((4, 6), (0, 6), (0, 3), (4, 3), (4, 0), (0, 0)),),
    "T": (((0, 6), (4, 6)), ((2, 6), (2, 0))),
    "U": (((0, 6), (0, 0), (4, 0), (4, 6)),),
    "V": (((0, 6), (2, 0), (4, 6)),),
    "W": (((0, 6), (1, 0), (2, 4), (3, 0), (4, 6)),),
    "X": (((0, 0), (4, 6)), ((0, 6), (4, 0))),
    "Y": (((0, 6), (2, 3), (4, 6)), ((2, 3), (2, 0))),
    "Z": (((0, 6), (4, 6), (0, 0), (4, 0)),),
}
ADVANCE = 5.5 / 6   # glyph pitch as a fraction of text height


def text_width(text: str, height: float) -> float:
    return max(0.0, len(text) * ADVANCE * height - 1.5 / 6 * height)


def text_paths(text: str, height: float, x: float, y: float, *, anchor: str = "left") -> Paths:
    """Stroke text with its baseline-left corner at (x, y). Unknown characters are skipped."""
    scale = height / 6
    if anchor == "center":
        x -= text_width(text, height) / 2
    elif anchor == "right":
        x -= text_width(text, height)
    paths = []
    for index, char in enumerate(text.upper()):
        for stroke in _GLYPHS.get(char, ()):
            paths.append(tuple((x + index * ADVANCE * height + px * scale, y + py * scale) for px, py in stroke))
    return tuple(paths)


def mark_layer(name="Marks") -> LayerSpec:
    """A gentle vector mark for labels and calibration crosses on card, paint or anodising."""
    return LayerSpec("marks", name, VECTOR_ENGRAVE, 30, 40, 100, 500)


def test_square(size: float, layer: LayerSpec, *, filled: bool) -> Job:
    path = rectangle(-size / 2, -size / 2, size / 2, size / 2)
    name = "Filled Test Square" if filled else "Outline Test Square"
    recipe = replace(layer, operation=RASTER_ENGRAVE if filled else VECTOR_ENGRAVE)
    return Job(name, (Artwork(name, (path,), (path,), recipe),), job_id=uuid4().hex)


def copper_coupon(size: float, layers: list[LayerSpec]) -> Job:
    if not 1 <= len(layers) <= 9 or not math.isfinite(size) or size <= 0:
        raise ValueError("Choose one to nine settings and a positive coupon size")
    columns = min(3, len(layers))
    rows = math.ceil(len(layers) / columns)
    artworks = []
    for i, layer in enumerate(layers):
        x = (i % columns - (columns - 1) / 2) * size * 1.2
        y = ((rows - 1) / 2 - i // columns) * size * 1.2
        x0, y0 = x - size / 2, y - size / 2
        regions = [rectangle(x0, y0, x0 + size * 0.42, y0 + size)]
        # Increasing clearance gaps beside the filled test patch.
        for row, fraction in enumerate((0.015, 0.025, 0.05, 0.09, 0.14)):
            bottom = y0 + row * size / 5
            regions.append(rectangle(x0 + size * 0.54, bottom, x0 + size, bottom + size * fraction))
        name = f"Cell {i + 1} · {layer.power_percent:g}% · {layer.speed_mm_s:g} mm/s"
        artworks.append(Artwork(name, tuple(regions), tuple(regions), replace(layer, operation=RASTER_ENGRAVE)))
    return Job("Copper coupon", tuple(artworks), job_id=uuid4().hex)


# field name -> (label, unit, one-letter tag, minimum, maximum, is integer)
PARAMETERS = {
    "power_percent": ("Power", "%", "P", 1, 100, False),
    "speed_mm_s": ("Speed", "mm/s", "S", 1, 10000, False),
    "frequency_khz": ("Frequency", "kHz", "F", 1, 4000, False),
    "pulse_width_ns": ("Pulse width", "ns", "W", 1, 500, True),
    "raster_pitch_mm": ("Hatch spacing", "mm", "H", 0.005, 1, False),
    "passes": ("Passes", "×", "N", 1, 50, True),
}


def steps(first: float, last: float, count: int, *, integer: bool = False) -> list[float]:
    if count < 1 or not all(math.isfinite(v) for v in (first, last)):
        raise ValueError("Choose a finite range and at least one step")
    if count == 1:
        values = [first]
    else:
        values = [first + (last - first) * i / (count - 1) for i in range(count)]
    return [int(round(v)) for v in values] if integer else [round(v, 4) for v in values]


def _label(value) -> str:
    return f"{value:g}"


def test_matrix(base: LayerSpec, row_param: str, row_values: list, col_param: str, col_values: list,
                *, cell: float = 5.0, gap: float = 2.0, labels: LayerSpec | None = None) -> Job:
    """A grid of patches, one parameter varying down the rows and one across the columns."""
    if row_param not in PARAMETERS or col_param not in PARAMETERS or row_param == col_param:
        raise ValueError("Choose two different parameters for the rows and columns")
    if not row_values or not col_values or len(row_values) * len(col_values) > 144:
        raise ValueError("A test matrix has 1–144 cells")
    for param, values in ((row_param, row_values), (col_param, col_values)):
        _, unit, _, low, high, _ = PARAMETERS[param]
        if any(not math.isfinite(v) or not low <= v <= high for v in values):
            raise ValueError(f"{PARAMETERS[param][0]} values must be {low:g}–{high:g} {unit}")
    if not (math.isfinite(cell) and math.isfinite(gap)) or cell < 1 or gap < 0.5:
        raise ValueError("Cells must be at least 1 mm with a 0.5 mm gap")
    labels = labels or mark_layer("Labels")
    pitch = cell + gap
    rows, cols = len(row_values), len(col_values)
    width, height = cols * pitch - gap, rows * pitch - gap
    left, top = -width / 2, height / 2
    # Size the labels so the longest column value fits within one column.
    longest = max(len(_label(value)) for value in col_values)
    text = min(2.0, cell * 0.4, (pitch - 0.8) / (longest * ADVANCE))
    marks = []

    def caption(param, direction):
        name, unit = PARAMETERS[param][0], PARAMETERS[param][1].replace("×", "X")
        return f"{name} {unit} {direction}"

    title = f"{caption(row_param, 'DOWN')} - {caption(col_param, 'ACROSS')}"
    title_height = min(text, max(width, 20.0) / (len(title) * ADVANCE))
    marks += text_paths(title, title_height, 0, top + text * 2.4, anchor="center")
    artworks = []
    for r, row_value in enumerate(row_values):
        y0 = top - r * pitch - cell
        marks += text_paths(_label(row_value), text, left - gap, y0 + (cell - text) / 2, anchor="right")
        for c, col_value in enumerate(col_values):
            x0 = left + c * pitch
            if r == 0:
                marks += text_paths(_label(col_value), text, x0 + cell / 2, top + text * 0.7, anchor="center")
            layer = replace(base, **{row_param: row_value, col_param: col_value})
            square = rectangle(x0, y0, x0 + cell, y0 + cell)
            if layer.operation == RASTER_ENGRAVE:
                shapes = (square,)
            else:  # A vector operation is judged on lines, not a filled patch.
                shapes = tuple(((x0, y0 + cell * f), (x0 + cell, y0 + cell * f)) for f in (0.2, 0.4, 0.6, 0.8))
            name = (f"{PARAMETERS[row_param][0]} {_label(row_value)} {PARAMETERS[row_param][1]} · "
                    f"{PARAMETERS[col_param][0]} {_label(col_value)} {PARAMETERS[col_param][1]}")
            artworks.append(Artwork(name, shapes, shapes if layer.operation == RASTER_ENGRAVE else (), layer, base.layer_id))
    artworks.append(Artwork("Labels", tuple(marks), (), labels))
    return Job(f"Test matrix — {PARAMETERS[row_param][0]} × {PARAMETERS[col_param][0]}", tuple(artworks),
               job_id=uuid4().hex)


def kerf_ladder(fill: LayerSpec, contour: LayerSpec, *, widths=(0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5),
                labels: LayerSpec | None = None) -> Job:
    """Isolated islands ringed by channels of increasing width, plus single lines.

    Probe from each island to the surrounding copper to find the narrowest
    channel that isolates. Measure a single line's width for the kerf.
    """
    if not widths or any(not math.isfinite(w) or not 0.02 <= w <= 2 for w in widths):
        raise ValueError("Channel widths must be 0.02–2 mm")
    labels = labels or mark_layer("Labels")
    island_w, island_h, gap, text = 6.0, 4.0, 3.0, 1.6
    pitch = island_w + 2 * max(widths) + gap
    left = -pitch * len(widths) / 2
    rings, marks = [], []
    for index, width in enumerate(widths):
        cx = left + pitch * (index + 0.5)
        outer = rectangle(cx - island_w / 2 - width, -island_h / 2 - width, cx + island_w / 2 + width, island_h / 2 + width)
        inner = rectangle(cx - island_w / 2, -island_h / 2, cx + island_w / 2, island_h / 2)
        rings += [outer, inner]
        marks += text_paths(_label(width), text, cx, island_h / 2 + max(widths) + 1.0, anchor="center")
    line_y = -island_h / 2 - max(widths) - 3.0
    lines = tuple(((left + 2, line_y - i * 1.5), (left + 22, line_y - i * 1.5)) for i in range(3))
    marks += text_paths("KERF", text, left + 24, line_y - 2.2)
    return Job("Kerf and isolation ladder", (
        Artwork("Isolation channels", tuple(rings), tuple(rings), replace(fill, operation=RASTER_ENGRAVE), fill.layer_id),
        Artwork("Kerf lines", lines, (), replace(contour, operation=VECTOR_ENGRAVE), contour.layer_id),
        Artwork("Labels", tuple(marks), (), labels),
    ), job_id=uuid4().hex)


def calibration_grid(span: float, count: int, layer: LayerSpec | None = None, *, arm: float = 2.0) -> Job:
    """Crosses on a square grid, with a corner flag marking the top-left point."""
    points = grid_points(span, count)
    marks = []
    for x, y in points:
        marks += [((x - arm, y), (x + arm, y)), ((x, y - arm), (x, y + arm))]
    x, y = points[0]
    flag = arm * 1.5
    marks.append(((x - flag, y + flag * 0.4), (x - flag, y + flag), (x - flag * 0.4, y + flag)))
    return Job(f"Calibration grid {count} × {count}, {span:g} mm",
               (Artwork("Calibration crosses", tuple(marks), (), layer or mark_layer("Calibration marks")),),
               job_id=uuid4().hex)


def focus_test(layer: LayerSpec, slot: int, *, slots: int = 9, labels: LayerSpec | None = None) -> Job:
    """One numbered group of fine lines. Run it once per Z height, a new slot each time,
    then pick the slot with the thinnest, brightest lines."""
    if not 1 <= slot <= slots:
        raise ValueError(f"Choose a slot from 1 to {slots}")
    pitch = 6.0
    x = (slot - (slots + 1) / 2) * pitch
    lines = tuple(((x - 2 + i * 0.5, -4), (x - 2 + i * 0.5, 4)) for i in range(9))
    marks = text_paths(str(slot), 2.0, x, 5.0, anchor="center")
    return Job(f"Focus test — slot {slot}", (
        Artwork("Focus lines", lines, (), replace(layer, operation=VECTOR_ENGRAVE, passes=1)),
        Artwork("Labels", marks, (), labels or mark_layer("Labels")),
    ), job_id=uuid4().hex)


def hole_targets(points: list[tuple[float, float]], radius: float = 1.0, segments: int = 24) -> Paths:
    """Red-light circles around registration holes, for a visual check."""
    return tuple(
        tuple((x + radius * math.cos(2 * math.pi * i / segments), y + radius * math.sin(2 * math.pi * i / segments))
              for i in range(segments + 1))
        for x, y in points
    )


def crosshair(point: tuple[float, float], arm: float = 0.6) -> Paths:
    x, y = point
    return (((x - arm, y), (x + arm, y)), ((x, y - arm), (x, y + arm)))
