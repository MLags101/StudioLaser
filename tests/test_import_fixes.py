import math
import struct

import pytest
from shapely import affinity
from shapely.geometry import GeometryCollection, Point, Polygon, box

from mopa_luiz.importers import import_geometry, parse_dxf
from mopa_luiz.pcb.board import ROLES, BoardSide, InversionMode, detect_role, prepare_board, read_drills
from mopa_luiz.pcb.gerber_parser import detect_layer_type, read_outline
from mopa_luiz.svg_artwork import import_svg

HEADER = "%FSLAX36Y36*%\n%MOMM*%\n%LPD*%\n"


def near(points, expected, tolerance=1e-6):
    return len(points) == len(expected) and all(math.dist(a, b) <= tolerance for a, b in zip(points, expected))


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def rectangle(path, x0, y0, x1, y1):
    um = lambda value: round(value * 1_000_000)
    points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    moves = "".join(f"X{um(x)}Y{um(y)}D0{1 if i else 2}*\n" for i, (x, y) in enumerate(points))
    return write(path, HEADER + "%ADD10C,0.100*%\nD10*\n" + moves + "M02*\n")


def flashes(path, aperture, points, attributes=""):
    body = "".join(f"X{round(x * 1e6)}Y{round(y * 1e6)}D03*\n" for x, y in points)
    return write(path, HEADER + attributes + f"%ADD10{aperture}*%\nD10*\n" + body + "M02*\n")


# 1. Rounded-corner outlines -------------------------------------------------

ROUNDED = HEADER + """%ADD10C,0.100*%
G75*
D10*
G01*
X12000000Y10000000D02*
X28000000Y10000000D01*
G03*
X30000000Y12000000I0J2000000D01*
G01*
X30000000Y18000000D01*
G03*
X28000000Y20000000I-2000000J0D01*
G01*
X12000000Y20000000D01*
G03*
X10000000Y18000000I0J-2000000D01*
G01*
X10000000Y12000000D01*
G03*
X12000000Y10000000I2000000J0D01*
M02*
"""


def test_rounded_corner_outline_is_one_closed_polygon(tmp_path):
    outline = read_outline(write(tmp_path / "board-Edge_Cuts.gbr", ROUNDED))
    assert isinstance(outline, Polygon) and not outline.interiors
    assert outline.area == pytest.approx(20 * 10 - (4 - math.pi) * 2 ** 2, abs=0.01)
    assert outline.bounds == pytest.approx((10, 10, 30, 20), abs=1e-3)


def test_outline_endpoints_within_a_micron_are_joined_but_gaps_are_not(tmp_path):
    noisy = ROUNDED.replace("X12000000Y10000000D02*", "X12000000Y10000001D02*")
    assert read_outline(write(tmp_path / "noisy.gbr", noisy)).area == pytest.approx(196.566, abs=0.01)
    gap = ROUNDED.replace("X12000000Y10000000D02*", "X12100000Y10000000D02*")
    with pytest.raises(ValueError, match="open or intersecting"):
        read_outline(write(tmp_path / "open.gbr", gap))
    bowtie = HEADER + """%ADD10C,0.100*%
D10*
X0Y0D02*
X10000000Y10000000D01*
X10000000Y0D01*
X0Y10000000D01*
X0Y0D01*
M02*
"""
    with pytest.raises(ValueError):
        read_outline(write(tmp_path / "bowtie.gbr", bowtie))


# 2. Excellon without a format header ----------------------------------------

def test_headerless_excellon_format_is_chosen_from_the_outline(tmp_path):
    # Inch 2.4 with no unit or format statement: holes at (1.0, 0.5) and (1.5, 0.5) inch.
    inch = write(tmp_path / "inch.drl", "M48\nT01C0.0400\n%\nT01\nX010000Y005000\nX015000Y005000\nM30\n")
    cutouts, centers = read_drills([inch], (20, 5, 45, 20))
    assert near(centers, [(25.4, 12.7), (38.1, 12.7)])
    assert cutouts.area == pytest.approx(2 * math.pi * 0.508 ** 2, rel=0.02)
    # The same digits on a board where only the metric 3.3 reading fits.
    assert near(read_drills([inch], (8, 3, 16, 7))[1], [(10, 5), (15, 5)])
    # Decimal coordinates with no unit statement.
    decimal = write(tmp_path / "decimal.drl", "M48\nT01C1.0\n%\nT01\nX14.0Y24.0\nM30\n")
    assert read_drills([decimal], (10, 20, 30, 40))[1] == ((14.0, 24.0),)


