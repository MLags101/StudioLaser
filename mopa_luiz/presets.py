"""Laser presets: built-in starting points and the user's saved library."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from uuid import uuid4

from .device_profile import write_json
from .layers import LayerSpec, RASTER_ENGRAVE, VECTOR_ENGRAVE, layer_from_dict
from .workspace import CLEANUP, COPPER_CONTOUR, COPPER_FILL, KIND_LABELS, KINDS, MASK, SILK

VERSION = 2
UNTESTED = "Untested starting point — run a material test and save your own."


@dataclass(frozen=True)
class Preset:
    preset_id: str
    name: str
    kind: str
    layer: LayerSpec
    machine: str = ""
    lens: str = ""
    source: str = ""
    stock: str = ""
    copper_um: float = 0.0
    kerf_mm: float = 0.0
    note: str = ""
    builtin: bool = False
    default: bool = False

    def label(self) -> str:
        parts = [self.name]
        if self.stock:
            parts.append(self.stock)
        if self.copper_um:
            parts.append(f"{self.copper_um:g} µm Cu")
        if self.lens:
            parts.append(self.lens)
        return " · ".join(parts) + ("  (built-in)" if self.builtin else "")

    def summary(self) -> str:
        return layer_summary(self.layer)

    def to_dict(self) -> dict:
        return {
            "preset_id": self.preset_id, "name": self.name, "kind": self.kind, "layer": self.layer.to_dict(),
            "machine": self.machine, "lens": self.lens, "source": self.source, "stock": self.stock,
            "copper_um": self.copper_um, "kerf_mm": self.kerf_mm, "note": self.note, "default": self.default,
        }


def layer_summary(layer: LayerSpec) -> str:
    text = (f"{layer.power_percent:g}% · {layer.speed_mm_s:g} mm/s · {layer.frequency_khz:g} kHz · "
            f"{layer.pulse_width_ns:g} ns")
    if layer.operation == RASTER_ENGRAVE:
        text += f" · {layer.raster_pitch_mm:g} mm hatch"
        if layer.crosshatch:
            text += " ×"
    return text + f" · {layer.passes}×"


def _layer(kind, operation, power, khz, ns, speed, passes, *, pitch=0.03, angle=0.0, step=90.0,
           cross=False, output=True, color="#e0a030") -> LayerSpec:
    return LayerSpec(kind, KIND_LABELS[kind], operation, power, khz, ns, speed, passes=passes, output=output,
                     color=color, raster_pitch_mm=pitch, raster_angle_deg=angle, raster_angle_step_deg=step,
                     crosshatch=cross)


# Starting points for a 60 W JPT M7 MOPA, 150 mm lens, 35 µm (1 oz) copper on FR4.
# Sources are vendor and community figures, not measurements on this machine;
# see docs/reference/SOURCES.md.
_BUILTIN = (
    (COPPER_CONTOUR, _layer(COPPER_CONTOUR, VECTOR_ENGRAVE, 100, 40, 20, 800, 3, color="#ff7a45"),
     "Short 20 ns pulses along trace edges for a clean wall. From ComMarker's 60 W MOPA fine-trace figures."),
    (COPPER_FILL, _layer(COPPER_FILL, RASTER_ENGRAVE, 100, 45, 200, 1000, 4, pitch=0.025, angle=45, color="#e0a030"),
     "200 ns at its 45 kHz cut-off for full pulse energy; hatch turns 90° each pass. "
     "From ComMarker's 60 W PCB settings and a LightBurn-forum MOPA write-up."),
    (CLEANUP, _layer(CLEANUP, RASTER_ENGRAVE, 25, 100, 200, 1500, 1, pitch=0.02, angle=0, output=False, color="#c7b26a"),
     "Light pass to lift residue and char. Off by default; enable if the laminate looks dirty."),
    (MASK, _layer(MASK, RASTER_ENGRAVE, 14, 40, 100, 900, 5, pitch=0.03, cross=True, color="#3fb68b"),
     "Scaled from 20 W fiber figures for UV-cured mask. Check pads under magnification: the copper should be bare, not pitted."),
    (SILK, _layer(SILK, RASTER_ENGRAVE, 11, 60, 45, 1000, 1, pitch=0.03, color="#d9d9e3"),
     "Marks the solder mask surface. Lowest-confidence starting point; dark mask gives the best contrast."),
)


def builtin_presets() -> tuple[Preset, ...]:
    return tuple(
        Preset(f"builtin-{kind}", f"{KIND_LABELS[kind]} — 60 W starting point", kind, replace(layer),
               machine="ComMarker B6 MOPA", lens="150 mm", source="JPT M7 60 W (YDFLP-60-M7)",
               stock="FR4", copper_um=35.0 if kind in (COPPER_CONTOUR, COPPER_FILL, CLEANUP) else 0.0,
               note=f"{note} {UNTESTED}", builtin=True)
        for kind, layer, note in _BUILTIN
    )


def starting_layer(kind: str) -> LayerSpec:
    for preset in builtin_presets():
        if preset.kind == kind:
            return replace(preset.layer)
    return LayerSpec(kind or "Engrave", KIND_LABELS.get(kind, "Engrave"), VECTOR_ENGRAVE, 0, 30, 200, 1000)


class PresetLibrary:
    """User presets on disk plus the read-only built-ins."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.user: list[Preset] = []
        self.error = ""
        if path is not None and path.exists():
            try:
                self.user = read_presets(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # A damaged library must not stop the app or other state loading.
                self.error = f"Preset library could not be read ({exc}). It was left untouched."
                self.path = None

    def all(self) -> list[Preset]:
        return [*self.user, *builtin_presets()]

    def for_kind(self, kind: str) -> list[Preset]:
        return [preset for preset in self.all() if preset.kind == kind or not preset.kind or not kind]

    def get(self, preset_id: str) -> Preset | None:
        return next((preset for preset in self.all() if preset.preset_id == preset_id), None)

    def defaults(self) -> dict[str, LayerSpec]:
        """Layer per kind for a freshly imported board: the user's default, else the built-in."""
        chosen = {}
        for kind in KINDS:
            preset = next((p for p in self.user if p.kind == kind and p.default), None)
            if preset is not None:
                chosen[kind] = replace(preset.layer)
        return chosen

    def kerf(self, kind: str = COPPER_FILL) -> float:
        preset = next((p for p in self.user if p.kind == kind and p.default), None)
        return preset.kerf_mm if preset else 0.0

    def save(self) -> None:
        if self.path is None:
            raise OSError("The preset library is read-only this session because it could not be read at start-up")
        write_json(self.path, {"version": VERSION, "presets": [preset.to_dict() for preset in self.user]})

    def add(self, preset: Preset) -> Preset:
        preset = replace(preset, preset_id=preset.preset_id or uuid4().hex, builtin=False)
        validate_preset(preset)
        self._replace([p for p in self.user if p.preset_id != preset.preset_id] + [preset], preset)
        return preset

    def remove(self, preset_id: str) -> None:
        self._replace([p for p in self.user if p.preset_id != preset_id], None)

    def _replace(self, presets: list[Preset], changed: Preset | None) -> None:
        if changed is not None and changed.default:
            # One default per operation kind.
            presets = [replace(p, default=False) if p.kind == changed.kind and p.preset_id != changed.preset_id else p
                       for p in presets]
        previous, self.user = self.user, presets
        try:
            self.save()
        except Exception:
            self.user = previous
            raise

    def export(self, path: Path, preset_ids: list[str] | None = None) -> None:
        chosen = [p for p in self.all() if preset_ids is None or p.preset_id in preset_ids]
        write_json(path, {"version": VERSION, "presets": [preset.to_dict() for preset in chosen]})

    def import_file(self, path: Path) -> int:
        incoming = [replace(preset, preset_id=uuid4().hex, default=False)
                    for preset in read_presets(json.loads(path.read_text(encoding="utf-8")))]
        self._replace(self.user + incoming, None)
        return len(incoming)


def validate_preset(preset: Preset) -> None:
    if not preset.name.strip():
        raise ValueError("Give the preset a name")
    if preset.kind not in ("", *KINDS):
        raise ValueError("Unknown preset operation")
    if not 0 <= preset.kerf_mm <= 1 or not 0 <= preset.copper_um <= 1000:
        raise ValueError("Kerf must be 0–1 mm and copper thickness 0–1000 µm")


def read_presets(data) -> list[Preset]:
    version = data.get("version")
    if version == 1:
        # Version 1: {"recipes": [{name, stock, copper_um, machine, lens, source, layer}]}
        return [
            Preset(uuid4().hex, str(raw["name"]), "", layer_from_dict(raw["layer"]), str(raw.get("machine", "")),
                   str(raw.get("lens", "")), str(raw.get("source", "")), str(raw.get("stock", "")),
                   float(raw.get("copper_um", 0)))
            for raw in data.get("recipes", [])
        ]
    if version != VERSION:
        raise ValueError("unsupported preset library version")
    presets = []
    for raw in data.get("presets", []):
        preset = Preset(
            str(raw.get("preset_id") or uuid4().hex), str(raw["name"]), str(raw.get("kind", "")),
            layer_from_dict(raw["layer"]), str(raw.get("machine", "")), str(raw.get("lens", "")),
            str(raw.get("source", "")), str(raw.get("stock", "")), float(raw.get("copper_um", 0)),
            float(raw.get("kerf_mm", 0)), str(raw.get("note", "")), False, bool(raw.get("default", False)),
        )
        validate_preset(preset)
        presets.append(preset)
    return presets
