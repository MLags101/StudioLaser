"""Application state and workflow rules, with no widgets.

Everything that decides whether the laser may fire lives here so it can be
tested without a window: job edits, arming, the per-job verification flag,
the back-side gate and the hardware worker's events.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import base64
import math
import os
from pathlib import Path
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from ..cli import pyusb_probe
from ..desktop_hardware import HardwareWorker
from ..device_profile import DeviceProfile
from ..job_planner import Arming, MarkPlan, compile_frame, compile_mark, format_duration
from ..live import mock_mode
from ..patterns import crosshair, hole_targets
from ..presets import PresetLibrary
from ..workspace import (
    Artwork, Job, Placement, flipped_placement, job_from_board, new_layer, open_project, rectangle, save_project,
)

ARTWORK_SUFFIXES = {".svg", ".dxf", ".stl"}
FRAME_MODES = {
    "holes": "Outline and holes",
    "outline": "Outline",
    "box": "Bounding box",
    "design": "Whole design",
}
HOLE_RING_MM = 0.4          # radius of the red ring traced at each drilled hole
DESIGN_FRAME_LAP_S = 2.0     # a slower lap than this flickers too much to align by (and nears the frame timeout)
LINK_TEXT = {
    "connected": "Laser connected",
    "mock": "Simulated laser",
    "offline": "Laser offline",
    "multiple": "Two lasers found",
    "driver": "Wrong USB driver",
}


def state_directory() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    return (Path(root) if root else Path.home() / ".config") / "OpenLaser"


def probe_link() -> tuple[str, str]:
    if mock_mode():
        return "mock", "OPENMOPA_MOCK=1: commands go to a simulated controller, not a laser."
    report = pyusb_probe()
    count = len(report["devices"])
    if count == 1:
        return "connected", "One JCZ/LMC controller found on USB."
    if count > 1:
        return "multiple", "More than one JCZ/LMC controller is connected. Leave exactly one plugged in."
    if not report.get("available"):
        return "offline", report.get("note") or "The USB library could not be loaded."
    return "offline", "No JCZ/LMC controller found. Check the laser's power and USB cable."


def probe_driver() -> tuple[str, str] | None:
    """Slow OS-level check, run only while offline: is the laser there but on EZCAD's driver?"""
    from ..cli import detect_hint, os_usb_listing

    try:
        _, _, hits = os_usb_listing()
    except Exception:
        return None
    if hits:
        return "driver", detect_hint(True, False, True) or ""
    return None


def read_artwork(path: Path, width: float | None) -> Job:
    if path.suffix.lower() == ".svg":
        from ..svg_artwork import import_svg

        artwork = import_svg(path, width)
        vectors, fills = artwork.vectors, artwork.fills
    else:
        from ..importers import import_geometry

        payload = "data:application/octet-stream;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        item = import_geometry(path.name, payload)
        vectors = tuple(tuple(map(tuple, line)) for line in item["polylines"])
        fills = tuple(line for line in vectors if len(line) > 3 and line[0] == line[-1])
        if path.suffix.lower() == ".stl":
            fills = ()   # A mesh projection is overlapping triangles; hatching it is meaningless.
    return Job(path.stem, (Artwork(path.name, vectors, fills, new_layer()),), job_id=uuid4().hex)


