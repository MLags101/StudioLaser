from __future__ import annotations

import argparse
import configparser
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_PROFILE = Path(
    os.environ.get("STUDIOLASER_PROFILE")
    or os.environ.get("OPENLASER_PROFILE")        # former name
    or os.environ.get("OPENMOPA_PROFILE")
    or Path(__file__).resolve().parents[1] / "profiles" / "openmopa-machine.ini"
)

JCZ_VENDOR_ID = 0x9588
JCZ_PRODUCT_ID = 0x9899
JPT_M7_PULSE_WIDTHS_NS = (
    2,
    4,
    6,
    9,
    13,
    20,
    30,
    45,
    55,
    60,
    80,
    100,
    150,
    200,
    250,
    300,
    350,
    400,
    450,
    500,
)


@dataclass(frozen=True)
class MarkConfig:
    path: Path
    values: dict[str, str]

    def get_int(self, key: str, default: int | None = None) -> int | None:
        value = self.values.get(key)
        if value is None:
            return default
        return int(float(value))

    def get_float(self, key: str, default: float | None = None) -> float | None:
        value = self.values.get(key)
        if value is None:
            return default
        return float(value)

    def get_bool(self, key: str, default: bool | None = None) -> bool | None:
        value = self.values.get(key)
        if value is None:
            return default
        return bool(int(float(value)))


def load_markcfg(path: Path) -> MarkConfig:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    text = path.read_text(encoding="latin-1", errors="ignore")
    parser.read_string(text)
    if "LMC_CFG" not in parser:
        raise ValueError(f"{path} does not contain an [LMC_CFG] section")
    return MarkConfig(path=path, values=dict(parser["LMC_CFG"]))


def pulse_width_table(cfg: MarkConfig | None) -> tuple[int, ...]:
    """Pulse widths (ns) this machine accepts.

    Profiles may override the default JPT M7 table with a PULSEWIDTHS key
    ([LMC_CFG], comma-separated integer nanoseconds). Invalid or empty
    values fall back to the M7 table so safety never loosens silently.
    """
    if cfg is None or "PULSEWIDTHS" not in cfg.values:
        return JPT_M7_PULSE_WIDTHS_NS
    try:
        values = tuple(
            sorted({int(float(part.strip())) for part in cfg.values["PULSEWIDTHS"].split(",")})
        )
    except (TypeError, ValueError):
        return JPT_M7_PULSE_WIDTHS_NS
    if not values or any(value <= 0 for value in values):
        return JPT_M7_PULSE_WIDTHS_NS
    return values


def nearest_pulse_width(value: float, table: tuple[int, ...] = JPT_M7_PULSE_WIDTHS_NS) -> int:
    return min(table, key=lambda candidate: abs(candidate - value))


def run_command(args: list[str]) -> tuple[int, str]:
    if shutil.which(args[0]) is None:
        return 127, f"{args[0]} not found"
    # A windowed app on Windows gets a console window flashed up for every child
    # process unless it asks for none; the desktop app runs this in the background.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
                              creationflags=flags, timeout=20, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"{args[0]} did not finish: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def usb_candidates():
    """Return the actual PyUSB devices and the backend status."""
    if importlib.util.find_spec("usb") is None:
        return [], {"available": False, "note": "pyusb is not installed for this Python."}
    import usb.core  # type: ignore[import-not-found]

    from .usb_backend import ensure_libusb_backend

    libusb = ensure_libusb_backend()
    if not libusb["available"]:
        return [], libusb
    devices = list(
        usb.core.find(idVendor=JCZ_VENDOR_ID, idProduct=JCZ_PRODUCT_ID, find_all=True)
    )
    return devices, libusb


def pyusb_probe() -> dict[str, Any]:
    devices, libusb = usb_candidates()
    return {**libusb, "libusb": libusb, "devices": [describe_usb(dev) for dev in devices]}


