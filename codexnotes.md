# StudioLaser implementation and handoff notes

## Update, 12 November 2026 (evening): what the B6's log showed, and the USB fix

`hardware-2026-11-12.log` (in the repo root, from the laser PC) recorded:
- **First failure:** one frame session ran 74 s, then a transfer timed out (`Errno 10060`).
- **What followed:** galvoplotter's retry reset the USB device and reopened it. Every transfer after that timed out, about 200 attempts over 75 s, until the laser was power-cycled.
- **After the power cycle:** the next frame session hung again after 4 s, and the board was then unresponsive on connect (serial number `(-1, -1, -1, -1)`, still reported as "connected").

Likely mechanism:
1. The original's wait for READY gave up after 3 s and wrote the list packet anyway. A long red-light lap can keep the buffer full for longer than that.
2. galvoplotter gives a transfer 100 ms.
3. On a timeout, galvoplotter resets the USB device mid-list, and the controller never recovers.

Changes, all in `live.py`:
- **Waits** raise after 15 s (`WAIT_TIMEOUT_S`) instead of writing blind; while stopping they give way at once.
- **USB timeout** is 2 s.
- **No reset-and-retry:** `UsbMonitor` passes `attempt=4` to galvoplotter. The first failed transfer sets `_openlaser_link_failed`, stops the job, and logs the last 40 transfers with their timings.
- **Connect** fails if any transfer failed during it, or if the version read is all `-1`.
- **Frame statistics:** every 10 s the frame logs its laps, USB transfers, slowest transfer and longest wait.
- **Connection Test:** the "old USB handling" option (100 ms with reset-and-retry) reproduces the old behaviour on purpose, for comparison.

## Update, 12 November 2026 (later): laser driving returned to the original program's way

Supersedes the "Red-light frame dropped the USB link" paragraph below. After that change the laser stopped
framing at all ("running", nothing happens, then *not responding*) with the factory .cor selected.

**Root cause analysis.** Compared with the original program (`6aed8ff:mopa_luiz/live.py`), the overhaul had changed
how galvoplotter is driven in nine ways, none of them tested on hardware:
1. It uploaded the factory .cor table on every connect, which EZCAD itself had switched off on this machine.
2. Its waits raised instead of giving up quietly after 3 s.
3. It turned off galvoplotter's USB retries.
4. It built lists on its own thread instead of the spooler.
5. It ended, restarted or aborted the frame list repeatedly.
6. It used a custom teardown.
7. It used MarkParam delays.
8. It used different speeds.
9. It killed the worker process 8 s after Stop.

All nine are now back to the original:
- **`live.py`:** the original GalvoController arguments, plus `machine_index`. It has the original `apply_wait_timeouts` (a spooler thread waiting at Stop raises `ConnectionAbortedError` straight away) and the original `stop_controller`, plus `stop_with_watchdog`.
- **`desktop_hardware.py`:** every operation is a spooler job (`controller.submit`):
  - frame once is the original `frame_polylines`;
  - the continuous frame is the original `FrameLoop` job, reading the latest frame geometry each lap, with no abort/restart;
  - a mark is one `with c.marking()` job per planned batch.
- **Retries:** galvoplotter's own, for marks too (owner's choice).
- **Parent process:** waits 15 s before killing a worker that won't stop. A worker whose stop hangs exits itself.

**Lens correction.** The board always gets a blank table, as in the original. The factory table is applied in software by `correction.CorTable`, inside `DeviceProfile.galvo_point`:
- **Interpolation:** bilinear, on nodes 1024 galvo units apart.
- **Axes:** in `jcz15.cor` the inner index is galvo X and the outer index is galvo Y, and the offsets point outward.
- **Direction:** `DeviceProfile.cor_sign`, shown as "Reverse the .cor table's direction" in Calibrate Lens. Confirm it once by marking the grid.

**Driver settings.** `DeviceProfile.driver` is `"original"` by default; `"ezcad"` also sends the MarkParam delays and profile speeds (Machine Setup → Motion).

