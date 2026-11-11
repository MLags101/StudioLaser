"""Lens correction: JCZ .cor tables for the board, and measured field maps in software.

Two independent layers correct the galvo field.

* The controller board applies a 65x65 offset table. StudioLaser uploads a
  factory ``.cor`` file verbatim and never edits its contents, because the
  table's sign and axis conventions are only known from the factory tool.
* ``FieldMap`` is a polynomial that maps a target position in millimetres to
  the position to command. It is fitted from marks the operator measured, so
  its conventions are fixed by measurement rather than by documentation.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct

GRID = 65
CELLS = GRID * GRID
FLOAT_LABEL = "LMC1COR_1.0"
Point = tuple[float, float]


def cor_layout(data: bytes) -> tuple[int, str, float]:
    """Return (table offset, struct code, galvo units per mm) for a JCZ .cor file."""
    if len(data) < 36:
        raise ValueError("Truncated JCZ correction file")
    try:
        floating = data[:22].decode("utf-16") == FLOAT_LABEL
    except UnicodeError:
        floating = False
    if floating:
        if len(data) < 528:
            raise ValueError("Truncated JCZ correction file")
        return 528, "d", struct.unpack("<63d", data[24:528])[43]
    return 36, "i", struct.unpack("<d", data[28:36])[0]


def read_cor(data: bytes) -> tuple[float, list[tuple[float, float]]]:
    """Parse the layouts galvoplotter 0.2 uploads. Trailing bytes are tolerated.

    EZCAD writes ``JCZ_COR_2_1`` files with a 36-byte header, 4225 int32
    pairs and a short trailer, so an exact size check would reject real files.
    """
    offset, code, scale = cor_layout(data)
    size = struct.calcsize("<" + code)
    need = offset + CELLS * 2 * size
    if len(data) < need:
        raise ValueError("JCZ correction file is too short for a 65 x 65 table")
    if len(data) - need > 64:
        raise ValueError("Unsupported JCZ correction table size")
    values = struct.unpack(f"<{CELLS * 2}{code}", data[offset:need])
    if any(not math.isfinite(value) or abs(value) > 32767 for value in values):
        raise ValueError("JCZ correction table contains invalid offsets")
    if not math.isfinite(scale) or scale <= 0:
        scale = 0.0
    return scale, list(zip(values[::2], values[1::2]))


def validate_cor(data: bytes) -> None:
    read_cor(data)


def cor_summary(data: bytes) -> dict[str, float]:
    """Numbers an operator can sanity-check against the lens."""
    scale, table = read_cor(data)
    peak = max(max(abs(dx), abs(dy)) for dx, dy in table)
    return {
        "field_mm": 65536 / scale if scale else 0.0,
        "peak_offset_units": peak,
        "peak_offset_mm": peak / scale if scale else 0.0,
    }


def _terms(degree: int, *, tensor: bool = False) -> tuple[tuple[int, int], ...]:
    if tensor:
        return tuple((i, j) for i in range(degree + 1) for j in range(degree + 1))
    return tuple((i, j) for i in range(degree + 1) for j in range(degree + 1 - i))


@dataclass(frozen=True)
class FieldMap:
    """target mm -> commanded mm, as a polynomial in (x / half, y / half)."""

    half: float = 1.0
    terms: tuple[tuple[int, int], ...] = ((1, 0), (0, 1))
    cx: tuple[float, ...] = (1.0, 0.0)
    cy: tuple[float, ...] = (0.0, 1.0)

    def __post_init__(self):
        if not (len(self.terms) == len(self.cx) == len(self.cy)) or not self.terms:
            raise ValueError("Field map coefficients do not match its terms")
        values = (self.half, *self.cx, *self.cy)
        if not all(math.isfinite(v) for v in values) or self.half <= 0:
            raise ValueError("Field map must be finite")

    @property
    def identity(self) -> bool:
        wanted = {(1, 0): (1.0, 0.0), (0, 1): (0.0, 1.0)}
        return all(
            math.isclose(a, wanted.get(term, (0.0, 0.0))[0], abs_tol=1e-12)
            and math.isclose(b, wanted.get(term, (0.0, 0.0))[1], abs_tol=1e-12)
            for term, a, b in zip(self.terms, self.cx, self.cy)
        )

    def apply(self, point: Point) -> Point:
        u, v = point[0] / self.half, point[1] / self.half
        x = y = 0.0
        for (i, j), a, b in zip(self.terms, self.cx, self.cy):
            basis = u ** i * v ** j
            x += a * basis
            y += b * basis
        return x * self.half, y * self.half

    def to_dict(self) -> dict:
        return {"half": self.half, "terms": [list(t) for t in self.terms],
                "cx": list(self.cx), "cy": list(self.cy)}

    @classmethod
    def from_dict(cls, raw: dict) -> "FieldMap":
        return cls(float(raw["half"]), tuple((int(i), int(j)) for i, j in raw["terms"]),
                   tuple(map(float, raw["cx"])), tuple(map(float, raw["cy"])))


IDENTITY = FieldMap()


@dataclass(frozen=True)
class EzcadField:
    """The parametric field settings EZCAD2 stores in markcfg7 (F3 dialog)."""

    offset_x: float = 0.0
    offset_y: float = 0.0
    angle: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    bulge_x: float = 1.0
    bulge_y: float = 1.0
    skew_x: float = 1.0
    skew_y: float = 1.0
    trapezoid_x: float = 1.0
    trapezoid_y: float = 1.0

    def __post_init__(self):
        if not all(math.isfinite(v) for v in self.__dict__.values()):
            raise ValueError("EZCAD field settings must be finite")
        for name in ("scale_x", "scale_y", "bulge_x", "bulge_y", "trapezoid_x", "trapezoid_y"):
            if not 0.5 <= getattr(self, name) <= 2.0:
                raise ValueError(f"EZCAD field setting {name} is outside 0.5–2.0")

    @property
    def distorts(self) -> bool:
        return any(not math.isclose(getattr(self, name), 1.0, abs_tol=1e-9) for name in (
            "scale_x", "scale_y", "bulge_x", "bulge_y", "skew_x", "skew_y", "trapezoid_x", "trapezoid_y"))

    def field_map(self, half: float, *, distortion: bool) -> FieldMap:
        """Offset and rotation always apply. The distortion terms are StudioLaser's
        approximation of EZCAD's undocumented model; measure and calibrate."""
        angle = math.radians(self.angle)
        c, s = math.cos(angle), math.sin(angle)
        # Start from a rigid move in normalised units.
        x = {(1, 0): c, (0, 1): -s, (0, 0): self.offset_x / half}
        y = {(1, 0): s, (0, 1): c, (0, 0): self.offset_y / half}
        if distortion:
            sx, sy = self.scale_x, self.scale_y
            x = {term: value * sx for term, value in x.items()}
            y = {term: value * sy for term, value in y.items()}
            x[(1, 2)] = sx * (self.bulge_x - 1)
            x[(1, 1)] = sx * (self.trapezoid_x - 1)
            x[(0, 1)] = x.get((0, 1), 0.0) + (self.skew_x - 1)
            y[(2, 1)] = sy * (self.bulge_y - 1)
            y[(1, 1)] = sy * (self.trapezoid_y - 1)
            y[(1, 0)] = y.get((1, 0), 0.0) + (self.skew_y - 1)
        terms = tuple(sorted(set(x) | set(y)))
        return FieldMap(half, terms, tuple(x.get(t, 0.0) for t in terms), tuple(y.get(t, 0.0) for t in terms))