def test_headerless_excellon_that_cannot_be_resolved_is_refused(tmp_path):
    inch = write(tmp_path / "inch.drl", "M48\nT01C0.0400\n%\nT01\nX010000Y005000\nX015000Y005000\nM30\n")
    for bounds in (None, (100, 100, 120, 120), (0, 0, 50, 30)):  # no outline, off the board, ambiguous
        with pytest.raises(ValueError, match=r"inch\.drl.*drill format could not be determined"):
            read_drills([inch], bounds)


def test_prepare_board_reads_headerless_drill_and_keeps_g85_slots(tmp_path):
    outline = rectangle(tmp_path / "board.gko", 20, 5, 45, 20)
    copper = flashes(tmp_path / "board.gtl", "R,4.0X4.0", [(30, 12)])
    drill = write(tmp_path / "board.drl", "M48\nT01C0.0400\n%\nT01\nX010000Y005000\nX015000Y005000\nM30\n")
    (side,) = prepare_board({"outline": [outline], "top_copper": [copper], "drill": [drill]},
                            mode=InversionMode.FULL_INVERT)
    assert near(side.references, [(25.4, 12.7), (38.1, 12.7)])
    assert not side.boundary.covers(Point(25.4, 12.7))
    slot = write(tmp_path / "slot.drl", "M48\nMETRIC\nG90\nT01C1.0\n%\nT01\nX17.0Y25.0G85X19.0Y25.0\nM30\n")
    assert read_drills([slot])[0].covers(Point(18, 25))


# 3. Role guessing -----------------------------------------------------------

@pytest.mark.parametrize("name, role", [
    ("board-F_Cu.gbr", "top_copper"), ("board-B_Cu.gbr", "bottom_copper"), ("board-F.Cu.gbr", "top_copper"),
    ("board-Edge_Cuts.gbr", "outline"), ("board-Edge.Cuts.gm1", "outline"),
    ("board-F_Mask.gbr", "top_mask"), ("board-B_Mask.gbr", "bottom_mask"),
    ("board-F_SilkS.gbr", "top_silk"), ("board-F_Silkscreen.gbr", "top_silk"),
    ("board-B_SilkS.gbr", "bottom_silk"), ("board-B_Silkscreen.gbr", "bottom_silk"),
    ("x.GTL", "top_copper"), ("x.gbl", "bottom_copper"), ("x.gko", "outline"), ("x.gm1", "outline"),
    ("x.gml", "outline"), ("x.gts", "top_mask"), ("x.gbs", "bottom_mask"), ("x.gto", "top_silk"),
    ("x.gbo", "bottom_silk"),
    ("x.cmp", "top_copper"), ("x.sol", "bottom_copper"), ("x.stc", "top_mask"), ("x.sts", "bottom_mask"),
    ("x.plc", "top_silk"), ("x.pls", "bottom_silk"), ("x.top", "top_copper"), ("x.bot", "bottom_copper"),
    ("circuit_doc.gbr", "other"), ("cutout_notes.gbr", "other"), ("layer10.gbr", "other"),
    ("top_notes.gbr", "other"), ("bottom_fab.gbr", "other"), ("stopmask.gbr", "other"),
])
def test_file_names_map_to_anchored_roles(name, role):
    layer_type, is_bottom, is_outline = detect_layer_type(f"C:/jobs/{name}")
    assert layer_type == role
    assert is_bottom == role.startswith("bottom_") and is_outline == (role == "outline")


def test_roles_and_x2_attributes_take_priority(tmp_path):
    assert ROLES == ("top_copper", "bottom_copper", "top_mask", "bottom_mask", "top_silk", "bottom_silk",
                     "outline", "drill", "other")
    cases = {"Soldermask,Bot": "bottom_mask", "Soldermask,Top": "top_mask", "Legend,Top": "top_silk",
             "Legend,Bot": "bottom_silk", "Copper,L1,Top": "top_copper", "Profile,NP": "outline",
             "Paste,Top": "other"}
    for index, (function, role) in enumerate(cases.items()):
        # The file name says top copper; the X2 attribute wins.
        path = flashes(tmp_path / f"{index}-F_Cu.gbr", "C,1.0", [(1, 1)], f"%TF.FileFunction,{function}*%\n")
        assert detect_role(path) == role
    assert detect_role(flashes(tmp_path / "circuit_doc.gbr", "C,1.0", [(1, 1)])) == "other"
    assert detect_role(flashes(tmp_path / "pcb-B_Mask.gbr", "C,1.0", [(1, 1)])) == "bottom_mask"


# 4. prepare_board: mask, silk, kerf -----------------------------------------