class AppController(QObject):
    jobs_changed = Signal()          # queue contents, order, names or status
    selection_changed = Signal()     # a different job is current
    job_changed = Signal()           # current job's operations or geometry
    placement_changed = Signal()     # current job moved only
    plan_changed = Signal()          # toolpath preview/estimate appeared or cleared
    state_changed = Signal()         # anything that affects which actions are allowed
    profile_changed = Signal()
    presets_changed = Signal()
    message = Signal(str, str)       # text, level: info | success | warning | error
    progress = Signal(float, float, str)   # seconds done, seconds total, label
    size_required = Signal(object)   # Path of an SVG with no physical size

    def __init__(self, state_dir: Path | None = None, *, autostart: bool = True):
        super().__init__()
        self.state_dir = state_dir or state_directory()
        self.profile = DeviceProfile()
        self.presets = PresetLibrary(None)
        self.jobs: list[Job] = []
        self.current = -1
        self.project_path: Path | None = None
        self.revision = 0
        self.arming = Arming()
        self.verified: set[str] = set()
        self.plan: MarkPlan | None = None
        self.target = None            # red-light paths that replace the job outline
        self.frame_mode = "holes"
        self.link = "offline"
        self.link_note = ""
        self.state_unknown = False
        self.running_id: str | None = None
        self.worker = HardwareWorker()
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.tasks: list[tuple[str, int, object]] = []
        self.undo_stack: list[tuple[tuple[Job, ...], int, str, float]] = []
        self.redo_stack: list[tuple[tuple[Job, ...], int]] = []
        self.closing = False
        self._detect = None
        self._driver_check = None
        self._last_detect = 0.0
        self._last_driver = 0.0
        self._frame_dirty = False
        self._target_owned_loop = False
        self._snapshot = None
        self._mark_started = 0.0
        self._design_frame_warned = False
        self._held_import_said = False
        self._running_plan: MarkPlan | None = None
        self._recovery_dirty = False
        self._recovery_blocked = False
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self.tick)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.flush_recovery)
        if autostart:
            self._timer.start()

    # ---- start-up state: each file loads on its own so one bad file cannot take the others with it

    def load_state(self) -> None:
        path = self.state_dir / "active-machine.json"
        if path.exists():
            try:
                self.profile = DeviceProfile.open(path)
            except Exception as exc:
                self.say(f"The saved machine profile could not be read ({exc}). Open Machine Setup to rebuild it; "
                         "the file was left untouched.", "error")
        self.presets = PresetLibrary(self.state_dir / "recipes.json")
        if self.presets.error:
            self.say(self.presets.error, "error")
        path = self.state_dir / "recovery.json"
        if path.exists():
            try:
                self.jobs = open_project(path)
            except Exception as exc:
                aside = path.with_name(f"recovery-unreadable-{int(time.time())}.json")
                try:
                    path.replace(aside)
                    self.say(f"The recovered workspace could not be read ({exc}). It was kept as {aside.name}.", "error")
                except OSError:
                    self._recovery_blocked = True
                    self.say(f"The recovered workspace could not be read ({exc}) and will not be overwritten "
                             "this session.", "error")
        self.current = 0 if self.jobs else -1
        if any(job.status == "interrupted" for job in self.jobs):
            self.say("A job was interrupted last session. Check the board, then use Review to reset it.", "warning")
        self.profile_changed.emit()
        self.presets_changed.emit()
        self.jobs_changed.emit()
        self.selection_changed.emit()

    # ---- small accessors

    @property
    def job(self) -> Job | None:
        return self.jobs[self.current] if 0 <= self.current < len(self.jobs) else None

    @property
    def marking(self) -> bool:
        return self.running_id is not None or self.worker.action == "mark"

    @property
    def framing(self) -> bool:
        return self.worker.action in {"frame", "frame-loop"}

    @property
    def busy(self) -> bool:
        return self.worker.process is not None

    @property
    def planning(self) -> bool:
        return any(kind in {"preview", "arm"} for kind, _, _ in self.tasks)

    @property
    def online(self) -> bool:
        return self.link in {"connected", "mock"}

    def is_verified(self, job: Job | None = None) -> bool:
        job = job or self.job
        return bool(job) and job.job_id in self.verified

    def say(self, text, level="info") -> None:
        self.message.emit(str(text), level)

    # ---- which actions are allowed, and why not

    def blockers(self) -> dict[str, str]:
        """Reason each action is unavailable; an empty string means it is allowed."""
        job = self.job
        machine = ""
        if not self.profile.enabled:
            gaps = self.profile.missing()
            machine = ("Machine Setup still needs " + ", ".join(gaps)) if gaps else \
                "Enable the machine profile in Machine Setup"
        elif not self.online:
            machine = self.link_note or LINK_TEXT[self.link]
        elif self.state_unknown:
            machine = "The last operation ended in an unknown state. Inspect the machine, then confirm it is idle."
        edit = "Wait for the job to finish or press Stop" if self.marking else ""
        frame = machine or ("Import or select a job" if job is None else "") or \
            ("The laser is busy" if self.busy and self.worker.action != "frame-loop" else "")
        arm = machine or self._arm_reason()
        start = machine or ("Arm the job first" if not self.arming.active else "") or \
            ("Stop the red-light frame first" if self.busy else "")
        preview = "Import or select a job" if job is None else "Planning…" if self.planning else ""
        stop = "" if (self.busy or self.arming.active or self.planning) else "Nothing is running"
        return {"edit": edit, "frame": frame, "arm": arm, "start": start, "preview": preview, "stop": stop}

    def _arm_reason(self) -> str:
        job = self.job
        if job is None:
            return "Import or select a job"
        if job.status != "ready":
            return f"This job is {job.status}. Review it before running it again."
        if self.busy:
            return "Stop the red-light frame first" if self.framing else "The laser is busy"
        if self.planning:
            return "Planning…"
        if job.side == "bottom" and self._top_unfinished(job):
            return "Complete the top side of this board first"
        if job.side and not self.is_verified(job):
            return "Tick “Side and alignment checked” for this board side"
        return ""

    # ---- edits (all go through _commit so undo, arming and recovery stay consistent)

    def _commit(self, jobs: list[Job], label: str, *, current: int | None = None,
                moved_only: bool = False, merge: str = "") -> None:
        before = (tuple(self.jobs), self.current, merge, time.monotonic())
        last = self.undo_stack[-1] if self.undo_stack else None
        if not (merge and last and last[2] == merge and before[3] - last[3] < 1.0):
            self.undo_stack.append(before)
            del self.undo_stack[:-100]
        else:   # Keep the earlier snapshot; refresh its time so a long drag stays one step.
            self.undo_stack[-1] = (last[0], last[1], merge, before[3])
        self.redo_stack.clear()
        self._apply(jobs, self.current if current is None else current, moved_only=moved_only)

    def _apply(self, jobs: list[Job], current: int, *, moved_only: bool = False) -> None:
        old = {job.job_id: job for job in self.jobs}
        previous = self.job.job_id if self.job else None
        self.jobs = list(jobs)
        self.current = min(current, len(self.jobs) - 1) if self.jobs else -1
        # A job whose geometry or position changed is no longer verified against the board.
        for job in self.jobs:
            was = old.get(job.job_id)
            if was is not None and (was.placement != job.placement or was.boundary != job.boundary):
                self.verified.discard(job.job_id)
        self.verified &= {job.job_id for job in self.jobs}
        self.invalidate()
        self._recovery_dirty = True
        self._save_timer.start()
        now = self.job.job_id if self.job else None
        if moved_only and now == previous:
            self.placement_changed.emit()
        else:
            if [j.job_id for j in jobs] != list(old) or any(
                    old[j.job_id].status != j.status or old[j.job_id].name != j.name for j in jobs if j.job_id in old):
                self.jobs_changed.emit()
            if now != previous:
                self.selection_changed.emit()
            self.job_changed.emit()
        self.state_changed.emit()

    def invalidate(self) -> None:
        """Any change to what would be marked cancels the arm and the stale preview."""
        self.revision += 1
        self.arming.clear()
        self._frame_dirty = True
        if self.plan is not None:
            self.plan = None
            self.plan_changed.emit()

    def undo(self) -> None:
        if self.undo_stack and not self.blockers()["edit"]:
            jobs, current, _, _ = self.undo_stack.pop()
            self.redo_stack.append((tuple(self.jobs), self.current))
            self._apply(list(jobs), current)
            self.jobs_changed.emit()

    def redo(self) -> None:
        if self.redo_stack and not self.blockers()["edit"]:
            jobs, current = self.redo_stack.pop()
            self.undo_stack.append((tuple(self.jobs), self.current, "", time.monotonic()))
            self._apply(list(jobs), current)
            self.jobs_changed.emit()

    def can_edit(self) -> bool:
        reason = self.blockers()["edit"]
        if reason:
            self.say(reason, "warning")
        return not reason

    def select(self, index: int) -> None:
        if index == self.current or not 0 <= index < len(self.jobs) or self.marking:
            return
        self.current = index
        self.clear_target()
        self.invalidate()
        self.selection_changed.emit()
        self.job_changed.emit()
        self.state_changed.emit()

    def add_jobs(self, jobs: list[Job]) -> None:
        if jobs:
            self._commit(self.jobs + list(jobs), "Add", current=len(self.jobs))

    def remove_job(self, index: int | None = None) -> None:
        index = self.current if index is None else index
        if 0 <= index < len(self.jobs) and self.can_edit():
            self._commit(self.jobs[:index] + self.jobs[index + 1:], "Remove", current=max(0, index - 1))

    def move_job(self, delta: int) -> None:
        target = self.current + delta
        if self.job and 0 <= target < len(self.jobs) and self.can_edit():
            jobs = list(self.jobs)
            jobs[self.current], jobs[target] = jobs[target], jobs[self.current]
            self._commit(jobs, "Reorder", current=target)

    def replace_job(self, job: Job, label: str, *, moved_only: bool = False, merge: str = "") -> None:
        if self.job is None or not self.can_edit():
            return
        jobs = list(self.jobs)
        jobs[self.current] = job
        self._commit(jobs, label, moved_only=moved_only, merge=merge)

    def set_placement(self, placement: Placement, *, merge: str = "move") -> None:
        if self.job is None or placement == self.job.placement:
            return
        try:
            placement.validate()
        except ValueError as exc:
            self.say(exc, "error")
            return
        self.replace_job(replace(self.job, placement=placement), "Move", moved_only=True, merge=merge)

    def nudge(self, dx=0.0, dy=0.0, angle=0.0) -> None:
        """Move by (dx, dy) mm and turn by ``angle`` degrees about the design's own centre."""
        job = self.job
        if job is None:
            return
        p = job.placement
        moved = replace(p, angle=(p.angle + angle + 180) % 360 - 180)
        box = job.bounds(placed=False)
        if angle and box:
            # Keep the design's centre where it is, so turning does not swing it across the field.
            centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            before, after = p.apply(centre), moved.apply(centre)
            moved = replace(moved, x=p.x + before[0] - after[0], y=p.y + before[1] - after[1])
        self.set_placement(replace(moved, x=round(moved.x + dx, 4), y=round(moved.y + dy, 4)), merge="nudge")

    def set_layer(self, index: int, layer, *, merge: str = "") -> None:
        job = self.job
        if job is None or not 0 <= index < len(job.objects) or layer == job.objects[index].layer:
            return
        objects = list(job.objects)
        objects[index] = replace(objects[index], layer=layer)
        self.replace_job(replace(job, objects=tuple(objects)), "Settings", merge=merge)

    def review(self) -> None:
        """Operator confirms an interrupted or finished job may be run again."""
        job = self.job
        if job and job.status in {"interrupted", "complete"} and self.can_edit():
            self.replace_job(replace(job, status="ready"), "Review")
            self.jobs_changed.emit()

    def set_verified(self, checked: bool) -> None:
        job = self.job
        if job is None:
            return
        if checked:
            if job.side == "bottom" and self._top_unfinished(job):
                self.say("Finish the top side first, then flip the board and check the bottom side's alignment.",
                         "warning")
                self.state_changed.emit()
                return
            self.verified.add(job.job_id)
        else:
            self.verified.discard(job.job_id)
            self.arming.clear()
        self.state_changed.emit()

    # ---- machine profile and presets

    def set_profile(self, profile: DeviceProfile) -> bool:
        if self.busy:
            self.say("Stop the laser before changing the machine profile", "warning")
            return False
        try:
            profile.save(self.state_dir / "active-machine.json")
        except Exception as exc:
            self.say(f"The machine profile was not saved: {exc}", "error")
            return False
        self.profile = profile
        self.invalidate()
        if self.verified:
            self.verified.clear()
            self.say("The machine profile changed, so every “Side and alignment checked” tick was cleared. "
                     "Frame and check the board again.", "warning")
        self.profile_changed.emit()
        self.job_changed.emit()
        self.state_changed.emit()
        return True

    # ---- imports run off the UI thread

    def import_artwork(self, paths: list[Path], width: float | None = None) -> None:
        if not paths or not self.can_edit():
            return
        for path in paths:
            self.tasks.append(("artwork", self.revision, self.executor.submit(self._read_artwork, path, width)))
        self.say(f"Importing {len(paths)} file(s)…")

    @staticmethod
    def _read_artwork(path, width):
        from ..svg_artwork import SvgSizeRequired

        try:
            return read_artwork(path, width)
        except SvgSizeRequired:
            return ("size", path)
        except Exception as exc:
            raise ValueError(f"{path.name}: {exc}") from exc

    def import_board(self, inputs: dict, *, mode, isolation_width: float, flip_axis: str, kerf_mm: float) -> None:
        if not self.can_edit():
            return
        layers = self.presets.defaults()
        name = inputs["outline"][0].stem if inputs.get("outline") else "Board"
        for suffix in ("-Edge_Cuts", "_Edge_Cuts", "-outline", "_outline"):
            name = name.removesuffix(suffix)

        def prepare():
            from ..pcb.board import prepare_board

            sides = prepare_board(inputs, mode=mode, isolation_width=isolation_width,
                                  flip_axis=flip_axis, kerf_mm=kerf_mm)
            board_id = uuid4().hex
            return [job_from_board(name, side, board_id, layers) for side in sides]

        self.tasks.append(("board", self.revision, self.executor.submit(prepare)))
        self.say("Preparing copper, outline and drill geometry…")

    # ---- projects and recovery

    def flush_recovery(self, *, strict: bool = False) -> None:
        if not self._recovery_dirty or self._recovery_blocked:
            return
        try:
            save_project(self.state_dir / "recovery.json", self.jobs)
            self._recovery_dirty = False
        except Exception as exc:
            if strict:
                raise
            self.say(f"The workspace could not be saved for recovery: {exc}", "warning")

    def open_project(self, path: Path) -> None:
        if not self.can_edit():
            return
        if self.busy:
            self.say("Stop the red-light frame before opening a project.", "warning")
            return
        try:
            jobs = open_project(path)
        except Exception as exc:
            self.say(f"{path.name} could not be opened: {exc}", "error")
            return
        self.project_path = path
        self._commit(jobs, "Open", current=0)
        self.jobs_changed.emit()
        self.selection_changed.emit()
        self.say(f"Opened {path.name}", "success")

    def save_project(self, path: Path | None = None) -> bool:
        path = path or self.project_path
        if path is None:
            return False
        try:
            save_project(path, self.jobs)
        except Exception as exc:
            self.say(f"The project was not saved: {exc}", "error")
            return False
        self.project_path = path
        self.say(f"Saved {path.name}", "success")
        return True

    # ---- preview, arm, start, stop

    def preview(self) -> None:
        self._plan(arm=False)

    def arm(self) -> None:
        self._plan(arm=True)

    def _plan(self, *, arm: bool) -> None:
        reason = self.blockers()["arm" if arm else "preview"]
        if reason:
            self.say(reason, "warning")
            return
        self.arming.clear()
        self.tasks.append(("arm" if arm else "preview", self.revision,
                           self.executor.submit(compile_mark, self.job, self.profile, live=arm)))
        self.say("Planning toolpaths…")
        self.state_changed.emit()

    def start(self) -> None:
        reason = self.blockers()["start"] or self._arm_reason()
        if reason:
            self.say(reason, "warning")
            return
        try:
            plan = self.arming.take(self.revision)
        except PermissionError as exc:
            self.say(exc, "warning")
            return
        job = self.job
        self.running_id = job.job_id
        self.jobs[self.current] = replace(job, status="running")
        # Undo must never bring back a pre-run status, so history ends where a run begins.
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._recovery_dirty = True
        try:
            self.flush_recovery(strict=True)
            if self.project_path:
                save_project(self.project_path, self.jobs)
            self._running_plan = plan
            self.worker.start("mark", self.profile, plan)
        except Exception as exc:
            self._finish("interrupted")
            self.say(f"The job did not start: {exc}", "error")
            return
        self.say("Connecting to the laser…")
        self.jobs_changed.emit()
        self.state_changed.emit()

    def _finish(self, status: str) -> None:
        if self.running_id is None:
            return
        finished = self.running_id
        self.jobs = [replace(job, status=status) if job.job_id == finished else job for job in self.jobs]
        self.running_id = None
        self.verified.discard(finished)
        if status == "complete":
            self._seed_other_side(finished)
        self._recovery_dirty = True
        try:
            self.flush_recovery(strict=True)
            if self.project_path:
                save_project(self.project_path, self.jobs)
        except Exception as exc:
            self.say(f"The job status could not be saved: {exc}", "error")
        self.jobs_changed.emit()
        self.job_changed.emit()

    def _top_unfinished(self, job: Job) -> bool:
        return any(other.board_id == job.board_id and other.side == "top" and other.status != "complete"
                   for other in self.jobs)

    def _seed_other_side(self, finished_id: str) -> None:
        """After the top side, start the bottom where an in-place flip would put it."""
        top = next((job for job in self.jobs if job.job_id == finished_id), None)
        if top is None or top.side != "top":
            return
        for index, job in enumerate(self.jobs):
            if job.board_id == top.board_id and job.side == "bottom" and job.placement == Placement():
                self.jobs[index] = replace(job, placement=flipped_placement(top.placement))
                self.verified.discard(job.job_id)
                self.say("Top side complete. Flip the board, select the bottom side and register it on the holes "
                         "before running. Its position was pre-set from the top side as a starting point.", "success")

    def frame(self, continuous: bool = True) -> None:
        reason = self.blockers()["frame"]
        if reason:
            self.say(reason, "warning")
            return
        if self.busy:
            return
        try:
            paths = compile_frame(self.frame_paths(), self.profile)
            self.arming.clear()
            self.worker.start("frame-loop" if continuous else "frame", self.profile, paths)
        except Exception as exc:
            self.say(f"Framing did not start: {exc}", "error")
            return
        self._frame_dirty = False
        self.say("Connecting to the laser for the red-light frame…")
        self.state_changed.emit()

    def frame_paths(self):
        """What the red light traces now, in placed millimetres."""
        if self.target:
            return self.target
        job = self.job
        if job is None:
            return ()
        mode = self.frame_mode
        if mode == "box" or (mode == "outline" and not job.boundary):
            box = job.bounds()
            return (rectangle(*box),) if box else ()
        paths = job.frame()
        if mode == "holes" and job.references:
            paths = paths + hole_targets([job.placement.apply(point) for point in job.references],
                                         radius=HOLE_RING_MM, segments=16)
        elif mode == "design":
            design = tuple(path for artwork in job.objects if artwork.layer.output
                           for path in job.placement.paths(artwork.paths))
            length = sum(math.dist(a, b) for path in design for a, b in zip(path, path[1:]))
            if length / self.profile.frame_speed > DESIGN_FRAME_LAP_S:
                if not self._design_frame_warned:
                    self._design_frame_warned = True
                    self.say(f"The whole design is {length / 1000:.1f} m of path; the red light would trace it too "
                             "slowly to see. Framing the outline and holes instead.", "warning")
                return paths + hole_targets([job.placement.apply(p) for p in job.references],
                                            radius=HOLE_RING_MM, segments=16)
            paths = paths + design
        return paths

    def set_frame_mode(self, mode: str) -> None:
        if mode in FRAME_MODES and mode != self.frame_mode:
            self.frame_mode = mode
            self._design_frame_warned = False
            self._frame_dirty = True
            self.state_changed.emit()

    def toggle_frame(self) -> None:
        if self.worker.action == "frame-loop":
            self.stop()
        else:
            self.frame(True)

    def stop(self) -> None:
        """Always safe to call. Stopping never clears the alignment check."""
        self.arming.clear()
        self.tasks = [task for task in self.tasks if task[0] not in {"preview", "arm"}]
        self._target_owned_loop = False
        if self.worker.process:
            self.worker.stop()
            self.say("Stopping the laser…", "warning")
        self.state_changed.emit()

    def show_target(self, paths) -> None:
        """Point the red light at something other than the job outline (a registration target)."""
        if paths == self.target:
            return
        self.target = paths
        self._frame_dirty = True
        if paths and not self.busy and not self.blockers()["frame"]:
            self.frame(True)
            self._target_owned_loop = self.worker.action == "frame-loop"
        self.job_changed.emit()

    def show_point(self, point) -> None:
        self.show_target(crosshair(point))

    def clear_target(self) -> None:
        if self.target is None:
            return
        self.target = None
        self._frame_dirty = True
        if self._target_owned_loop and self.worker.action == "frame-loop":
            self.worker.stop()
        self._target_owned_loop = False
        self.job_changed.emit()

    def acknowledge(self) -> None:
        """Operator has inspected the machine after an operation ended in an unknown state."""
        if not self.busy and self.state_unknown:
            self.state_unknown = False
            self.invalidate()
            self.say("Machine state confirmed idle.", "success")
            self.state_changed.emit()

    # ---- event pump

    def tick(self) -> None:
        self._pump_worker()
        self._pump_tasks()
        self._pump_detect()
        if self.worker.action == "frame-loop" and self._frame_dirty:
            try:
                self._frame_dirty = not self.worker.update_frame(compile_frame(self.frame_paths(), self.profile))
            except ValueError as exc:
                self._frame_dirty = False
                self.stop()
                self.say(f"Framing stopped: {exc}", "warning")
        snapshot = (tuple(self.blockers().items()), self.worker.action, self.link, self.link_note,
                    int(self.arming.remaining * 4), self.state_unknown)
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self.state_changed.emit()

    def _pump_worker(self) -> None:
        for kind, data in self.worker.poll():
            if kind == "status":
                self.say(data)
            elif kind == "running":
                if data == "mark":
                    self._mark_started = time.monotonic()
                    total = self._running_plan.seconds
                    self.progress.emit(0.0, total, "Engraving")
                    self.say(f"Engraving — about {format_duration(total)}")
                else:
                    self.say("Red-light frame running")
            elif kind == "progress":
                done, total = data
                plan = self._running_plan
                seconds = sum(batch.seconds for batch in plan.batches[:done])
                name = plan.batches[min(done, total - 1)].name
                self.progress.emit(seconds, plan.seconds, name)
            elif kind == "finished":
                if data == "mark":
                    took = time.monotonic() - self._mark_started
                    self.progress.emit(1.0, 1.0, "")
                    self._finish("complete")
                    self.say(f"Job complete in {format_duration(took)}. Check the board before continuing.", "success")
                else:
                    self.say("Red-light frame finished")
            elif kind in {"error", "unknown", "stopped", "timeout"}:
                self.arming.clear()
                if kind != "timeout":
                    if self.running_id:
                        self.progress.emit(0.0, 0.0, "")
                    self._finish("interrupted")
                if kind in {"unknown", "error", "timeout"}:
                    self.state_unknown = True
                self.say(data, "warning" if kind == "stopped" else "error")
            elif kind == "idle":
                self._target_owned_loop = False
                self._last_detect = 0.0
            self.state_changed.emit()

    def _pump_tasks(self) -> None:
        if not self.marking:
            self._held_import_said = False
        for task in [task for task in self.tasks if task[2].done()]:
            self.tasks.remove(task)
            kind, revision, future = task
            try:
                result = future.result()
            except Exception as exc:
                self.say(exc, "error")
                self.state_changed.emit()
                continue
            if kind in {"artwork", "board"} and self.marking:
                # The job list is locked while engraving; keep the result and add it afterwards.
                self.tasks.append(task)
                if not self._held_import_said:
                    self._held_import_said = True
                    self.say("The import finished during engraving; it will be added when the job ends.")
                continue
            if kind == "artwork":
                if isinstance(result, tuple):
                    self.size_required.emit(result[1])
                else:
                    self.add_jobs([result])
                    self.say(f"Imported {result.name}", "success")
            elif kind == "board":
                if result:
                    self.add_jobs(result)
                    self.say(f"Prepared {len(result)} board side(s). Place and register each side before running.",
                             "success")
                    box = result[0].bounds()
                    if box and (box[2] - box[0] > self.profile.width or box[3] - box[1] > self.profile.height):
                        self.say(f"This board is {box[2] - box[0]:.1f} × {box[3] - box[1]:.1f} mm, larger than the "
                                 f"{self.profile.width:g} × {self.profile.height:g} mm field. It cannot be engraved "
                                 "in one placement.", "error")
            elif revision != self.revision:
                # The job changed while it was being planned; say so instead of leaving "Planning…" up.
                self.say("The job changed during planning. Preview or arm it again.", "warning")
            else:
                self.plan = result
                self.plan_changed.emit()
                summary = (f"{result.paths:,} paths, about {format_duration(result.seconds)}")
                if kind == "arm":
                    reason = self._arm_reason()
                    if reason:
                        self.say(reason, "warning")
                    else:
                        self.arming.set(revision, result)
                        self.say(f"Armed for {self.arming.seconds} seconds — {summary}. Press Start.", "success")
                else:
                    self.say(f"Preview ready — {summary}")
            self.state_changed.emit()

    def _pump_detect(self) -> None:
        if self._detect is not None and self._detect.done():
            try:
                link, note = self._detect.result()
            except Exception as exc:
                link, note = "offline", str(exc)
            self._detect = None
            if link == "offline" and self.link == "driver":
                link, note = "driver", self.link_note   # keep the driver diagnosis until the device is usable
            if self.online and link not in {"connected", "mock"}:
                self.arming.clear()
                self.worker.stop()
            if (link, note) != (self.link, self.link_note):
                self.link, self.link_note = link, note
        if self._driver_check is not None and self._driver_check.done():
            try:
                found = self._driver_check.result()
            except Exception:
                found = None
            self._driver_check = None
            if self.link in {"offline", "driver"}:
                self.link, self.link_note = found or ("offline", "No JCZ/LMC controller found. Check the laser's "
                                                                  "power and USB cable.")
        now = time.monotonic()
        if self._detect is None and not self.busy and now - self._last_detect > 2:
            self._last_detect = now
            self._detect = self.executor.submit(probe_link)
        if self._driver_check is None and self.link in {"offline", "driver"} and now - self._last_driver > 12:
            self._last_driver = now
            self._driver_check = self.executor.submit(probe_driver)

    # ---- shutdown

    def shutdown(self) -> bool:
        """Return True when it is safe to close now; otherwise a stop was requested."""
        self._save_timer.stop()
        self.flush_recovery()
        if self.worker.process:
            self.closing = True
            self.stop()
            return False
        self._timer.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        return True
