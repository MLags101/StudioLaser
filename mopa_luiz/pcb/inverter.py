"""Inverter module for creating laser-engraveable inverted PCB geometries."""

import math
from enum import Enum
from typing import Optional, Tuple, Union
from shapely.geometry import box, Polygon, MultiPolygon
from shapely.ops import unary_union
from shapely.validation import make_valid


class InversionMode(str, Enum):
    FULL_INVERT = "full_invert"       # FlatCAM style: removes all non-copper in bounding box
    ISOLATION = "isolation"           # Fast laser mode: engraves only isolation channels
    POSITIVE = "positive"             # Direct copper geometry


class BoundaryMode(str, Enum):
    MARGIN = "margin"                 # Bounding box + margin
    OUTLINE = "outline"               # Use Edge_Cuts outline layer
    CUSTOM_SIZE = "custom_size"       # Explicit width x height


def extract_polygons(geom) -> Union[Polygon, MultiPolygon]:
    """
    Ensure only valid 2D Polygons or MultiPolygons are returned,
    filtering out any residual 0D Points, 1D LineStrings, or GeometryCollections
    produced by GEOS difference/intersection operations.
    """
    if geom is None or geom.is_empty:
        return Polygon()

    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        return geom

    # If GeometryCollection, collect all valid Polygons and MultiPolygons
    if hasattr(geom, 'geoms'):
        polys = []
        for g in geom.geoms:
            if isinstance(g, Polygon) and not g.is_empty:
                polys.append(g)
            elif isinstance(g, MultiPolygon) and not g.is_empty:
                polys.extend([p for p in g.geoms if not p.is_empty])

        if not polys:
            return Polygon()
        if len(polys) == 1:
            return polys[0]
        try:
            return unary_union(polys)
        except Exception:
            return MultiPolygon(polys)

    return Polygon()


def safe_bounds(geom, default: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)) -> Tuple[float, float, float, float]:
    """Return bounds tuple ensuring no NaN values."""
    if geom is None or geom.is_empty:
        return default
    try:
        b = geom.bounds
        if len(b) != 4 or any(math.isnan(x) for x in b):
            return default
        return (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    except Exception:
        return default


def invert_layer(
    copper_geometry: Union[Polygon, MultiPolygon],
    mode: InversionMode = InversionMode.FULL_INVERT,
    boundary_mode: BoundaryMode = BoundaryMode.MARGIN,
    margin_mm: float = 1.0,
    isolation_width_mm: float = 0.25,
    custom_width_mm: Optional[float] = None,
    custom_height_mm: Optional[float] = None,
    outline_geometry: Optional[Union[Polygon, MultiPolygon]] = None,
) -> Union[Polygon, MultiPolygon]:
    """
    Perform 2D geometric inversion or isolation.
    Guaranteed to return a clean Polygon or MultiPolygon with valid geometry.
    """
    if copper_geometry is None:
        return Polygon()

    copper_geometry = extract_polygons(copper_geometry)
    if copper_geometry.is_empty and boundary_mode != BoundaryMode.OUTLINE:
        return Polygon()

    if not copper_geometry.is_valid:
        try:
            copper_geometry = extract_polygons(make_valid(copper_geometry))
        except Exception:
            copper_geometry = copper_geometry.buffer(0)

    bounds = safe_bounds(copper_geometry)
    xmin, ymin, xmax, ymax = bounds
    if boundary_mode != BoundaryMode.OUTLINE and math.isclose(xmin, xmax) and math.isclose(ymin, ymax):
        return Polygon()

    # Construct the boundary polygon
    if boundary_mode == BoundaryMode.OUTLINE:
        boundary = extract_polygons(outline_geometry)
        if boundary.is_empty:
            raise ValueError("outline mode requires a closed board outline")
    elif boundary_mode == BoundaryMode.CUSTOM_SIZE and custom_width_mm and custom_height_mm:
        center_x = (xmin + xmax) / 2.0
        center_y = (ymin + ymax) / 2.0
        half_w = max(0.001, custom_width_mm / 2.0)
        half_h = max(0.001, custom_height_mm / 2.0)
        boundary = box(center_x - half_w, center_y - half_h,
                       center_x + half_w, center_y + half_h)
    else:
        # Default MARGIN mode: bounding box + margin
        m = max(0.0, margin_mm)
        # Use tiny epsilon (1e-6 mm = 1 nanometer) if m == 0.0 to prevent GEOS collinear boundary singularities
        eff_m = 1e-6 if m == 0.0 else m
        boundary = box(xmin - eff_m, ymin - eff_m, xmax + eff_m, ymax + eff_m)

    # Process based on selected mode
    try:
        if mode == InversionMode.FULL_INVERT:
            result = boundary.difference(copper_geometry)

        elif mode == InversionMode.ISOLATION:
            iso_width = max(0.01, isolation_width_mm)
            expanded = copper_geometry.buffer(iso_width, quad_segs=8, join_style='round')
            isolation_channels = expanded.difference(copper_geometry)
            result = isolation_channels.intersection(boundary)

        elif mode == InversionMode.POSITIVE:
            result = copper_geometry.intersection(boundary)

        else:
            result = boundary.difference(copper_geometry)

    except Exception as exc:
        raise ValueError(f"could not calculate {mode.value} geometry: {exc}") from exc

    # Ensure result only contains 2D polygonal geometry
    result = extract_polygons(result)

    if not result.is_valid:
        try:
            result = extract_polygons(make_valid(result))
        except Exception:
            result = result.buffer(0)

    return result
