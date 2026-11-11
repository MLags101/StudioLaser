# Commissioning StudioLaser on a ComMarker B6 MOPA

StudioLaser has been exercised against a simulated controller and automated
tests only. This checklist covers what has to be confirmed on the real machine,
in order. Stop at the first step that fails and report it.

Wear eyewear rated for 1064 nm, keep the enclosure closed when emitting, run
fume extraction, and keep the machine's own emergency stop within reach.

## 1. Connection

- [ ] Install the WinUSB driver with Zadig (README → USB driver).
- [ ] Start StudioLaser. The chip at the top right reads **Laser connected**.
- [ ] Unplug USB: the chip changes to **Laser offline** within a few seconds.
- [ ] With EZCAD's driver installed instead, the chip reads **Wrong USB driver**.
      If it reads *Laser connected* and framing then fails, note the error text:
      on some systems libusb lists a device it cannot open.

## 2. Machine Setup

- [ ] Load `markcfg7` and the lens `.cor` file. The report shows the field size
      (167.5 mm for the 150 mm lens) and which correction EZCAD was using.
- [ ] Load `MarkParam.lib` too (it is picked up from beside `markcfg7`). Tab 4 should show EZCAD's timing:
      laser-on −300 µs, laser-off 100, corner 100, jump settle 500 / 500, jump speed 4000 mm/s.
- [ ] Confirm the source is the 60 W JPT M7 and the lens is correct, enable, save. Laser Presets now lists
      your EZCAD sets (PCBCut, SMR, PCBClean, PCBEn) marked “from EZCAD”.

## 3. Red light and Stop, no emission

- [ ] Tools → Outline Test Square, 40 mm. **Frame**: the red square appears,
      centred under the lens, and is not mirrored or rotated a quarter turn.
      If it is, change the axis options in Machine Setup.
- [ ] Drag the square while framing: the red outline follows within about a second. Try the jog pad and
      the arrow keys too, and Ctrl+arrows: the square turns about its own centre.
- [ ] Frame button drop-down: switch between outline, outline and holes, box and whole design while framing.
- [ ] Open the F1 guide while framing and press **Esc**: the red light stops.
- [ ] Press **Stop** (and separately **Esc**): the red light goes **off** and
      stays off. *This is the check for the pointer being left on after an abort.*
- [ ] Close the app while framing: the red light goes off and the app exits.

## 4. First marks, on card or painted scrap

Use the test square with power around 10–20 %.

- [ ] Arm, Start. The square is marked once, the right size to within a few
      percent, and the laser stops at the end.
- [ ] Start a filled test square and press **Stop** part-way. Emission stops
      at once. The job shows *Interrupted*. No further marking occurs.
      *This is the check that Stop aborts the controller's buffered list.*
- [ ] Unplug USB during a (low-power) mark. Note what the machine does and what
      the app reports. Expect the red banner asking you to confirm the machine
      is idle.
- [ ] Line starts: look at the first millimetre of lines after a long jump. A
      hook or a faint start means the jump-settle delays need raising
      (Machine Setup → Motion).

## 5. Lens calibration

- [ ] Calibrate Lens → mark a 3 × 3 grid at 100 mm with the imported correction.
      Measure the eight spans.
- [ ] If the spans are more than about 1 % off, switch the correction base
      (factory `.cor` ↔ EZCAD parameters), mark again, and keep the closer one.
      *Also check here that the `.cor` base does not make things worse in an
      odd way, such as a mirrored bow; that would mean the table's orientation
      is not what the uploader assumes.*
- [ ] Calculate and apply. Mark the grid again: spans within about 0.1 mm.
- [ ] For tighter accuracy use a 5 × 5 grid with measured positions.
- [ ] Red-light tab: align on the centre and corner crosses, save, and confirm
      the red crosshair now lands on the burnt crosses.

## 6. Material

On scrap of the real laminate, focused on the copper surface:

- [ ] Focus Test at several lens heights; note the best height.
- [ ] Material Test Matrix for copper fill (for example power 40–100 %, speed
      500–2000 mm/s). Look for copper fully removed with the least charring of
      the FR4. Save the best patch as a preset and make it the default.
- [ ] Repeat for copper contour, and for solder mask and silkscreen if used.
- [ ] Kerf and Isolation Ladder. Probe each island to the surrounding copper
      with a multimeter; note the narrowest channel that isolates. Measure a
      single line's width and store it as the preset's kerf.
- [ ] Watch for copper lifting or delaminating on thin traces. If it happens,
      use more, lighter passes.

## 7. Two-sided registration

- [ ] Import a board with at least three drilled holes. Register the top side on
      three holes; the reported errors should be below about 0.1 mm. Larger
      errors point at lens calibration or red-light alignment.
- [ ] Run the top side. Flip, register the bottom on the same holes, run it.
- [ ] Check front-to-back alignment at pads around drilled holes, and compare
      the offset with the board's pad-to-hole clearance. Check continuity and
      shorts on both sides.

## Report back

For anything that fails, note the step, what happened, and the text in the
Activity panel.
