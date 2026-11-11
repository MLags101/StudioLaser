from __future__ import annotations

import base64
import math
import re
import struct
import xml.etree.ElementTree as ET
from typing import Any

Polyline = list[tuple[float, float]]


def import_geometry(filename: str, data_url_or_text: str) -> dict[str, Any]:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    raw = decode_payload(data_url_or_text)
    if suffix == "svg":
        polylines = parse_svg(raw.decode("utf-8", errors="ignore"))
        flip_y = True
        preserve_units = False
        unit_scale = 1.0
        unit_name = "svg_units"
    elif suffix == "dxf":
        text = raw.decode("utf-8", errors="ignore")
        unit_scale, unit_name = dxf_units_to_mm(text)
        polylines = parse_dxf(text)
        flip_y = False
        preserve_units = True
    elif suffix == "stl":
        polylines = parse_stl(raw)
        flip_y = False
        preserve_units = True
        unit_scale = 1.0
        unit_name = "millimeters"
    else:
        raise ValueError("supported imports: .svg, .dxf, .stl")
    polylines = [poly for poly in polylines if len(poly) >= 2]
    if not polylines:
        raise ValueError(f"no usable geometry found in {filename}")
    if preserve_units:
        normalized, bounds = preserve_units_to_workspace(polylines, unit_scale_to_mm=unit_scale, unit_name=unit_name, flip_y=flip_y)
    else:
        normalized, bounds = normalize_to_workspace(polylines, flip_y=flip_y)
    return {
        "name": filename,
        "polylines": normalized,
        "source_bounds": bounds,
        "polyline_count": len(normalized),
        "point_count": sum(len(poly) for poly in normalized),
    }


def decode_payload(payload: str) -> bytes:
    if payload.startswith("data:"):
        _, encoded = payload.split(",", 1)
        return base64.b64decode(encoded)
    return payload.encode("utf-8")


def normalize_to_workspace(
    polylines: list[Polyline],
    target_mm: float = 60.0,
    flip_y: bool = True,
) -> tuple[list[Polyline], dict[str, float]]:
    points = [pt for poly in polylines for pt in poly]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max(max_x - min_x, 1e-9)
    height = max(max_y - min_y, 1e-9)
    scale = target_mm / max(width, height)
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    y_sign = -1.0 if flip_y else 1.0
    normalized = [[((x - cx) * scale, (y - cy) * scale * y_sign) for x, y in poly] for poly in polylines]
    return normalized, {
        "min_x": min_x,
        "max_x": max_x,
        "min_y": min_y,
        "max_y": max_y,
        "width": width,
        "height": height,
        "applied_scale_to_mm": scale,
        "flip_y": flip_y,
    }


def preserve_units_to_workspace(
    polylines: list[Polyline],
    unit_scale_to_mm: float,
    unit_name: str,
    flip_y: bool = False,
) -> tuple[list[Polyline], dict[str, float | str | bool]]:
    points = [pt for poly in polylines for pt in poly]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max(max_x - min_x, 1e-9)
    height = max(max_y - min_y, 1e-9)
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    y_sign = -1.0 if flip_y else 1.0
    normalized = [
        [((x - cx) * unit_scale_to_mm, (y - cy) * unit_scale_to_mm * y_sign) for x, y in poly]
        for poly in polylines
    ]
    return normalized, {
        "min_x": min_x,
        "max_x": max_x,
        "min_y": min_y,
        "max_y": max_y,
        "width": width,
        "height": height,
        "unit_name": unit_name,
        "unit_scale_to_mm": unit_scale_to_mm,
        "applied_scale_to_mm": unit_scale_to_mm,
        "flip_y": flip_y,
        "preserved_source_units": True,
    }


def dxf_units_to_mm(text: str) -> tuple[float, str]:
    unit_by_code = {
        0: (1.0, "unitless_assumed_mm"),
        1: (25.4, "inches"),
        2: (304.8, "feet"),
        3: (1609344.0, "miles"),
        4: (1.0, "millimeters"),
        5: (10.0, "centimeters"),
        6: (1000.0, "meters"),
        7: (1000000.0, "kilometers"),
        8: (0.0000254, "microinches"),
        9: (0.0254, "mils"),
        10: (914.4, "yards"),
        11: (1e-7, "angstroms"),
        12: (1e-6, "nanometers"),
        13: (0.001, "microns"),
        14: (100.0, "decimeters"),
        15: (10000.0, "decameters"),
        16: (100000.0, "hectometers"),
        17: (1000000000.0, "gigameters"),
        18: (149597870700000.0, "astronomical_units"),
        19: (9.4607304725808e18, "light_years"),
        20: (3.0856775814913673e19, "parsecs"),
    }
    pairs = dxf_pairs(text)
    for index, (code, value) in enumerate(pairs[:-1]):
        if code == "9" and value.upper() == "$INSUNITS":
            next_code, next_value = pairs[index + 1]
            if next_code == "70":
                return unit_by_code.get(int(float(next_value)), unit_by_code[0])
    return unit_by_code[0]