**Diagnostics.**
- **Logs:** `Documents\StudioLaser\Logs\hardware-YYYY-MM-DD.log`, kept for 14 days. `STUDIOLASER_LOG_DIR` overrides the location.
- **Help menu:** Open Log Folder, Copy Problem Report.
- **Machine → Connection Test:** find, connect, red dot, frame a 20 mm square for N s, stop, reconnect. Checkboxes switch the overhaul's .cor upload, EZCAD timing, or no-retries back on, so each can be confirmed on the laser.

**Not yet run on the laser.** The tests (170) drive galvoplotter's real controller on a recording connection. The frozen builds start, but the frozen worker was not driven.

## Update, 12 November 2026: framing drop-outs, flexible jobs, quick size calibration

**Red-light frame dropped the USB link after ~20 s (needed a power cycle).** The frame loop ended and re-ran the
whole list every lap, about 20 times a second, each lap with reset, execute, 10 ms busy polling, fiber MO off, port
write and a mark-time read: roughly 10,000 USB transactions in 20 s, with galvoplotter's retries disabled so the first
100 ms read timeout was fatal. The original fork used galvoplotter's spooler, which keeps one list open and tops it up.
`desktop_hardware.run_frame` now does the same: one continuous red-light list, throttled by the board's READY bit;
a moved frame aborts the queued laps and restarts the list (at most every 0.15 s while dragging). Red-light actions
keep galvoplotter's USB retries; marks still fail on the first error. **Not yet run on the laser.**
Every real hardware operation now appends USB events and its outcome to `%LOCALAPPDATA%\StudioLaser\hardware.log`
(rotated at 2 MB). If a drop happens again, that file says where.

