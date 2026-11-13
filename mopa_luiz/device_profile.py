"""Versioned machine/lens profiles and the EZCAD2 import boundary."""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import asdict, dataclass, field, replace
from functools import cached_property
import json
import math
from pathlib import Path
from tempfile import NamedTemporaryFile

from .cli import MarkConfig, load_markcfg
from .correction import CorTable, EzcadField, FieldMap, cor_summary, validate_cor
from .sources import DEFAULT_SOURCE, SOURCES, Limits, parse_limits

DELAYS = {
    "LASERONDELAY": "delay_laser_on",
    "LASEROFFDELAY": "delay_laser_off",
    "POLYGONDELAY": "delay_polygon",
    "JUMPDELAY": "delay_jump_short",
    "JUMPDELAYLONG": "delay_jump_long",
}
BASES = ("cor", "ezcad", "none")
BASE_LABELS = {
    "cor": "Factory correction file (.cor)",
    "ezcad": "EZCAD field parameters (approximated)",
    "none": "No lens correction",
}
VERSION = 2
TRIM_LIMITS = (0.8, 1.25)
# How galvoplotter is driven. "original" uses exactly the settings of the original program, which ran this
# machine reliably; "ezcad" also sends the timing and speeds read from markcfg7 / MarkParam.lib.
DRIVERS = ("original", "ezcad")
DRIVER_LABELS = {"original": "Original program's settings (recommended)",
                 "ezcad": "Timing and speeds from the EZCAD files"}


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        scratch = Path(tmp.name)
        try:
            json.dump(data, tmp, indent=2, allow_nan=False)
            tmp.write("\n")
            tmp.close()
            scratch.replace(path)
        finally:
            tmp.close()
            scratch.unlink(missing_ok=True)