def parse_svg(text: str) -> list[Polyline]:
    root = ET.fromstring(text)
    polylines: list[Polyline] = []
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1].lower()
        if tag == "line":
            polylines.append([
                (svg_num(elem.get("x1")), svg_num(elem.get("y1"))),
                (svg_num(elem.get("x2")), svg_num(elem.get("y2"))),
            ])
        elif tag in ("polyline", "polygon"):
            points = parse_svg_points(elem.get("points", ""))
            if tag == "polygon" and points:
                points.append(points[0])
            polylines.append(points)
        elif tag == "rect":
            x = svg_num(elem.get("x"))
            y = svg_num(elem.get("y"))
            w = svg_num(elem.get("width"))
            h = svg_num(elem.get("height"))
            polylines.append([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)])
        elif tag == "circle":
            cx = svg_num(elem.get("cx"))
            cy = svg_num(elem.get("cy"))
            r = svg_num(elem.get("r"))
            polylines.append(circle_points(cx, cy, r))
        elif tag == "ellipse":
            cx = svg_num(elem.get("cx"))
            cy = svg_num(elem.get("cy"))
            rx = svg_num(elem.get("rx"))
            ry = svg_num(elem.get("ry"))
            polylines.append([(cx + math.cos(t) * rx, cy + math.sin(t) * ry) for t in linspace(0, math.tau, 73)])
        elif tag == "path":
            polylines.extend(parse_svg_path(elem.get("d", "")))
    return polylines


def svg_num(value: str | None) -> float:
    if not value:
        return 0.0
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group(0)) if match else 0.0


def parse_svg_points(value: str) -> Polyline:
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", value)]
    return list(zip(nums[0::2], nums[1::2]))


def parse_svg_path(d: str) -> list[Polyline]:
    tokens = re.findall(r"[MmLlHhVvZz]|-?\d+(?:\.\d+)?", d)
    polylines: list[Polyline] = []
    current: Polyline = []
    x = y = sx = sy = 0.0
    command = ""
    i = 0
    while i < len(tokens):
        if re.match(r"[A-Za-z]", tokens[i]):
            command = tokens[i]
            i += 1
        if command in ("M", "m", "L", "l"):
            while i + 1 < len(tokens) and not re.match(r"[A-Za-z]", tokens[i]):
                nx, ny = float(tokens[i]), float(tokens[i + 1])
                i += 2
                if command in ("m", "l"):
                    nx += x
                    ny += y
                if command in ("M", "m") and current:
                    polylines.append(current)
                    current = []
                x, y = nx, ny
                if command in ("M", "m"):
                    sx, sy = x, y
                    command = "L" if command == "M" else "l"
                current.append((x, y))
        elif command in ("H", "h", "V", "v"):
            while i < len(tokens) and not re.match(r"[A-Za-z]", tokens[i]):
                value = float(tokens[i])
                i += 1
                if command == "H":
                    x = value
                elif command == "h":
                    x += value
                elif command == "V":
                    y = value
                else:
                    y += value
                current.append((x, y))
        elif command in ("Z", "z"):
            current.append((sx, sy))
            i += 0
            command = ""
        else:
            i += 1
    if current:
        polylines.append(current)
    return polylines