def usb_port(dev) -> str:
    try:
        numbers = dev.port_numbers
    except Exception:
        numbers = None
    return ".".join(map(str, numbers)) if numbers else ""


def describe_usb(dev) -> dict[str, Any]:
    """A JCZ device as reported by libusb, with a key that survives power cycles.

    The key is the bus and the physical port chain, so the same laser keeps its
    key while it stays plugged into the same socket. Without port information
    it falls back to the bus address, which changes when the device re-enumerates.
    """
    bus, port, address = getattr(dev, "bus", None), usb_port(dev), getattr(dev, "address", None)
    return {
        "idVendor": f"0x{dev.idVendor:04x}",
        "idProduct": f"0x{dev.idProduct:04x}",
        "bus": bus,
        "address": address,
        "port": port,
        "key": f"{bus}-{port}" if port else f"{bus}:{address}",
    }


def usb_label(key: str) -> str:
    """'1-3.2' -> 'USB bus 1, port 3.2'; '1:7' -> 'USB bus 1, device 7'."""
    if "-" in key:
        bus, port = key.split("-", 1)
        return f"USB bus {bus}, port {port}"
    if ":" in key:
        bus, address = key.split(":", 1)
        return f"USB bus {bus}, device {address}"
    return key


def os_usb_listing() -> tuple[str, int, list[str]]:
    """Ask the operating system's own USB tool for the laser, independent of libusb."""
    if sys.platform == "darwin":
        tool = ["ioreg", "-p", "IOUSB", "-l", "-w", "0"]
    elif sys.platform.startswith("win"):
        tool = ["pnputil", "/enum-devices", "/connected"]
    else:
        tool = ["lsusb"]
    code, out = run_command(tool)
    interesting_terms = ("9588", "9899", "JCZ", "BJJCZ", "LMCV")
    hits = [
        line.strip()
        for line in out.splitlines()
        if any(term.lower() in line.lower() for term in interesting_terms)
    ]
    return tool[0], code, hits


def detect_hint(seen_by_os: bool, usable: bool, libusb_ok: bool) -> str | None:
    if usable:
        return None
    if not libusb_ok:
        return "The libusb library could not be loaded. Run the StudioLaser installer again."
    if not seen_by_os:
        return "No JCZ/LMC laser is visible. Check machine power and the USB cable, then run detect again."
    if sys.platform.startswith("win"):
        return (
            "Windows sees the laser, but it is using the EZCAD (BJJCZ) driver, which StudioLaser "
            "cannot use. Install the WinUSB driver for USBLMCV4 with Zadig; see the README."
        )
    if sys.platform.startswith("linux"):
        return (
            "Linux sees the laser, but this user may not have permission to open it. Install "
            "the udev rule from scripts/99-studiolaser.rules; see the README."
        )
    return "The laser is visible to the system but not to libusb. Reconnect USB and try again."


def detect_report() -> dict[str, Any]:
    pyusb = pyusb_probe()
    tool, tool_code, os_hits = os_usb_listing()
    usable = bool(pyusb["devices"])
    seen_by_os = bool(os_hits)
    libusb = pyusb.get("libusb") or {}
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "expected_jcz_lmc_usb_id": "0x9588:0x9899",
        "connected": usable,
        "seen_by_operating_system": seen_by_os,
        "libusb": libusb,
        "pyusb": pyusb,
        "os_usb_tool": tool,
        "os_usb_tool_exit": tool_code,
        "os_usb_hits": os_hits,
        "hint": detect_hint(seen_by_os, usable, bool(libusb.get("available"))),
        "safe_status": "no laser commands sent",
    }


def detect(_: argparse.Namespace) -> int:
    report = detect_report()
    print(json.dumps(report, indent=2))
    if not report["connected"]:
        print(f"\n{report['hint']}")
        return 2
    return 0


