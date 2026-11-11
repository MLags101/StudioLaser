"""Keep two StudioLaser processes from claiming the galvo together."""

import os
from pathlib import Path
from tempfile import gettempdir


def lock_galvo(name: str = "galvo"):
    """Hold the lock for one laser; each laser has its own, so two can run side by side."""
    directory = Path(os.environ.get("LOCALAPPDATA") or gettempdir()) / "StudioLaser"
    directory.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name) or "galvo"
    handle = (directory / f"{safe}.lock").open("a+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError("Another StudioLaser operation owns the galvo") from exc
    return handle
