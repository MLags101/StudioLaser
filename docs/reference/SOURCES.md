# Reference material

Files collected in this folder, and where the numbers in OpenLaser come from.
None of the laser parameters below were measured on the target machine.

## Files here

| File | Source | Used for |
| --- | --- | --- |
| `jpt-ydflp-m7-60-120w-manual.pdf` | JPT YDFLP-60/80/100/120-M7-M-R User Manual V1.0 (2021), via a reseller's CDN | Pulse-width list, cut-off and maximum frequency per pulse width (Table 5, page 5). `mopa_luiz/sources.py` copies the 60-M7-M column exactly. |
| `commarker-b6-user-manual.pdf` | commarker.com | B6 focus, lens and EZCAD setup; names the `jcz15.cor` correction file. |
| `commarker-m7-mopa-ezcad-manual.pdf` | orders.commarker.com | EZCAD2 parameter screens (field, galvo, red light), driver folder layout. |
| `hawes-fiber-laser-pcb-settings.json` | github.com/sphawes/fiber-laser-pcb-fab | 20 W fiber PCB settings: traces and solder mask removal. |
| `sklar-xtool-f1-ultra-pcb-settings.json` | github.com/mikeysklar/cnc-fiber-laser-pcb | 20 W fiber PCB settings: trace engrave, silkscreen, mask. |

## Built-in presets (60 W MOPA, 150 mm lens, 35 µm copper on FR4)

| Preset | Values | Basis |
| --- | --- | --- |
| Copper contour | 100 %, 20 ns, 40 kHz, 800 mm/s, 3 passes | ComMarker blog, 60 W MOPA fine-trace row (80 %, 800 mm/s, 38 kHz, 20 ns). https://blog.commarker.com/archives/59265 |
| Copper fill | 100 %, 200 ns, 45 kHz, 1000 mm/s, 0.025 mm hatch, 45° + 90°/pass, 4 passes | ComMarker B6 60 W MOPA "PCB marking" (100 %, 1000 mm/s, 40 kHz, 200 ns, 0.05 mm, 4 passes), https://blog.commarker.com/archives/49730; hatch spacing and angle rotation from a LightBurn-forum 50 W MOPA write-up, https://forum.lightburnsoftware.com/t/pcb-etching-on-a-mopa-after-drilling-milling-with-millmage/186589. 45 kHz is the 200 ns cut-off. |
| Cleanup pass | 25 %, 200 ns, 100 kHz, 1500 mm/s, 0.02 mm hatch | ComMarker "PCB cleaning" row (20 %, 1500 mm/s, 100 kHz, 200 ns). |
| Solder mask removal | 14 %, 100 ns, 40 kHz, 900 mm/s, 0.03 mm cross-hatch, 5 passes | Scaled by wattage from the Hawes and Sklar 20 W settings (40 %, about 0.033 mm, 6 passes) and ComMarker's 20 W figure (25 %). |
| Silkscreen | 11 %, 45 ns, 60 kHz, 1000 mm/s, 0.03 mm hatch | Scaled from Sklar's 20 W silkscreen (40 %, 2 passes). Lowest confidence. |

All of these are vendor, reseller or forum figures. None is lab-validated, and
several were scaled from 20 W non-MOPA machines. Tune them with a test matrix.

## Calibration and correction

- EZCAD2 `.cor` layout as uploaded by galvoplotter / MeerK40t:
  https://github.com/meerk40t/galvoplotter (`galvo/controller.py`). The
  `JCZ_COR_2_1` variant (36-byte header, 65 × 65 int32 pairs, short trailer) was
  read from the machine's own `jcz15.cor`.
- EZCAD's parametric correction (scale, bulge, skew, trapezoid) has no published
  formula. OpenLaser's version is an approximation.
- Calibration procedure modelled on LightBurn's galvo lens calibration:
  https://docs.lightburnsoftware.com/2.1/Reference/CalibrateGalvoLens/ and
  https://docs.lightburnsoftware.com/2.1/Reference/DeviceSettings/GalvoBasicSettings/
- A forum report of ±30 µm from a fitted multi-point calibration:
  https://forum.lightburnsoftware.com/t/fast-and-accurate-calibration-of-galvo-lens/192204

## PCB process background

- Kurokesu, fine-pitch PCB prototypes with a 20 W fiber laser:
  https://www.kurokesu.com/main/2021/01/07/making-fine-pitch-pcb-prototypes-with-fiber-laser/
- EEVblog thread on DIY PCBs with a 60 W JPT M7:
  https://www.eevblog.com/forum/eda/diy-pcb-using-a-fiber-laser-video
- MOPA settings rule of thumb (line interval about 0.7 × spot, frequency =
  speed ÷ interval): https://forum.lightburnsoftware.com/t/the-golden-rule-of-mopa-settings/171740
- Double-sided alignment holes in FlatCAM: https://flatcam.org/manual/doubleside.html