def config_summary(cfg: MarkConfig) -> dict[str, Any]:
    keys = (
        "LASERTYPE",
        "FIELDSIZE",
        "MINPWMFREQ",
        "MAXPWMFREQ",
        "m_nIPGSerialNo",
        "m_bEnableIPGSetPulseWidth",
        "m_nEnableFiberLaserStateCheckInMarking",
        "REDLIGHTSPEED",
        "DOORINPORT",
        "STARTMARKPORT",
        "ENABLECORFILE",
        "CORFILE",
    )
    summary = {key: cfg.values.get(key) for key in keys if key in cfg.values}
    summary["path"] = str(cfg.path)
    summary["safe_status"] = "parsed only; machine profile not modified"
    return summary


def show_config(args: argparse.Namespace) -> int:
    cfg = load_markcfg(args.profile)
    summary = config_summary(cfg)
    print(json.dumps(summary, indent=2))
    return 0


def pulse_widths(args: argparse.Namespace) -> int:
    cfg = load_markcfg(args.profile)
    print(json.dumps({"profile": str(cfg.path), "pulse_widths_ns": pulse_width_table(cfg)}, indent=2))
    return 0


def desktop_command(_: argparse.Namespace) -> int:
    try:
        from .desktop import main as desktop_main
    except ImportError as exc:
        raise RuntimeError(
            f'the desktop app needs the [desktop] extra ({exc}). Run: pip install -e ".[desktop]"'
        ) from exc
    return desktop_main() or 0


def export_pcb_svg(args: argparse.Namespace) -> int:
    from .pcb.board import prepare_board
    from .pcb.inverter import InversionMode
    from .pcb.svg_exporter import export_to_svg

    copper = {key: [path] for key, path in (("top_copper", args.top), ("bottom_copper", args.bottom)) if path}
    sides = prepare_board(
        {"outline": [args.outline], "drill": args.drill, **copper},
        mode=InversionMode.ISOLATION if args.mode == "isolation" else InversionMode.FULL_INVERT,
        isolation_width=args.isolation_width, flip_axis=args.flip_axis,
    )
    for side in sides:
        output = args.output / f"{args.outline.stem}-{side.side}.svg"
        export_to_svg(side.removal, str(output), view_bounds=side.outline.bounds)
        print(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="studiolaser",
        description="StudioLaser: safety-first jobs for JCZ/LMC galvo fiber lasers. "
        "Without a command, the desktop workspace opens.",
    )
    parser.set_defaults(func=desktop_command)
    sub = parser.add_subparsers(dest="command")
    profile = {"dest": "profile", "metavar": "PROFILE", "type": Path, "default": DEFAULT_PROFILE}

    p = sub.add_parser("desktop", help="Open the native StudioLaser workspace (the default).")
    p.set_defaults(func=desktop_command)

    p = sub.add_parser("detect", help="Check local USB/runtime readiness without laser commands.")
    p.set_defaults(func=detect)

    p = sub.add_parser("show-config", help="Parse the machine profile and print mapped fields.")
    p.add_argument("--profile", "--markcfg", **profile)
    p.set_defaults(func=show_config)

    p = sub.add_parser("pulse-widths", help="Print the pulse-width table the machine profile accepts.")
    p.add_argument("--profile", "--markcfg", **profile)
    p.set_defaults(func=pulse_widths)

    p = sub.add_parser("pcb-svg", help="Export registered copper-removal SVGs without hardware.")
    p.add_argument("--outline", type=Path, required=True)
    p.add_argument("--top", type=Path)
    p.add_argument("--bottom", type=Path)
    p.add_argument("--drill", type=Path, action="append", default=[])
    p.add_argument("--mode", choices=("isolation", "full-clear"), default="isolation")
    p.add_argument("--isolation-width", type=float, default=0.25, metavar="MM")
    p.add_argument("--flip-axis", choices=("vertical", "horizontal"), default="vertical")
    p.add_argument("--output", type=Path, required=True, metavar="DIRECTORY")
    p.set_defaults(func=export_pcb_svg)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
