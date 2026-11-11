from __future__ import annotations

import io
import math
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ICONS = ASSETS / "icons"
MACOS_APP = ROOT / "StudioLaser.app"
MACOS_RESOURCES = MACOS_APP / "Contents" / "Resources"


def rounded(draw: ImageDraw.ImageDraw, box, radius, fill, outline=None, width=1) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def pts(points, scale: int) -> list[tuple[int, int]]:
    return [(int(x * scale), int(y * scale)) for x, y in points]


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def draw_desktop_icon(size: int = 1024) -> Image.Image:
    scale = 4
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def s(value: float) -> int:
        return int(round(value * scale))

    shadow = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    rounded(sd, (s(112), s(112), s(912), s(912)), s(132), (0, 0, 0, 170))
    shadow = shadow.filter(ImageFilter.GaussianBlur(s(34)))
    image.alpha_composite(shadow)

    rounded(draw, (s(96), s(76), s(928), s(928)), s(130), (17, 22, 29, 255))
    rounded(draw, (s(124), s(104), s(900), s(900)), s(106), (13, 18, 24, 255), (57, 70, 83, 255), s(5))

    draw.line(pts([(238, 745), (780, 745), (856, 806), (170, 806), (238, 745)], scale), fill=(246, 248, 245, 255), width=s(28), joint="curve")
    draw.line(pts([(238, 745), (780, 745), (856, 806), (170, 806)], scale), fill=(15, 21, 27, 255), width=s(10), joint="curve")

    draw.arc((s(150), s(320), s(360), s(610)), start=84, end=270, fill=(246, 248, 245, 255), width=s(22))
    draw.line((s(310), s(398), s(310), s(742)), fill=(246, 248, 245, 255), width=s(18))
    draw.line((s(354), s(398), s(354), s(742)), fill=(246, 248, 245, 255), width=s(18))

    rounded(draw, (s(310), s(276), s(638), s(520)), s(34), (246, 248, 245, 255))
    rounded(draw, (s(342), s(306), s(604), s(484)), s(16), (20, 26, 34, 255))
    draw.polygon(pts([(310, 312), (250, 350), (250, 495), (310, 520)], scale), fill=(246, 248, 245, 255))
    draw.line(pts([(310, 312), (250, 350), (250, 495), (310, 520)], scale), fill=(20, 26, 34, 255), width=s(10))

    rounded(draw, (s(624), s(350), s(784), s(498)), s(26), (246, 248, 245, 255))
    rounded(draw, (s(648), s(376), s(760), s(474)), s(18), (22, 27, 35, 255), (255, 142, 32, 255), s(9))
    tri = pts([(704, 398), (668, 456), (742, 456), (704, 398)], scale)
    draw.line(tri, fill=(255, 142, 32, 255), width=s(8), joint="curve")

    rounded(draw, (s(392), s(520), s(554), s(582)), s(18), (246, 248, 245, 255))
    rounded(draw, (s(420), s(580), s(526), s(630)), s(16), (246, 248, 245, 255))
    rounded(draw, (s(404), s(630), s(548), s(674)), s(18), (246, 248, 245, 255))

    beam = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    bd = ImageDraw.Draw(beam)
    bd.line((s(476), s(676), s(476), s(734)), fill=(255, 119, 21, 255), width=s(10))
    for radius, alpha in [(94, 65), (58, 180), (28, 255)]:
        for i in range(16):
            angle = math.tau * i / 16
            x = 476 + math.cos(angle) * radius
            y = 744 + math.sin(angle) * radius
            bd.line((s(476), s(744), s(x), s(y)), fill=(255, 119, 21, alpha), width=s(7 if i % 2 == 0 else 3))
    bd.ellipse((s(450), s(718), s(502), s(770)), fill=(255, 132, 28, 255))
    beam = beam.filter(ImageFilter.GaussianBlur(s(0.4)))
    image.alpha_composite(beam)

    label_font = font(s(44))
    draw.text((s(374), s(368)), "OpenMopa", font=label_font, fill=(246, 248, 245, 255), anchor="lm")

    return image.resize((size, size), Image.Resampling.LANCZOS)


def write_icns(source: Image.Image, out_path: Path) -> None:
    chunks = [("icp4", 16), ("icp5", 32), ("icp6", 64), ("ic07", 128), ("ic08", 256), ("ic09", 512), ("ic10", 1024)]
    payload = bytearray()
    for chunk_type, out_size in chunks:
        buffer = io.BytesIO()
        source.resize((out_size, out_size), Image.Resampling.LANCZOS).save(buffer, format="PNG")
        png = buffer.getvalue()
        payload.extend(chunk_type.encode("ascii"))
        payload.extend(struct.pack(">I", len(png) + 8))
        payload.extend(png)
    out_path.write_bytes(b"icns" + struct.pack(">I", len(payload) + 8) + bytes(payload))


def save_icons() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    source = draw_desktop_icon(1024)
    source.save(ICONS / "openmopa.png")
    for size in (16, 24, 32, 48, 64, 128, 256, 512, 1024):
        source.resize((size, size), Image.Resampling.LANCZOS).save(ICONS / f"openmopa-{size}.png")
    source.save(ICONS / "openmopa.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    write_icns(source, ICONS / "openmopa.icns")

    MACOS_RESOURCES.mkdir(parents=True, exist_ok=True)
    write_icns(source, MACOS_RESOURCES / "openmopa.icns")


if __name__ == "__main__":
    save_icons()