def board_files(tmp_path):
    return {
        "outline": [rectangle(tmp_path / "b-Edge_Cuts.gbr", 0, 0, 20, 10)],
        "top_copper": [flashes(tmp_path / "b-F_Cu.gbr", "R,2.0X2.0", [(4, 5), (6.3, 5)])],  # 0.3 mm gap
        "top_mask": [flashes(tmp_path / "b-F_Mask.gbr", "R,2.2X2.2", [(4, 5)])],
        "bottom_mask": [flashes(tmp_path / "b-B_Mask.gbr", "R,3.0X3.0", [(4, 5), (19.5, 5)])],
        "bottom_silk": [flashes(tmp_path / "b-B_SilkS.gbr", "R,1.0X1.0", [(2, 2)])],
    }


def test_board_side_fields_and_mask_silk_layers(tmp_path):
    assert list(BoardSide.__dataclass_fields__) == [
        "side", "outline", "boundary", "retained", "removal", "mask_openings", "silkscreen", "references", "origin", "flip",
        "inferred", "drawn_copper"]
    front, back = prepare_board(board_files(tmp_path), mode=InversionMode.ISOLATION)
    assert (front.side, back.side) == ("top", "bottom")
    assert front.mask_openings.symmetric_difference(box(2.9, 3.9, 5.1, 6.1)).area < 1e-9
    assert isinstance(front.silkscreen, GeometryCollection) and front.silkscreen.is_empty
    # The bottom side has no copper: it is still produced, mirrored about the board centre.
    for empty in (back.retained, back.removal):
        assert isinstance(empty, GeometryCollection) and empty.is_empty
    openings = box(2.5, 3.5, 5.5, 6.5).union(box(18, 3.5, 20, 6.5))  # second flash clipped by the board edge
    assert back.mask_openings.symmetric_difference(affinity.scale(openings, -1, 1, origin=(10, 5))).area < 1e-9
    assert back.silkscreen.symmetric_difference(box(17.5, 1.5, 18.5, 2.5)).area < 1e-9
    flipped = prepare_board(board_files(tmp_path), mode=InversionMode.ISOLATION, flip_axis="horizontal")[1]
    assert flipped.silkscreen.symmetric_difference(box(1.5, 7.5, 2.5, 8.5)).area < 1e-9


def test_layer_selection_errors(tmp_path):
    files = board_files(tmp_path)
    with pytest.raises(ValueError, match="copper, mask or silkscreen"):
        prepare_board({"outline": files["outline"]}, mode=InversionMode.ISOLATION)
    with pytest.raises(ValueError, match="one top mask file"):
        prepare_board({**files, "top_mask": files["top_mask"] * 2}, mode=InversionMode.ISOLATION)
    for kerf in (-0.1, math.inf, math.nan):
        with pytest.raises(ValueError, match="Kerf"):
            prepare_board(files, mode=InversionMode.ISOLATION, kerf_mm=kerf)


@pytest.mark.parametrize("mode", [InversionMode.ISOLATION, InversionMode.FULL_INVERT])
def test_kerf_keeps_the_removal_half_a_spot_from_retained_copper(tmp_path, mode):
    files = board_files(tmp_path)
    plain = prepare_board(files, mode=mode, isolation_width=0.3)[0]
    assert plain.removal.distance(plain.retained) < 1e-9
    side = prepare_board(files, mode=mode, isolation_width=0.3, kerf_mm=0.1)[0]
    assert side.retained.equals(plain.retained)
    assert not side.removal.is_empty and side.removal.distance(side.retained) >= 0.05 - 1e-3
    assert side.removal.difference(plain.removal).area < 1e-9
    assert side.removal.covers(Point(5.15, 5))  # the 0.3 mm channel between the pads survives, 0.2 mm wide
    assert not side.removal.covers(Point(5.04, 5))
    assert side.mask_openings.symmetric_difference(box(2.95, 3.95, 5.05, 6.05)).area < 1e-9
    # A channel narrower than the kerf vanishes instead of failing.
    wide = prepare_board(files, mode=InversionMode.ISOLATION, isolation_width=0.05, kerf_mm=0.2)[0]
    assert wide.removal.is_empty


# 5. SVG artwork -------------------------------------------------------------

def svg(tmp_path, body):
    return write(tmp_path / "art.svg",
                 f'<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="20mm" viewBox="0 0 40 20">{body}</svg>')