@dataclass(frozen=True)
class DeviceProfile:
    name: str = "Offline preview"
    lens: str = ""
    source: str = ""
    width: float = 150
    height: float = 150
    min_hz: int = 0
    max_hz: int = 0
    limits: Limits = ()
    swap_xy: bool = False
    invert_x: bool = False
    invert_y: bool = False
    red_x: float = 0
    red_y: float = 0
    red_scale_x: float = 1
    red_scale_y: float = 1
    frame_speed: float = 3000
    jump_speed: float = 3000
    correction: str = ""
    correction_name: str = ""     # file the .cor table came from, for display only
    base: str = "none"
    ezcad: EzcadField = EzcadField()
    calibration: FieldMap | None = None
    calibration_note: str = ""
    delays: dict[str, float] = field(default_factory=dict)
    enabled: bool = False
    # Size trim from the quick square check: scales X and Y about the field centre, on top of the
    # measured calibration. A full grid calibration folds it in and resets it to 1.
    trim_x: float = 1.0
    trim_y: float = 1.0
    # +1 adds the .cor table's offsets the way the controller board does; -1 reverses them.
    cor_sign: int = 1
    driver: str = "original"

    @property
    def pulse_widths(self) -> tuple[int, ...]:
        return tuple(width for width, _, _ in self.limits)

    @property
    def half(self) -> float:
        return max(self.width, self.height) / 2

    @property
    def units_per_mm(self) -> float:
        """Galvo units per millimetre for speed conversion (the controller takes one value)."""
        return 65535 / ((self.width + self.height) / 2)

    def missing(self) -> list[str]:
        """What still stops this profile from driving the laser, in plain words."""
        gaps = []
        if not self.name.strip():
            gaps.append("a machine name")
        if not self.source.strip():
            gaps.append("the laser source")
        if not self.lens.strip():
            gaps.append("the lens")
        if not self.limits:
            gaps.append("the source's pulse widths")
        if not 0 < self.min_hz <= self.max_hz <= 4_000_000:
            gaps.append("the frequency range")
        if self.base == "cor" and not self.correction:
            gaps.append("the lens correction file")
        return gaps

    def validate(self, *, live: bool = False) -> None:
        if any(not math.isfinite(v) or v <= 0 for v in (
            self.width, self.height, self.red_scale_x, self.red_scale_y, self.frame_speed, self.jump_speed,
        )):
            raise ValueError("Field sizes, frame scales and speeds must be finite and positive")
        if not all(math.isfinite(v) for v in (self.red_x, self.red_y)):
            raise ValueError("Red-light offsets must be finite")
        if not all(math.isfinite(v) and TRIM_LIMITS[0] <= v <= TRIM_LIMITS[1] for v in (self.trim_x, self.trim_y)):
            raise ValueError(f"Size trim must be between {TRIM_LIMITS[0]:g} and {TRIM_LIMITS[1]:g}")
        if self.frame_speed > 15000 or self.jump_speed > 15000:
            raise ValueError("Frame and jump speeds must not exceed 15000 mm/s")
        if self.base not in BASES:
            raise ValueError("Unknown lens correction base")
        if self.driver not in DRIVERS or self.cor_sign not in (1, -1):
            raise ValueError("Unknown driver setting or .cor direction")
        # EZCAD uses a negative laser-on delay (the laser starts before the mirrors settle); the
        # controller encodes the sign, so on/off delays may be negative and the others may not.
        signed = {"delay_laser_on", "delay_laser_off"}
        if set(self.delays) - set(DELAYS.values()) or any(
            not math.isfinite(v) or not (-32767 if key in signed else 0) <= v <= 32767
            for key, v in self.delays.items()
        ):
            raise ValueError("Unsupported controller delay settings")
        parse_limits(self.limits)
        if self.correction:
            validate_cor(b64decode(self.correction, validate=True))
        if live:
            if not self.enabled:
                raise ValueError("Enable this machine profile in Machine Setup before live use")
            gaps = self.missing()
            if gaps:
                raise ValueError("Machine Setup still needs " + ", ".join(gaps))

    def config(self) -> MarkConfig:
        return MarkConfig(Path(self.name), {
            "FIELDSIZE": str(self.width),
            "MINPWMFREQ": str(self.min_hz),
            "MAXPWMFREQ": str(self.max_hz),
            "PULSEWIDTHS": ",".join(map(str, self.pulse_widths)),
        })

    @cached_property
    def _calibration_map(self) -> FieldMap | None:
        return self.calibration if self.calibration is not None and not self.calibration.identity else None

    @cached_property
    def _base_map(self) -> FieldMap | None:
        base = self.ezcad.field_map(self.half, distortion=self.base == "ezcad")
        return None if base.identity else base

    @cached_property
    def _cor_table(self) -> CorTable | None:
        """The factory table, applied here: the board itself always gets a blank one."""
        if self.base != "cor" or not self.correction:
            return None
        table = CorTable(b64decode(self.correction), self.cor_sign)
        return None if table.identity else table

    @property
    def curved(self) -> bool:
        """True when straight lines must be subdivided to follow the correction."""
        return self._calibration_map is not None or self._base_map is not None or self._cor_table is not None

    def calibrated(self, point):
        """Target mm after the measured calibration and the size trim: what a grid fit is relative to."""
        x, y = point
        if self._calibration_map is not None:
            x, y = self._calibration_map.apply((x, y))
        return x * self.trim_x, y * self.trim_y

    def command_point(self, point, *, red_light=False):
        """Target mm -> the mm position to command, after software correction."""
        x, y = point
        if not (abs(x) <= self.width / 2 + 1e-6 and abs(y) <= self.height / 2 + 1e-6):
            raise ValueError(f"Point ({x:.2f}, {y:.2f}) mm lies outside the {self.width:g} × {self.height:g} mm field")
        x, y = self.calibrated((x, y))
        if self._base_map is not None:
            x, y = self._base_map.apply((x, y))
        if red_light:
            x, y = x * self.red_scale_x + self.red_x, y * self.red_scale_y + self.red_y
        return x, y

    def galvo_point(self, point, *, red_light=False):
        x, y = self.command_point(point, red_light=red_light)
        x, y = x / self.width, y / self.height
        if not (-0.500000001 <= x <= 0.500000001 and -0.500000001 <= y <= 0.500000001):
            raise ValueError(f"Point ({point[0]:.2f}, {point[1]:.2f}) mm needs galvo travel beyond the field after correction")
        if self.swap_xy:
            x, y = y, x
        x = -x if self.invert_x else x
        y = -y if self.invert_y else y
        gx, gy = (min(0.5, max(-0.5, x)) + 0.5) * 65535, (min(0.5, max(-0.5, y)) + 0.5) * 65535
        if self._cor_table is not None:
            # The table works in the controller's own coordinates, after any axis swap or reversal.
            gx, gy = self._cor_table.apply(gx, gy)
            if not (-0.5 <= gx <= 65535.5 and -0.5 <= gy <= 65535.5):
                raise ValueError(f"Point ({point[0]:.2f}, {point[1]:.2f}) mm needs galvo travel beyond the field "
                                 "after the lens correction")
        return min(65535, max(0, round(gx))), min(65535, max(0, round(gy)))

    def with_calibration(self, field_map: FieldMap | None, note: str = "") -> "DeviceProfile":
        """A grid fit is measured relative to calibration and trim together, so it replaces both."""
        return replace(self, calibration=field_map, calibration_note=note, trim_x=1.0, trim_y=1.0)

    def with_trim(self, target: float, measured_x: float, measured_y: float) -> "DeviceProfile":
        """Scale so a square drawn ``target`` mm wide, which came out ``measured`` mm, comes out true."""
        if not all(math.isfinite(v) and v > 0 for v in (target, measured_x, measured_y)):
            raise ValueError("Enter the square's size and both measurements in millimetres")
        trim_x, trim_y = self.trim_x * target / measured_x, self.trim_y * target / measured_y
        if not all(TRIM_LIMITS[0] <= v <= TRIM_LIMITS[1] for v in (trim_x, trim_y)):
            raise ValueError(f"That is a {max(abs(trim_x - 1), abs(trim_y - 1)) * 100:.0f} % correction. Check the "
                             "measurement and that the square was marked at the size entered here.")
        return replace(self, trim_x=round(trim_x, 6), trim_y=round(trim_y, 6))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["limits"] = [list(row) for row in self.limits]
        data["ezcad"] = asdict(self.ezcad)
        data["calibration"] = self.calibration.to_dict() if self.calibration else None
        return {"version": VERSION, **data}

    def save(self, path: Path) -> None:
        self.validate(live=self.enabled)
        write_json(path, self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "DeviceProfile":
        data = dict(data)
        version = data.pop("version", None)
        if version == 1:
            # Version 1 stored a bare pulse-width list and always used a .cor file.
            widths = data.pop("pulse_widths", ())
            low, high = data.get("min_hz", 0) / 1000, data.get("max_hz", 0) / 1000
            known = {row[0]: row for row in SOURCES[DEFAULT_SOURCE]}
            data["limits"] = [known.get(int(w), (int(w), max(low, 0.001), min(max(high, 0.001), 4000))) for w in widths]
            data["base"] = "cor" if data.get("correction") else "none"
        elif version != VERSION:
            raise ValueError("Unsupported machine profile version")
        data["limits"] = parse_limits(data.get("limits", ()))
        data["ezcad"] = EzcadField(**data.get("ezcad", {}))
        calibration = data.get("calibration")
        data["calibration"] = FieldMap.from_dict(calibration) if calibration else None
        known_fields = set(cls.__dataclass_fields__)
        profile = cls(**{key: value for key, value in data.items() if key in known_fields})
        profile.validate(live=profile.enabled)
        return profile

    @classmethod
    def open(cls, path: Path) -> "DeviceProfile":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def import_ezcad(config: Path, correction: Path | None = None, *,
                 source: str = DEFAULT_SOURCE, lens: str = "") -> tuple[DeviceProfile, str]:
    """Build a profile from EZCAD2's markcfg7 and, optionally, the lens .cor file.

    The profile is returned disabled: the operator reviews it and runs the
    calibration check before enabling live use.
    """
    cfg = load_markcfg(config)
    values = {key.upper(): value for key, value in cfg.values.items()}
    cfg = MarkConfig(config, values)
    notes = []
    cor_text = ""
    if correction is not None:
        cor_data = correction.read_bytes()
        validate_cor(cor_data)
        cor_text = b64encode(cor_data).decode("ascii")
        summary = cor_summary(cor_data)
        notes.append(
            f"Lens correction file {correction.name}: valid 65 × 65 table"
            + (f", written for a {summary['field_mm']:.1f} mm field" if summary["field_mm"] else "")
            + (f", largest offset {summary['peak_offset_mm']:.2f} mm" if summary["field_mm"] else "") + "."
        )
    field_size = cfg.get_float("FIELDSIZE", 0) or 0
    if field_size <= 0:
        field_size = 150.0
        notes.append("FIELDSIZE is missing; assumed 150 mm. Set the real field size.")
    ezcad = EzcadField(
        offset_x=cfg.get_float("FIELDOFFSETX", 0.0), offset_y=cfg.get_float("FIELDOFFSETY", 0.0),
        angle=cfg.get_float("FIELDANGLE", 0.0),
        scale_x=cfg.get_float("GALVOASPECT0", 100.0) / 100, scale_y=cfg.get_float("GALVOASPECT1", 100.0) / 100,
        bulge_x=cfg.get_float("GALVODISTOR0", 1.0), bulge_y=cfg.get_float("GALVODISTOR1", 1.0),
        skew_x=cfg.get_float("GALVOHORVER0", 1.0), skew_y=cfg.get_float("GALVOHORVER1", 1.0),
        trapezoid_x=cfg.get_float("GALVOTRAPEDISTOR0", 1.0), trapezoid_y=cfg.get_float("GALVOTRAPEDISTOR1", 1.0),
    )
    cor_enabled = bool(cfg.get_bool("ENABLECORFILE", False))
    if cor_enabled and cor_text:
        base = "cor"
        notes.append("EZCAD had the correction file enabled, so it is selected as the lens correction.")
    elif ezcad.distorts:
        base = "ezcad"
        notes.append(
            "EZCAD had the correction file switched off (ENABLECORFILE=0) and was correcting the lens with its "
            f"field parameters (bulge {ezcad.bulge_x:g} / {ezcad.bulge_y:g}). StudioLaser approximates those "
            "parameters; EZCAD's exact formula is not published. Run Calibrate Lens and measure before trusting sizes."
        )
        if cor_text:
            notes.append("The .cor file is stored in the profile and can be selected instead in Calibrate Lens.")
    else:
        base = "cor" if cor_text else "none"
        if base == "none":
            notes.append("No lens correction was found. Run Calibrate Lens before marking to size.")
    limits = SOURCES.get(source, ())
    if "PULSEWIDTHS" in values:
        widths = {int(float(n)) for n in values["PULSEWIDTHS"].split(",") if n.strip()}
        known = {row[0]: row for row in limits}
        low = max(cfg.get_int("MINPWMFREQ", 1000) / 1000, 0.001)
        high = min(cfg.get_int("MAXPWMFREQ", 4_000_000) / 1000, 4000)
        # Honour every listed width; ones the source table does not know get the profile's whole range.
        limits = tuple(known.get(w, (w, low, high)) for w in sorted(widths))
    else:
        notes.append(f"markcfg7 does not list pulse widths; using the {source} table. Confirm the source model.")
    # In EZCAD, GALVOX=1 means galvo 1 drives X, which is the unswapped arrangement.
    profile = DeviceProfile(
        name="ComMarker B6 MOPA", lens=lens, source=source, width=field_size, height=field_size,
        min_hz=cfg.get_int("MINPWMFREQ", 0), max_hz=cfg.get_int("MAXPWMFREQ", 0), limits=limits,
        swap_xy=not cfg.get_bool("GALVOX", True) if "GALVOX" in values else bool(cfg.get_bool("SWAPXY", False)),
        invert_x=bool(cfg.get_bool("GALVONEGATE0", cfg.get_bool("INVERTX", False))),
        invert_y=bool(cfg.get_bool("GALVONEGATE1", cfg.get_bool("INVERTY", False))),
        red_x=cfg.get_float("REDLIGHTOFFSETX", 0.0) + 0.0, red_y=cfg.get_float("REDLIGHTOFFSETY", 0.0) + 0.0,
        red_scale_x=cfg.get_float("REDLIGHTSCALE", 1.0), red_scale_y=cfg.get_float("REDLIGHTSCALEY", 1.0),
        frame_speed=min(cfg.get_float("REDLIGHTSPEED", 3000), 15000),
        correction=cor_text, correction_name=correction.name if correction is not None else "", base=base, ezcad=ezcad,
        delays={dest: cfg.get_float(key) for key, dest in DELAYS.items() if key in values},
    )
    profile.validate()
    notes.insert(0, f"Field {field_size:g} mm, frequency {profile.min_hz / 1000:g}–{profile.max_hz / 1000:g} kHz, "
                    f"{len(profile.limits)} pulse widths.")
    if nominal_lens_hint(field_size):
        notes.append(nominal_lens_hint(field_size))
    return profile, "\n".join(notes)


def nominal_lens_hint(field_size: float) -> str:
    for nominal in (70, 110, 150, 175, 200, 300):
        if nominal < field_size <= nominal * 1.2:
            return (f"The {field_size:g} mm field is the galvo's full travel; the {nominal} mm lens covers "
                    "the middle of it. Keep work inside the lens's rated area.")
    return ""