**Flexible jobs.** `artwork_files.py` reads one SVG, DXF, STL or single Gerber file as an operation. *Add File to Job*
(Jobs panel, File menu, Ctrl+Shift+A, or the PCB import dialog's "Add to … as operations" button) appends files to
the selected job. A Gerber added to a board side is placed by the side's recorded fabrication origin and flip
(`Job.origin`, `Job.flip`, new and saved in projects; boards imported earlier get centred with a warning). Importing
several artwork files asks whether to make one job. Every operation can switch lines/fill, take any preset (matching
ones listed first), and has a Type. Right-click an operation to rename, duplicate, reorder or remove it.

**Lens calibration.** Choosing *Factory correction file* in Calibrate Lens now asks for the `.cor` when the profile has
none (before, it refused and pointed back at Machine Setup, where only *Read factory files* stored it). Machine Setup
also stores a `.cor` as soon as it is browsed, and on Save. Changing the base is refused with a message while the
laser is busy. New *Quick size* tab: trace or mark a square, enter the measured X and Y, Adjust; it sets
`DeviceProfile.trim_x/trim_y` (new trim = old × target ÷ measured), applied after the measured calibration. It can
change while the red light runs, so the trace updates live. A grid calibration is fitted relative to calibration and
trim together and resets the trim.

## Update, 11 November 2026: overhaul

This section supersedes anything below that conflicts with it.

**Removed.** The browser interface (`studiolaser ui`, `ui.py`), G-code export, GRBL machine profiles and their tests. The browser path let any web page start a mark, and its Stop on Windows killed the worker without aborting the controller. The desktop app is the only interface; bare `studiolaser` starts it.

**New layout.**

| Module | Role |
| --- | --- |
| `mopa_luiz/gui/controller.py` | All state and workflow rules, no widgets: edits, undo, arming, per-job alignment check, back-side gate, hardware events, recovery. |
| `mopa_luiz/gui/main_window.py`, `panels.py`, `canvas.py` | Window, docks (Jobs, Settings, Activity), canvas with grid, rulers and hole picking. |
| `mopa_luiz/gui/dialogs.py`, `machine.py` | PCB import, hole registration, presets, material test; Machine Setup and Calibrate Lens. |
| `mopa_luiz/gui/theme.py` | Dark and light tokens, stylesheet, painted icons. |
| `mopa_luiz/correction.py` | `.cor` parsing, EZCAD parametric field, measured `FieldMap` fit. |
| `mopa_luiz/sources.py` | JPT M7 60 W pulse widths with cut-off and maximum frequency. |
| `mopa_luiz/presets.py`, `patterns.py` | Preset library and built-in starting points; test matrix, kerf ladder, calibration grid, focus test. |

**Findings from the real factory files** (`markcfg7`, `jcz15.cor`):
- `jcz15.cor` is a `JCZ_COR_2_1` file: 36-byte header, 65 × 65 int32 pairs, 4-byte trailer. The old exact-size check rejected it.
- `markcfg7` has `ENABLECORFILE=0`. EZCAD on this machine was correcting the lens with `GALVODISTOR0/1 = 0.9705 / 0.9785`, not with the `.cor` table. Field size is 167.5 mm.
- `markcfg7` lists no pulse widths, so the old import could never produce a profile that was allowed to run.

**Lens correction design.** Only a factory `.cor` is ever uploaded to the controller, unmodified. Everything else is corrected in software in `DeviceProfile.galvo_point`: EZCAD's parametric field (an approximation; its formula is unpublished) and the measured calibration polynomial. Long lines are subdivided so they follow the correction. Which base is right for this machine is still open: the calibration dialog lets the operator mark a grid with each and compare.

**Decisions made along the way.**
- Tests that need the real factory files skip when `tests/fixtures/b6_60w/` is absent.
- Boards start with built-in presets applied (100 % power for copper) instead of zero power. Marking still needs the alignment tick, Arm and Start.
- Headerless Excellon files raise an error when inch and metric readings both fit the outline, instead of guessing.

**Second pass (same day).**
- *Console window flashing:* the offline USB check ran `pnputil` every 12 s, and a windowed app gets a console
  window for each child process unless it asks for none. `cli.run_command` now passes `CREATE_NO_WINDOW`, and
  `desktop.windowless_workers` starts hardware workers under `pythonw.exe` when the app has no console.
- *Framing for alignment:* the red light can trace the outline and a 0.4 mm ring at each drilled hole (the
  default), the outline only, the bounding box, or the whole design (refused if one lap would take over 2 s).
  The canvas draws the same trace dashed in red. Moves while framing are picked up on the next lap. Rotation
  turns about the design's own centre. The Settings panel has a jog pad with a step size.
- *EZCAD pen library:* `MarkParam.lib` (read by `mopa_luiz/markparam.py`) supplies the timing EZCAD used on this
  machine (laser on -300 us, off 100, corner 100, jump settle 500/500 us, jump speed 4000 mm/s), replacing
  galvoplotter's defaults (jump settle 8 us). Its named sets PCBCut, SMR, PCBClean, PCBEn become presets.
- *Review fixes:* bottom side auto-placement clears its alignment tick, and a bottom side cannot be ticked before
  the top is done. Esc stops the laser even inside modal dialogs. Each hardware cleanup step runs separately with
  its own wait, and a failed one reports an unknown state. Profile changes clear alignment ticks. Saving the
  red-light alignment never stops an engraving. Jobs with no operations are refused. Imports that finish during a
  mark are held until it ends.
- The factory files (`markcfg7`, `jcz15.cor`, `MarkParam.lib`) and the reference manuals are committed, with the
  owner's agreement.

**Third pass.**
- Renamed to **StudioLaser** (commands `studiolaser`, `studiolaser-desktop`; `openlaser`, `openlaser-desktop` and
  `openmopa` remain as aliases). The settings folder moves to `%LOCALAPPDATA%\StudioLaser`, copied once from
  `OpenLaser`; window settings are copied the same way; `.openlaser.json` projects still open. The Python package
  is still `mopa_luiz`.
- Fixed a crash when ticking an operation in the job tree (the tree rebuilt itself inside the tick-box signal).
  Panels and the canvas now rebuild after the event that asked for it. Bounds use a cached convex hull, recovery
  saves run on a background thread, and the grid draws in one call.
- Registration can fit to the board edge (edge cuts): each point is fitted only to the edge it was picked on,
  by Gauss-Newton on point-to-edge distance, and under-determined point sets are refused.
- The connection chip is a laser picker, keyed by USB bus and port; an unplugged choice is refused, never
  substituted.

**Not done.** Per-file preview in the PCB import dialog, snapping to drill centres while dragging, a saved fixture origin for a corner jig, native KiCad board import.

**Verification.** `python -m pytest tests -q`: 147 pass in a venv built with `pip install -e ".[desktop,freeze,dev]"`; `test_bundled_libusb_is_used_when_the_system_has_none` fails only where the `libusb-package` wheel is not installed. The app was driven end to end against the simulated controller (`OPENMOPA_MOCK=1`): import, preview, arm, start, completion, back-side gate, live frame updates, Stop. **Nothing has been run on the laser.** Use [docs/commissioning.md](docs/commissioning.md).

## State before the overhaul

StudioLaser now has a Windows-first PySide6 desktop workspace for the makerspace’s ComMarker B6 MOPA and its CNC-first, double-sided PCB workflow. The CNC cuts the board profile, holes, and slots. The B6 directly removes copper from each side after the operator transfers and aligns the board. (The browser entry point mentioned in earlier versions of these notes has since been removed.)

The standalone build is [StudioLaser.exe](dist/StudioLaser.exe), 75 MB, for x64 Windows 11 or Windows 10 version 1809 or later. It bundles Python, Qt, the Gerber/Excellon converter, and libusb. Copy the executable to the destination PC and double-click it. The target PC needs the JCZ/LMC WinUSB driver to communicate with the laser. The executable was built locally and is in the ignored `dist` directory.

## Saving and moving to another PC

- Save named projects as `.studiolaser.json` files in any writable location. A project stores its queue, artwork, PCB sides, placements, job status, and snapshots of operation settings.
- The desktop app keeps per-user settings in `%LOCALAPPDATA%\StudioLaser`. The active machine and lens profile is `active-machine.json`, the named preset library is `recipes.json`, and the recovered workspace is `recovery.json`.
- To migrate the setup, close StudioLaser, copy the settings folder to the matching location on the destination PC, and copy any named projects saved elsewhere. **Machine → Save Lens Profile As** exports an individual profile, including its imported lens correction. Use **Machine → Open Lens Profile** on the destination PC to activate it.
- On reopening a project, a job that was saved as running is treated as interrupted. Recheck the physical board placement before a fresh run.

## Makerspace PCB workflow

1. Export front copper, back copper, the board outline, and Excellon drill and slot files at 1:1 from the same fabrication origin used for the CNC. Use the unmirrored copper exports. StudioLaser applies the bottom reflection using the board center and the selected physical flip axis.
2. Use **Import PCB Board** to map the files and choose **Isolation channels** or **Full Clear**. The preview shows retained copper, copper removal, and drill references. Outline cutouts, holes, and slots are excluded from removal geometry.
3. Put the CNC-cut board on the B6. Use **Continuous Frame** to align the artwork with the stock. Drag, enter exact X/Y/rotation, or use the arrow keys. Ctrl+arrow rotates, and Shift selects a finer step.
4. Use **Register Board** to fit two ordered CNC hole centers or two points on a straight board edge. Registration fits translation and rotation at fixed scale. The spacing mismatch and optional third-point check help detect a wrong reference or flip.
5. Select a material recipe, preview the toolpaths, stop framing, and verify the current side and placement. **Arm** authorizes the validated job for 30 seconds. **Start** consumes that authorization. Job edits, Stop, disconnect, and selection changes clear it.
6. After the front is complete, physically flip the board, register the back, and arm and start it independently. After engraving, check trace continuity and shorts. Any required plated or otherwise conductive through-hole connections are a separate fabrication step.

## Implementation notes

- SVG artwork imports as one queue item on one operation by default. The native importer respects physical units, the view box, transforms, curves, and fill rules. It asks for a finished width when the physical size is ambiguous. Object assignment is under **Advanced**.
- The native planner checks the full job before starting and breaks large jobs into bounded batches at path boundaries. A long continuous contour stays intact and uses the JCZ driver’s packetization. Crosshatch runs two perpendicular directions within each pass. Pass count is independently configurable.
- The hardware worker runs in a separate Windows process and receives an explicit Stop message. USB ownership is shared with the legacy entry point through a lock. The native path stops on the first USB transfer error during a mark and reports interrupted or uncertain results for operator review.
- Machine profiles are stored by source and lens. The EZCAD2 importer reads `markcfg7` and a matching JCZ `.cor` file, reports applied, missing, and unsupported settings, and validates the correction table before connecting. Review the actual machine’s settings in the editor. StudioLaser has tools for an outline test square, filled test square, and copper coupon matrix. Recipes can record the lens, stock, and copper thickness.
- Gerber preparation uses Gerbonara and Shapely. File roles use Gerber X2 metadata where present, with filename hints and manual mapping for older or ambiguous files. The code uses routed outline centerlines, handles clear primitives and negative copper file polarity, and supports Excellon routed slots and KiCad’s alternate G85 slots.
- The original `elegant-galileo` checkout was preserved. Its source received fixes for lost Gerber arc regions, shared board-origin mirroring, and preview/export consistency. The existing Gerber2Laser executable was separate from this build. The new standalone SVG command uses the same PCB preparation code as the native StudioLaser app.

## Rebuild and verify

Run the build and test commands from the repository root in an x64 Windows Python environment.

```powershell
python -m pip install -e ".[desktop,freeze,dev]"
python -m PyInstaller --noconfirm StudioLaser.spec
python -m pytest tests -q
```

The PyInstaller specification bundles the app icon, Qt runtime, and libusb. It leaves the Windows Universal CRT and API-set DLLs to the OS and prefers Qt’s matching Visual C++ runtime. This corrected the first build’s Qt startup failure caused by incompatible DLLs picked up from the build machine.

Use the PCB SVG command for offline export.

```powershell
studiolaser pcb-svg --outline board-Edge_Cuts.gbr --top board-F_Cu.gbr --bottom board-B_Cu.gbr --drill board.drl --output svg
```

The CLI uses isolation by default. Use `--mode full-clear` for full removal outside the retained copper, `--flip-axis horizontal` for a top-to-bottom flip, and repeated `--drill` arguments for multiple drill files.

### Verification results

- 128 StudioLaser tests and 13 Gerber2Laser tests passed. The Gerber2Laser suite emitted a parser warning about the bundled sample Gerber’s implicit circular-arc interpolation mode.
- The dense-job regression checked 30,001 ordered paths against mock JCZ packets, including a continuous contour longer than one batch. Other regressions covered SVG sizing and fill rules, PCB polarity and registration, slot exclusion, stop and disconnect handling, stale arming, and queue recovery.
- The rebuilt executable opened and closed normally from an isolated folder without a Python executable on its search path. The frozen Windows hardware worker also completed a mock engraving job.

## B6 commissioning still pending

Bring the exact makerspace unit’s `markcfg7`, matching lens `.cor`, source model and wattage, installed lens details, a representative two-sided board, and scrap of the same laminate and copper thickness. Verify USB detection and Stop behavior on the B6. Compare red-light framing against actual marks at the center and edges of the lens, then measure a coupon for copper removal, trace width, electrical isolation, and visible laminate damage. Save a recipe after those checks. Validate registration on both sides against the board’s pad-to-hole clearance budget before adopting the setup for production.

## References

- Shared fabrication coordinates and metadata. [Ucamco Gerber format](https://www.ucamco.com/en/gerber)
- Gerber and Excellon export origin and slot options. [KiCad PCB Editor manual](https://docs.kicad.org/9.0/en/pcbnew/pcbnew.html)
- B6 setup and lens correction. [ComMarker B6/B6 MOPA manual](https://commarker.com/dist/js/uploads/Machines/BSeries/B6/Manual/B6UserManual.pdf)
- Galvo and red-light calibration settings. [LightBurn Galvo and Basic Settings](https://docs.lightburnsoftware.com/latest/Reference/DeviceSettings/GalvoBasicSettings/)
- Direct copper structuring and through-hole interconnects. [LPKF PCB structuring](https://www.lpkfusa.com/products-technologies/research-in-house-pcb-prototyping/process-steps-for-pcb-prototyping/pcb-structuring), [LPKF PCB prototyping guide](https://app.lpkfusa.com/datasheets/prototyping/techguide-in-house-pcb-prototyping.pdf)
