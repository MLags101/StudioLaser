# StudioLaser

<p align="center">
  <img src="assets/icons/openmopa.png" alt="StudioLaser icon" width="160">
</p>

StudioLaser is an open-source desktop application for JPT MOPA fiber galvo
lasers on JCZ/BJJCZ LMC controllers, built around one job: making
double-sided PCBs on a **ComMarker B6 MOPA**. It replaces EZCAD2 or LightBurn
for that workflow.

- Import Gerber and Excellon files for a board that is already cut and drilled.
- Remove copper to isolate traces on both sides, registered on the drilled holes.
- Remove solder mask from pads and mark silkscreen.
- Calibrate the lens from the factory files plus a measured test grid.
- Build and keep laser presets from test matrices.

SVG, DXF and STL artwork can be engraved too.

> [!IMPORTANT]
> StudioLaser is early-stage software that controls equipment capable of causing
> fires, eye injuries, burns, fumes, and machine damage. Never leave a running
> machine unattended. Wear eyewear rated for 1064 nm (OD 6+ with an EN 207 LB
> rating), enclose the beam, extract fumes (FR4 and solder mask give off
> hazardous smoke), and test at low power first.

> [!NOTE]
> **Tested in simulation, not yet on a laser.** Everything here runs against a
> simulated controller and automated tests. Direct control of a real B6 on
> Windows has not been verified by the maintainers. Follow
> [docs/commissioning.md](docs/commissioning.md) before trusting it, and treat
> the built-in laser presets as untested starting points.

## Install

### Standalone Windows executable

Open `StudioLaser.exe` on Windows 11 or Windows 10 version 1809 or later (x64).
It bundles Python, Qt, the PCB converter and libusb. To build it from source:

```powershell
python -m pip install -e ".[desktop,freeze]"
python -m PyInstaller --noconfirm StudioLaser.spec
```

The build is written to `dist\StudioLaser.exe`.

### Source installers

| System | Run | Start StudioLaser with |
| --- | --- | --- |
| Windows 10 / 11 | `Install StudioLaser.bat` | the **StudioLaser** shortcut on the Desktop and in the Start menu |
| macOS | `Install StudioLaser.command` | `StudioLaser.app` |
| Linux | `bash install-studiolaser.sh` | **StudioLaser** in your applications menu |

