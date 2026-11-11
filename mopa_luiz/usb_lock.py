"""Keep two OpenLaser processes from claiming the galvo together."""

import os
from pathlib import Path
from tempfile import gettempdir


def lock_galvo():
    directory = Path(os.environ.get("LOCALAPPDATA") or gettempdir()) / "OpenLaser"
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "galvo.lock").open("a+b")
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
        raise RuntimeError("Another OpenLaser operation owns the galvo") from exc
    return handle