@dataclass(frozen=True)
class Fit:
    field_map: FieldMap
    residuals: tuple[float, ...]   # mm, per point, of the fit itself
    before: tuple[float, ...]      # mm, per point, error of the marks as measured

    @property
    def rms(self) -> float:
        return math.sqrt(sum(r * r for r in self.residuals) / len(self.residuals))

    @property
    def rms_before(self) -> float:
        return math.sqrt(sum(r * r for r in self.before) / len(self.before))


def fit_field_map(commanded: list[Point], measured: list[Point], targets: list[Point],
                  half: float, *, degree: int | None = None) -> Fit:
    """Fit the map that would have put each mark on its target.

    ``commanded`` are the positions actually sent after the calibration that
    was active, ``measured`` where the marks landed, ``targets`` where they
    were meant to land. Regressing commanded on measured gives the new map
    directly, so repeating the procedure refines rather than stacks.
    """
    import numpy as np

    count = len(commanded)
    if count != len(measured) or count != len(targets) or count < 4:
        raise ValueError("Enter at least four measured points")
    flat = [v for group in (commanded, measured, targets) for point in group for v in point]
    if not all(math.isfinite(v) for v in flat):
        raise ValueError("Calibration measurements must be finite")
    if degree is None:
        degree = 3 if count >= 16 else 2 if count >= 9 else 1
    terms = _terms(degree, tensor=(degree == 2 and count == 9)) if degree > 1 else ((0, 0), (1, 0), (0, 1))
    if count < len(terms):
        raise ValueError("Not enough measured points for this correction")
    m = np.asarray(measured, dtype=float) / half
    c = np.asarray(commanded, dtype=float) / half
    basis = np.stack([m[:, 0] ** i * m[:, 1] ** j for i, j in terms], axis=1)
    if np.linalg.matrix_rank(basis) < len(terms):
        raise ValueError("Measured points do not span the field. Use a full grid.")
    solution, *_ = np.linalg.lstsq(basis, c, rcond=None)
    fitted = basis @ solution
    residuals = tuple(float(v) * half for v in np.hypot(*(fitted - c).T))
    before = tuple(math.dist(a, b) for a, b in zip(measured, targets))
    field_map = FieldMap(half, terms, tuple(map(float, solution[:, 0])), tuple(map(float, solution[:, 1])))
    # A correction is a trim. Refuse a fit that would move a point by more than 15 % of the field.
    for point in targets:
        if math.dist(field_map.apply(point), point) > 0.3 * half:
            raise ValueError("The measurements imply an implausibly large correction. Check the entries.")
    return Fit(field_map, residuals, before)