Each needs Python 3.10 or newer ([python.org/downloads](https://www.python.org/downloads/);
on Windows tick **Add python.exe to PATH**). Download the repository as a ZIP,
unzip it somewhere permanent, and run the installer for your system. The macOS
and Linux installers have not been run since the desktop app replaced the old
browser interface.

### Developers

```bash
git clone https://github.com/MLags101/StudioLaser.git
cd StudioLaser
python -m venv .venv
.venv\Scripts\activate            # macOS / Linux: source .venv/bin/activate
python -m pip install -e ".[desktop,dev]"
studiolaser desktop
python -m pytest tests -q
```

### USB driver

- **Windows:** the laser normally uses the EZCAD (BJJCZ) driver, which
  StudioLaser cannot use. Switch it to WinUSB once:
  1. Connect the laser by USB and power it on.
  2. Download **Zadig** from [zadig.akeo.ie](https://zadig.akeo.ie/) and open it.
  3. In **Options**, enable **List All Devices**.
  4. Choose **USBLMCV4** (USB ID `9588 9899`).
  5. Choose **WinUSB** and click **Replace Driver**.

  After this, EZCAD and LightBurn cannot use the laser on this PC until you
  put their driver back (Device Manager → USBLMCV4 → Update driver).
- **Linux:** install the udev rule: `sudo cp scripts/99-studiolaser.rules /etc/udev/rules.d/`,
  then `sudo udevadm control --reload-rules && sudo udevadm trigger`, and replug the laser.
- **macOS:** nothing to do.

The status chip at the top right shows **Laser detected** when the USB device is
visible. It shows **Controller responding** during an operation after the board
answers the handshake. Hover the chip for details or click it to choose a laser
when more than one is plugged in.
Each laser is listed by its USB bus and port, and the choice is remembered as
long as that laser stays in the same USB socket. If the chosen laser is
unplugged, StudioLaser refuses to run rather than switch to another one.
**Automatic** uses whichever single laser is connected. `studiolaser detect` prints the same diagnosis without sending any
laser command.

## The window

- **Top right:** connection status and **STOP**. Stop (or **Esc**) is always
  available, from any window.
- **Toolbar:** Undo, Redo, Fit · Preview, Frame · Arm, Start. A button that is
  unavailable says why in its tooltip.
- **Jobs (left):** the job list. A board side lists its operations with a tick
  box each: Copper contour, Copper fill, Cleanup pass, Solder mask removal,
  Silkscreen.
- **Canvas:** the galvo field with grid and rulers. Drag a job to move it;
  arrow keys nudge 0.1 mm (Shift 0.01, Alt 1); Ctrl+arrows rotate. Wheel zooms,
  right- or middle-drag pans, **F** fits. Anything outside the field is shaded.
- **Settings (right):** placement and the selected operation's laser settings,
  always visible, with warnings as you type.
- **Activity (bottom):** progress with time remaining, and a log of every message.

## One-time setup

### 1. Machine Setup

**Machine → Machine Setup.** On the first tab, choose the files ComMarker
ships on the machine's USB stick, in the EZCAD folder:

- `markcfg7` — EZCAD's machine settings.
- the lens correction file, for example `jcz15.cor`.
- `MarkParam.lib` (optional, found automatically beside `markcfg7`) — EZCAD's
  pen library. It supplies the controller timing EZCAD used (laser on/off,
  corner and jump delays, jump speed), and each parameter set you saved in
  EZCAD becomes a preset named “… (from EZCAD)”.

**Read factory files** fills in the field size, axis directions, red-light
settings, frequency range and timing, and reports what it found. Check the source and
lens on the Machine tab, then tick **This profile describes my machine** and
save. Nothing is sent to the laser.

The pulse widths and their frequency limits come from the JPT M7 60 W manual
(tab 3). If your source is a different wattage, enter its table from its manual.

### 2. Calibrate Lens

**Machine → Calibrate Lens.**

1. **Choose the lens correction to start from.** EZCAD can correct the lens
   either with the `.cor` file or with its own bulge/skew/trapezoid numbers.
   StudioLaser imports whichever EZCAD had switched on. If you are unsure which is
   right for your machine, mark the same grid with each and keep the one whose
   spans measure closest to the grid size. StudioLaser's version of EZCAD's
   parametric correction is an approximation, because EZCAD's formula is not
   published.
2. **Mark a grid** on card, painted metal or anodised aluminium: add the grid to
   the job list and run it like any job at a power that just marks.
3. **Measure it.** For a 3 × 3 grid, enter eight caliper distances. For 5 × 5 or
   7 × 7, enter or paste each cross's measured position.
4. **Calculate, then Apply.** Mark the grid again to confirm; repeating refines it.
5. **Red light.** Steer the red dot onto the burnt centre cross and one corner
   cross, then save. Framing and hole registration then match the beam.

The measured calibration is stored in the machine profile, which can be saved
to a file and opened on another PC from Machine Setup.

### 3. Laser presets

Each PCB operation starts from a built-in preset for a 60 W source, 150 mm lens
and 35 µm copper on FR4. They come from vendor and community figures (see
[docs/reference/SOURCES.md](docs/reference/SOURCES.md)), not from measurements
on your machine. Tune them on scrap of your laminate:

- **Tools → Tune a PCB Preset** makes one coupon at a time for trace isolation,
  silkscreen or solder mask removal. Start from a saved or imported preset,
  frame and run the coupon on matching scrap, then review its result. The tuner
  holds frequency at 37 kHz and suggests one change at a time. Verify trace
  isolation with a meter. Use the Settings panel to choose parallel lines,
  crosshatch or contour offsets for production fills. Radial spokes are available
  on round test coupons.
- **Tools → Material Test Matrix** engraves a labelled grid that varies two
  settings. Pick the best patch, select it in the job list, and use **Save as
  preset** in the Settings panel.
- **Tools → Kerf and Isolation Ladder** engraves islands ringed by channels from
  0.1 to 0.5 mm, plus single lines. Probe each island for isolation, and measure
  a line's width: that is the kerf. Store it on your copper-fill preset.
- **Tools → Focus Test** marks one numbered group of fine lines. Run it at
  several lens heights, a new slot each time, and keep the sharpest.
- **Tools → Laser Presets** edits, duplicates, imports and exports presets.
  Mark one preset per operation as the default; new boards start from it, and
  its kerf becomes the default kerf compensation.

The frequency note estimates the decrease in average power below the source's
cut-off for the selected pulse width. The note does not block engraving.

## Making a two-sided board

1. **Export** copper, mask, silkscreen, outline and drill files from your CAD
   tool at 1:1 from the same origin the CNC used, **unmirrored**.
2. **Import Gerber / PCB.** Confirm the detected file roles, then choose
   **Isolate traces**, **Clear all non-trace copper**, or **Engrave drawn shapes**.
   Set the isolation width, kerf, flip axis, and which side runs first. Review
   the preview before adding the job. For a single positive copper layer,
   the outline is optional. Check that the stock covers the inferred work area.
3. **Align the first side by framing.** Put the board under the lens and press
   **Frame**. The red light traces the board outline and a small ring at every
   drilled hole, and the canvas shows the same trace as a dashed red line. Move
   the design while it runs and the red light follows:
   - drag it on the canvas for the rough position;
   - arrow keys move 0.1 mm (Shift 0.01, Alt 1), Ctrl+arrows rotate;
   - or use the jog pad under Placement, with its step size.

   Rotation turns about the design's centre. It is aligned when every red ring
   sits centred in its hole. The drop-down on the Frame button chooses what is
   traced: outline and holes, outline only, bounding box, or the whole design.
4. **Check it precisely (optional): Register.** Choose what to register on:
   - **Drilled holes.** Click a hole on the canvas, steer the red dot into that
     hole on the board, and repeat for a second hole far from the first. A third
     or fourth hole checks the fit, and each hole's error is shown.
   - **Board edge (edge cuts).** Frame first so the outline is roughly on the
     board. Then click any point on the outline on the canvas (the pick snaps to
     the edge) and steer the red dot onto that edge of the real board. Any spot
     along the edge will do. Use three or more points on at least two edges;
     corners and opposite edges fit best. Each point is fitted only to the edge
     it was picked on, and its distance from that edge is shown. If the points
     cannot fix both position and rotation (all on one straight edge, say), the
     dialog asks for another point instead of guessing.

   **Check with red light** traces the fitted result (hole rings, or the outline)
   before you apply it.
5. Tick **Side and alignment checked**, then **Preview** (toolpaths and time
   estimate), **Arm**, **Start**. Arming lasts 30 seconds and is cancelled by any
   edit; moving the board clears the tick.
6. **Flip the board**, select the second side, align it on the same holes, and
   run it. The second side can be armed after the first is
   complete.

Plated holes and vias are a separate step; StudioLaser does not make them.

## Projects and settings

- **File → Save Project** writes a `.studiolaser.json` file with the job list,
  placements, settings and job status.
- Per-user state is in `%LOCALAPPDATA%\StudioLaser` (`~/.config/StudioLaser` on
  macOS and Linux): `active-machine.json`, `recipes.json` (presets) and
  `recovery.json` (the job list, restored on the next start).
- A job that was running when the app closed reopens as *interrupted*. Check
  the board, then **Review and allow re-run**.

## Safety rules the software enforces

- Nothing marks without **Arm** then **Start**; arming expires after 30 seconds
  and is cancelled by any change to the job, a disconnect, or Stop.
- A board side must be ticked as checked before arming, and the tick is cleared
  whenever that side is moved.
- The whole job is planned and checked against the field and the source's
  limits before the first pulse.
- Each hardware operation runs in its own process; Stop sends an abort and
  turns off the laser gate and the red pointer. If a stop cannot be confirmed,
  the app locks hardware actions until you confirm the machine is idle.
- There is no door interlock or foot-pedal input. Use the machine's own
  enclosure and emergency stop.

## Limitations

- Direct control of a real laser is untested (see the note at the top).
- One controller family: JCZ/BJJCZ LMC (EZCAD2) boards over USB. No EZCAD3, no
  GRBL, no Ruida.
- No Z-axis or rotary control; set focus on the machine.
- Gerber and Excellon input only; no native KiCad or Eagle board files.
- The G-code export, GRBL machine profiles and the browser interface from
  earlier versions have been removed.

## Command line

```bash
studiolaser                 # start the desktop app
studiolaser detect          # USB diagnosis, no laser commands
studiolaser pulse-widths    # pulse widths of a profile
studiolaser pcb-svg --outline board-Edge_Cuts.gbr --top board-F_Cu.gbr --bottom board-B_Cu.gbr --drill board.drl --output svg
```

Set `OPENMOPA_MOCK=1` to run the whole application against a simulated
controller with no laser attached.

## History

StudioLaser began as a fork of [OpenLaser](https://github.com/luizbueno3d/OpenLaser) by
Luiz Bueno and was renamed when it became a dedicated PCB tool for the ComMarker B6.
Settings, presets and project files from OpenLaser are picked up automatically, and
the old `openlaser` and `openlaser-desktop` commands still work.

## License and security

MIT. See [SECURITY.md](SECURITY.md) for reporting safety issues. The factory files
in `tests/fixtures/b6_60w/` belong to one specific machine; load your own machine's files instead.
