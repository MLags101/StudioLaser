"""SVG Exporter module generating EZCAD-compatible vector files from 2D geometries."""

import os
import math
from typing import Optional, Union, List
from shapely.geometry import Polygon, MultiPolygon
from shapely.validation import make_valid
import shapely.affinity as affinity


def polygon_to_svg_path(geom: Union[Polygon, MultiPolygon]) -> str:
    """
    Convert a Shapely Polygon, MultiPolygon, or GeometryCollection into an SVG path data string
    with closed contours and holes formatted for evenodd filling.
    """
    if geom is None or geom.is_empty:
        return ""

    paths: List[str] = []
    polygons: List[Polygon] = []

    if isinstance(geom, Polygon):
        polygons = [geom]
    elif isinstance(geom, MultiPolygon):
        polygons = list(geom.geoms)
    elif hasattr(geom, 'geoms'):
        for g in geom.geoms:
            if isinstance(g, Polygon) and not g.is_empty:
                polygons.append(g)
            elif isinstance(g, MultiPolygon) and not g.is_empty:
                polygons.extend([p for p in g.geoms if not p.is_empty])

    for p in polygons:
        if p.is_empty or not hasattr(p, 'exterior') or p.exterior is None:
            continue

        try:
            # Exterior ring
            coords = list(p.exterior.coords)
            if len(coords) >= 3:
                cmd = [f"M {coords[0][0]:.4f},{coords[0][1]:.4f}"]
                for pt in coords[1:]:
                    cmd.append(f"L {pt[0]:.4f},{pt[1]:.4f}")
                cmd.append("Z")
                paths.append(" ".join(cmd))

            # Interior rings (holes)
            for interior in p.interiors:
                icoords = list(interior.coords)
                if len(icoords) >= 3:
                    cmd = [f"M {icoords[0][0]:.4f},{icoords[0][1]:.4f}"]
                    for pt in icoords[1:]:
                        cmd.append(f"L {pt[0]:.4f},{pt[1]:.4f}")
                    cmd.append("Z")
                    paths.append(" ".join(cmd))
        except Exception:
            continue

    return " ".join(paths)


def export_to_svg(
    geom: Union[Polygon, MultiPolygon],
    filepath: Optional[str] = None,
    include_reference_mark: bool = False,
    reference_mark_size_mm: float = 10.0,
    view_bounds: tuple[float, float, float, float] | None = None,
) -> str:
    """
    Export geometry to clean SVG formatted for EZCAD fiber laser marking software.
    """
    svg_empty = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="10mm" viewBox="0 0 10 10" version="1.1">\n'
        '</svg>\n'
    )

    if geom is None or (geom.is_empty and view_bounds is None):
        if filepath:
            os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(svg_empty)
        return svg_empty

    try:
        if not geom.is_valid:
            geom = make_valid(geom)
    except Exception:
        pass

    try:
        bounds = view_bounds if view_bounds is not None else geom.bounds
        if any(math.isnan(x) for x in bounds):
            return svg_empty
        xmin, ymin, xmax, ymax = bounds
    except Exception:
        return svg_empty

    width = max(0.001, xmax - xmin)
    height = max(0.001, ymax - ymin)

    try:
        # Affine transform to normalize to (0,0) and flip Y axis:
        norm_geom = affinity.affine_transform(geom, [1.0, 0.0, 0.0, -1.0, -xmin, ymax])
    except Exception:
        norm_geom = geom

    path_data = polygon_to_svg_path(norm_geom)

    ref_elements = ""
    total_w = width
    total_h = height

    if include_reference_mark and reference_mark_size_mm > 0:
        ref_elements = (
            f'\n  <!-- 10mm Calibration Square for EZCAD scale verification -->'
            f'\n  <rect x="0" y="0" width="{reference_mark_size_mm:.4f}" height="{reference_mark_size_mm:.4f}" '
            f'fill="none" stroke="red" stroke-width="0.05" />'
        )

    svg_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{total_w:.4f}mm" height="{total_h:.4f}mm" '
        f'viewBox="0 0 {total_w:.4f} {total_h:.4f}" version="1.1">\n'
        f'  <desc>Exported by Gerber2Laser for EZCAD</desc>\n'
        f'  <path d="{path_data}" fill="black" fill-rule="evenodd" stroke="none" />{ref_elements}\n'
        '</svg>\n'
    )

    if filepath:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(svg_content)

    return svg_content