def grid_points(span: float, count: int) -> list[Point]:
    """Row-major grid, top row first, left to right — the order an operator reads it."""
    if count < 2 or not math.isfinite(span) or span <= 0:
        raise ValueError("Choose a positive span and at least a 2 x 2 grid")
    step = span / (count - 1)
    return [(-span / 2 + col * step, span / 2 - row * step) for row in range(count) for col in range(count)]


def points_from_spans(span: float, widths: tuple[float, float, float], heights: tuple[float, float, float],
                      diagonals: tuple[float, float]) -> list[Point]:
    """Turn caliper spans of a 3 x 3 grid into point positions.

    ``widths`` are the left-to-right spans of the top, middle and bottom rows,
    ``heights`` the bottom-to-top spans of the left, middle and right columns,
    ``diagonals`` bottom-left→top-right then top-left→bottom-right. The grid is
    assumed centred; the diagonal difference gives the shear.
    """
    values = (*widths, *heights, *diagonals)
    if not all(math.isfinite(v) and v > 0 for v in values):
        raise ValueError("Enter every measured span as a positive number")
    if any(abs(v / span - 1) > 0.25 for v in (*widths, *heights)):
        raise ValueError("A measured span differs from the nominal by more than 25 %. Check the entries.")
    wt, wm, wb = widths
    hl, hm, hr = heights
    d1, d2 = diagonals
    shear = (d1 * d1 - d2 * d2) / (4 * wm * hm)
    rows = ((wt, +1), (wm, 0), (wb, -1))
    cols = ((hl, -1), (hm, 0), (hr, +1))
    points = []
    for width, row in rows:
        for height, col in cols:
            y = row * height / 2
            points.append((col * width / 2 + shear * y, y))
    return points
