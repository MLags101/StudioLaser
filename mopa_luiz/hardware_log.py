"""Where hardware operations are logged: Documents\\StudioLaser\\Logs, one file per day.

Every real laser operation writes its settings, galvoplotter's USB events, timings and outcome
here, so a connection problem can be diagnosed from the file (Help → Open Log Folder).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from hashlib import sha256
import os
from pathlib import Path
import sys

KEEP_DAYS = 14


def build_id() -> str:
    """Identify the running bundle or the hardware code in a source checkout."""
    digest = sha256()
    paths = [Path(sys.executable)] if getattr(sys, "_MEIPASS", None) else [
        Path(__file__).with_name(name) for name in ("live.py", "desktop_hardware.py", "usb_backend.py")
    ]
    try:
        for path in paths:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    except OSError:
        return "unavailable"
    return digest.hexdigest()[:16]


def documents_folder() -> Path:
    """The user's Documents folder, following OneDrive or other redirection on Windows."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            buffer = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_PERSONAL = 5 (Documents), SHGFP_TYPE_CURRENT = 0
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0 and buffer.value:
                return Path(buffer.value)
        except Exception:
            pass
    return Path.home() / "Documents"


def log_folder() -> Path:
    override = os.environ.get("STUDIOLASER_LOG_DIR")
    return Path(override) if override else documents_folder() / "StudioLaser" / "Logs"


def log_file(day: date | None = None) -> Path:
    return log_folder() / f"hardware-{(day or date.today()).isoformat()}.log"


def prune(folder: Path, keep_days: int = KEEP_DAYS) -> None:
    oldest = date.today() - timedelta(days=keep_days)
    for path in folder.glob("hardware-*.log"):
        try:
            if date.fromisoformat(path.stem.removeprefix("hardware-")) < oldest:
                path.unlink()
        except (ValueError, OSError):
            continue


class HardwareLog:
    """Append-only, best effort: a log that cannot be written never affects the laser."""

    def __init__(self, enabled: bool = True):
        self.file = None
        if not enabled:
            return
        try:
            folder = log_folder()
            folder.mkdir(parents=True, exist_ok=True)
            prune(folder)
            self.file = open(log_file(), "a", encoding="utf-8", buffering=1)
        except OSError:
            self.file = None

    def __call__(self, text) -> None:
        if self.file is not None:
            try:
                stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                for line in str(text).rstrip().splitlines() or [""]:
                    self.file.write(f"{stamp} [{os.getpid()}] {line}\n")
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                pass
            self.file = None


def recent_lines(count: int = 200) -> list[str]:
    """The last ``count`` lines across the newest log files."""
    folder = log_folder()
    lines: list[str] = []
    try:
        files = sorted(folder.glob("hardware-*.log"))
    except OSError:
        return lines
    for path in reversed(files):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        lines = [f"--- {path.name}"] + text + lines
        if len(lines) >= count:
            break
    return lines[-count:]
