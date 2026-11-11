# Security and Safety

StudioLaser controls hardware that can emit laser radiation. Treat this project as experimental software and use it only with appropriate laser safety controls, enclosure, interlocks, eyewear, ventilation, and trained operators.

## Sensitive files not meant for git

Do not commit local machine or laser-profile files, including:

- `markcfg*`
- correction/calibration files such as `*.cor`
- job/design files such as `*.ezd`, `*.dxf`, `*.svg`, `*.stl`
- machine profiles exported from Machine Setup (they embed the lens correction)
- local virtual environments and logs

The repository `.gitignore` excludes these by default.

The exception is `tests/fixtures/b6_60w/`, which holds the maintainers' own ComMarker B6 factory files
(`markcfg7`, `jcz15.cor`, `MarkParam.lib`), committed deliberately as regression fixtures. They describe
one machine's lens and timing; do not load them into a different machine.

## Reporting issues

Please open a GitHub issue for safety bugs, crashes, or unexpected emission behavior. Do not include private calibration files, serial numbers, or proprietary job files in public reports.