def test_svg_style_element_classes_ids_and_elements(tmp_path):
    result = import_svg(svg(tmp_path, """
        <defs><style type="text/css"><![CDATA[
          .st0{fill:#231F20;}
          #frame { fill: none; stroke: #000 }
          circle { fill: none }
          .gone { display: none }
          .unseen { visibility: hidden }
        ]]></style></defs>
        <rect class="st0" x="0" y="0" width="10" height="10"/>
        <rect id="frame" x="20" y="0" width="10" height="10"/>
        <circle cx="35" cy="15" r="2"/>
        <rect class="gone" x="0" y="12" width="5" height="5"/>
        <rect class="unseen" x="6" y="12" width="5" height="5"/>
    """))
    assert len(result.vectors) == 3
    assert len(result.fills) == 1
    assert Polygon(result.fills[0]).symmetric_difference(box(-20, 0, -10, 10)).area < 1e-3


def test_svg_display_none_on_shapes_and_ancestors(tmp_path):
    result = import_svg(svg(tmp_path, """
        <rect x="0" y="0" width="4" height="4"/>
        <rect display="none" x="10" y="0" width="4" height="4"/>
        <rect style="display:none" x="10" y="5" width="4" height="4"/>
        <g display="none"><g><rect x="20" y="0" width="4" height="4"/></g></g>
        <g style="display: none"><rect x="20" y="5" width="4" height="4"/></g>
        <g visibility="hidden"><rect x="30" y="0" width="4" height="4"/></g>
    """))
    assert len(result.vectors) == len(result.fills) == 1
    assert Polygon(result.fills[0]).symmetric_difference(box(-20, 6, -16, 10)).area < 1e-3


@pytest.mark.parametrize("white", ["#fff", "#FFFFFF", "white", "rgb(255,255,255)", "rgb(100%, 100%, 100%)"])
def test_svg_white_fill_knocks_out_earlier_fills(tmp_path, white):
    result = import_svg(svg(tmp_path, f"""
        <rect fill="{white}" x="0" y="0" width="40" height="20"/>
        <rect fill="#000" x="10" y="5" width="10" height="10"/>
        <rect fill="{white}" x="12" y="7" width="6" height="6"/>
        <rect style="fill:black" x="14" y="9" width="2" height="2"/>
    """))
    assert len(result.vectors) == 4
    expected = box(-10, -5, 0, 5).difference(box(-8, -3, -2, 3)).union(box(-6, -1, -4, 1))
    assert len(result.fills) == 3
    rings = [Polygon(ring) for ring in result.fills]
    assert sorted(ring.area for ring in rings) == pytest.approx([4, 36, 100], rel=1e-5)
    assert rings[0].symmetric_difference(rings[1]).symmetric_difference(rings[2]).symmetric_difference(expected).area < 1e-3


# 6. STL scale and DXF blocks ------------------------------------------------

def test_stl_keeps_millimetre_scale_and_is_centred():
    ascii_stl = """solid part
      facet normal 0 0 1
        outer loop
          vertex 10 10 0
          vertex 30 10 0
          vertex 30 20 0
        endloop
      endfacet
    endsolid part
    """
    result = import_geometry("part.stl", ascii_stl)
    assert result["polylines"] == [[(-10, -5), (10, -5), (10, 5), (-10, -5)]]
    assert result["source_bounds"]["applied_scale_to_mm"] == 1.0
    binary = bytes(80) + struct.pack("<I", 1) + struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 120, 0, 0, 0, 90, 0, 0)
    from base64 import b64encode
    result = import_geometry("big.STL", "data:model/stl;base64," + b64encode(binary).decode())
    xs = [x for x, _ in result["polylines"][0]]
    ys = [y for _, y in result["polylines"][0]]
    assert (min(xs), max(xs), min(ys), max(ys)) == (-60, 60, -45, 45)


def dxf_line(x1, x2):
    return f"0\nLINE\n10\n{x1}\n20\n0\n11\n{x2}\n21\n0\n"


def test_dxf_block_definitions_are_not_model_geometry():
    dxf = ("0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n0\nENDSEC\n"
           "0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n2\nARROW\n" + dxf_line(100, 200) + "0\nCIRCLE\n10\n0\n20\n0\n40\n50\n"
           "0\nENDBLK\n0\nENDSEC\n"
           "0\nSECTION\n2\nENTITIES\n" + dxf_line(0, 10) + "0\nENDSEC\n0\nEOF\n")
    assert parse_dxf(dxf) == [[(0.0, 0.0), (10.0, 0.0)]]
    assert import_geometry("part.dxf", dxf)["polylines"] == [[(-5.0, 0.0), (5.0, 0.0)]]
    # Fragments with no section markers still parse.
    assert parse_dxf(dxf_line(0, 10) + dxf_line(10, 20)) == [[(0.0, 0.0), (10.0, 0.0)], [(10.0, 0.0), (20.0, 0.0)]]