def parse_dxf(text: str) -> list[Polyline]:
    pairs = dxf_entities(dxf_pairs(text))
    polylines: list[Polyline] = []
    i = 0
    while i < len(pairs):
        code, value = pairs[i]
        if code == "0" and value in {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE"}:
            entity = value
            fields: list[tuple[str, str]] = []
            i += 1
            while i < len(pairs) and pairs[i][0] != "0":
                fields.append(pairs[i])
                i += 1
            if entity == "LINE":
                vals = dxf_values(fields)
                polylines.append([(vals.get("10", 0), vals.get("20", 0)), (vals.get("11", 0), vals.get("21", 0))])
            elif entity == "LWPOLYLINE":
                polylines.append(parse_lwpolyline(fields))
            elif entity == "POLYLINE":
                pts, closed, i = parse_old_polyline(pairs, i, fields)
                if closed and pts:
                    pts.append(pts[0])
                polylines.append(pts)
            elif entity == "CIRCLE":
                vals = dxf_values(fields)
                polylines.append(circle_points(vals.get("10", 0), vals.get("20", 0), vals.get("40", 0)))
            elif entity == "ARC":
                vals = dxf_values(fields)
                polylines.append(arc_points(vals.get("10", 0), vals.get("20", 0), vals.get("40", 0), vals.get("50", 0), vals.get("51", 0)))
            elif entity == "ELLIPSE":
                polylines.append(ellipse_points(fields))
            elif entity == "SPLINE":
                pts = repeated_xy(fields, preferred_x_code="11", preferred_y_code="21") or repeated_xy(fields)
                if len(pts) >= 2:
                    polylines.append(pts)
            continue
        i += 1
    return polylines


def dxf_entities(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep only model geometry: block definitions (BLOCKS) are not drawn.

    Fragments with no ENTITIES section keep everything outside other sections.
    """
    kept: list[tuple[str, str]] = []
    outside: list[tuple[str, str]] = []
    section = None
    found = False
    for index, (code, value) in enumerate(pairs):
        if code == "0" and value == "SECTION":
            section = pairs[index + 1][1].upper() if index + 1 < len(pairs) and pairs[index + 1][0] == "2" else ""
            found = found or section == "ENTITIES"
        elif code == "0" and value == "ENDSEC":
            section = None
        elif section == "ENTITIES":
            kept.append((code, value))
        elif section is None:
            outside.append((code, value))
    return kept if found else outside


def dxf_pairs(text: str) -> list[tuple[str, str]]:
    """Return DXF group-code/value pairs, tolerating extra blank lines.

    DXF files are line-oriented, but exports vary in whitespace. We look for
    integer group-code lines and pair them with the following line as value.
    """
    raw_lines = [line.rstrip("\r\n") for line in text.splitlines()]
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(raw_lines):
        code = raw_lines[i].strip()
        if not re.fullmatch(r"-?\d+", code):
            i += 1
            continue
        value = raw_lines[i + 1].strip() if i + 1 < len(raw_lines) else ""
        pairs.append((code, value))
        i += 2
    return pairs


def parse_old_polyline(
    pairs: list[tuple[str, str]],
    index: int,
    header_fields: list[tuple[str, str]],
) -> tuple[Polyline, bool, int]:
    closed = any(code == "70" and int(float(val)) & 1 for code, val in header_fields)
    vertices: list[tuple[float, float, float]] = []
    i = index
    while i < len(pairs):
        code, value = pairs[i]
        if code == "0" and value == "SEQEND":
            return flatten_bulged_vertices(vertices, closed), False, i + 1
        if code == "0" and value == "VERTEX":
            fields: list[tuple[str, str]] = []
            i += 1
            while i < len(pairs) and pairs[i][0] != "0":
                fields.append(pairs[i])
                i += 1
            vals = dxf_values(fields)
            vertices.append((vals.get("10", 0.0), vals.get("20", 0.0), vals.get("42", 0.0)))
            continue
        if code == "0":
            return flatten_bulged_vertices(vertices, closed), False, i
        i += 1
    return flatten_bulged_vertices(vertices, closed), False, i


def parse_lwpolyline(fields: list[tuple[str, str]]) -> Polyline:
    vertices: list[tuple[float, float, float]] = []
    current: dict[str, float] = {}
    closed = any(code == "70" and int(float(val)) & 1 for code, val in fields)
    for code, value in fields:
        if code == "10":
            if "x" in current and "y" in current:
                vertices.append((current["x"], current["y"], current.get("bulge", 0.0)))
            current = {"x": float(value)}
        elif code == "20" and "x" in current:
            current["y"] = float(value)
        elif code == "42" and "x" in current:
            current["bulge"] = float(value)
    if "x" in current and "y" in current:
        vertices.append((current["x"], current["y"], current.get("bulge", 0.0)))
    return flatten_bulged_vertices(vertices, closed)


def flatten_bulged_vertices(vertices: list[tuple[float, float, float]], closed: bool) -> Polyline:
    if not vertices:
        return []
    points: Polyline = [(vertices[0][0], vertices[0][1])]
    last = len(vertices) if closed else len(vertices) - 1
    for i in range(last):
        x1, y1, bulge = vertices[i]
        x2, y2, _ = vertices[(i + 1) % len(vertices)]
        segment = bulge_segment_points((x1, y1), (x2, y2), bulge)
        points.extend(segment[1:])
    if closed and points and points[0] != points[-1]:
        points.append(points[0])
    return points


def bulge_segment_points(start: tuple[float, float], end: tuple[float, float], bulge: float) -> Polyline:
    if abs(bulge) < 1e-12:
        return [start, end]
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    chord = math.hypot(dx, dy)
    if chord < 1e-12:
        return [start, end]
    theta = 4.0 * math.atan(bulge)
    radius = chord * (1 + bulge * bulge) / (4 * abs(bulge))
    mid = ((x1 + x2) / 2, (y1 + y2) / 2)
    nx, ny = -dy / chord, dx / chord
    center_offset = chord * (1 - bulge * bulge) / (4 * bulge)
    cx = mid[0] + nx * center_offset
    cy = mid[1] + ny * center_offset
    start_angle = math.atan2(y1 - cy, x1 - cx)
    steps = max(6, min(96, int(abs(theta) / (math.pi / 24)) + 2))
    return [
        (cx + math.cos(start_angle + theta * t) * radius, cy + math.sin(start_angle + theta * t) * radius)
        for t in linspace(0.0, 1.0, steps)
    ]


def repeated_xy(
    fields: list[tuple[str, str]],
    preferred_x_code: str = "10",
    preferred_y_code: str = "20",
) -> Polyline:
    points: Polyline = []
    x = None
    for code, value in fields:
        if code == preferred_x_code:
            x = float(value)
        elif code == preferred_y_code and x is not None:
            points.append((x, float(value)))
            x = None
    return points


def dxf_values(fields: list[tuple[str, str]]) -> dict[str, float]:
    values: dict[str, float] = {}
    for code, value in fields:
        if code in {"10", "20", "11", "21", "40", "41", "42", "50", "51"}:
            values[code] = float(value)
    return values


def parse_stl(raw: bytes, max_triangles: int = 900) -> list[Polyline]:
    if raw[:80].lstrip().lower().startswith(b"solid"):
        points = [(float(a), float(b)) for a, b, _ in re.findall(rb"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", raw)]
        polylines = []
        for i in range(0, min(len(points), max_triangles * 3), 3):
            tri = points[i:i + 3]
            if len(tri) == 3:
                polylines.append([tri[0], tri[1], tri[2], tri[0]])
        return polylines
    if len(raw) < 84:
        return []
    count = min(struct.unpack_from("<I", raw, 80)[0], max_triangles)
    polylines = []
    offset = 84
    for _ in range(count):
        if offset + 50 > len(raw):
            break
        coords = struct.unpack_from("<12fH", raw, offset)
        tri = [(coords[3], coords[4]), (coords[6], coords[7]), (coords[9], coords[10])]
        polylines.append([tri[0], tri[1], tri[2], tri[0]])
        offset += 50
    return polylines


def circle_points(cx: float, cy: float, r: float) -> Polyline:
    return [(cx + math.cos(t) * r, cy + math.sin(t) * r) for t in linspace(0, math.tau, 73)]


def arc_points(cx: float, cy: float, r: float, start_deg: float, end_deg: float) -> Polyline:
    if end_deg < start_deg:
        end_deg += 360
    return [(cx + math.cos(math.radians(a)) * r, cy + math.sin(math.radians(a)) * r) for a in linspace(start_deg, end_deg, 48)]


def ellipse_points(fields: list[tuple[str, str]]) -> Polyline:
    vals = dxf_values(fields)
    cx = vals.get("10", 0.0)
    cy = vals.get("20", 0.0)
    major_x = vals.get("11", 0.0)
    major_y = vals.get("21", 0.0)
    ratio = vals.get("40", 1.0)
    start = vals.get("41", 0.0)
    end = vals.get("42", math.tau)
    if end <= start:
        end += math.tau
    minor_x = -major_y * ratio
    minor_y = major_x * ratio
    return [
        (
            cx + major_x * math.cos(t) + minor_x * math.sin(t),
            cy + major_y * math.cos(t) + minor_y * math.sin(t),
        )
        for t in linspace(start, end, 96)
    ]


def linspace(start: float, end: float, steps: int) -> list[float]:
    if steps <= 1:
        return [start]
    step = (end - start) / (steps - 1)
    return [start + i * step for i in range(steps)]
