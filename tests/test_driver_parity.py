"""The factory .cor table applied in software, and where the hardware log goes."""

from base64 import b64encode
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import struct

import pytest

from mopa_luiz.correction import GRID, CorTable
from mopa_luiz.hardware_log import HardwareLog, log_file, recent_lines
from tests.test_calibration_and_model import b6_profile

REPO = Path(__file__).resolve().parents[1]
FACTORY = REPO / "jcz15.cor"


def synthetic_cor(dx, dy) -> bytes:
    """A JCZ_COR_2_1-style file: 36-byte header (scale at 28), 65 x 65 int32 pairs, short trailer."""
    header = bytearray(36)
    header[:11] = b"JCZ_COR_2_1"
    header[28:36] = struct.pack("<d", 391.25)
    body = b"".join(struct.pack("<2i", dx(j, k), dy(j, k)) for j in range(GRID) for k in range(GRID))
    return bytes(header) + body + bytes(4)


def test_table_offsets_interpolate_between_nodes_along_the_right_axes():
    # dx depends on the inner index (galvo X), dy on the outer index (galvo Y).
    table = CorTable(synthetic_cor(lambda j, k: 10 * k, lambda j, k: -7 * j))
    assert table.offset(0, 0) == (0, 0)
    assert table.offset(1024, 0) == pytest.approx((10, 0))
    assert table.offset(1536, 2048) == pytest.approx((15, -14))       # halfway between nodes in X
    assert table.offset(65535, 65535)[0] == pytest.approx(640, abs=0.01)
    reverse = CorTable(synthetic_cor(lambda j, k: 10 * k, lambda j, k: -7 * j), sign=-1)
    assert reverse.offset(1536, 2048) == pytest.approx((-15, 14))


@pytest.mark.skipif(not FACTORY.is_file(), reason="factory .cor not in the repository")
def test_factory_table_is_centred_and_pushes_the_corners_outward():
    table = CorTable(FACTORY.read_bytes())
    assert table.offset(32768, 32768) == pytest.approx((0, 0), abs=1)
    left, right = table.offset(0, 32768)[0], table.offset(65535, 32768)[0]
    low, high = table.offset(32768, 0)[1], table.offset(32768, 65535)[1]
    assert left < 0 < right and low < 0 < high


@pytest.mark.skipif(not FACTORY.is_file(), reason="factory .cor not in the repository")
def test_cor_base_moves_points_by_the_table_and_marks_lines_curved():
    data = FACTORY.read_bytes()
    plain = replace(b6_profile(), base="none", correction=b64encode(data).decode())
    corrected = replace(plain, base="cor")
    assert not plain.curved and corrected.curved
    assert corrected.galvo_point((0, 0)) == pytest.approx(plain.galvo_point((0, 0)), abs=1)
    corner = (60, 60)
    gx, gy = plain.galvo_point(corner)
    dx, dy = CorTable(data).offset(gx, gy)
    assert corrected.galvo_point(corner) == pytest.approx((gx + dx, gy + dy), abs=1)
    reversed_ = replace(corrected, cor_sign=-1)
    assert reversed_.galvo_point(corner) == pytest.approx((gx - dx, gy - dy), abs=1)


def test_log_goes_to_the_log_folder_and_old_days_are_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIOLASER_LOG_DIR", str(tmp_path))
    old = tmp_path / f"hardware-{(date.today() - timedelta(days=30)).isoformat()}.log"
    old.write_text("old\n", encoding="utf-8")
    log = HardwareLog()
    log("=== frame-loop on 1:1\nusb: Connected")
    log.close()
    assert not old.exists()
    text = log_file().read_text(encoding="utf-8")
    assert "=== frame-loop on 1:1" in text and "usb: Connected" in text
    assert any("usb: Connected" in line for line in recent_lines(10))
    HardwareLog(enabled=False)("nothing")       # a disabled log writes nothing and never fails
