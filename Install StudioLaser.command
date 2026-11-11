#!/bin/bash
# StudioLaser one-step installer for macOS.
#
# Double-click this file in Finder (or run it from Terminal). It:
#   1. finds a suitable Python 3,
#   2. sets up the private Python environment (.venv),
#   3. installs StudioLaser and its dependencies into it,
#   4. builds the double-clickable StudioLaser.app launcher.
#
# Safe to run again at any time (after moving this folder, or to update).

set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m%s\033[0m\n' "$*"; printf '%s\n' "${2:-}"; finish 1; }
finish() {
  # When double-clicked, Terminal closes instantly on exit; give the user a
  # chance to read the outcome first.
  printf '\n'
  read -n 1 -s -r -p "Press any key to close this window." || true
  printf '\n'
  exit "${1:-0}"
}

printf '\n\033[1mStudioLaser installer\033[0m\n'
printf 'Installing into: %s\n' "$(pwd)"

step "Step 1 of 3: Looking for Python 3.10 or newer"
PYTHON=""
for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  fail "Python 3.10 or newer was not found on this Mac." \
"Install it first (it is free and takes two minutes):
  1. Open https://www.python.org/downloads/ in your browser.
  2. Click the big download button and run the installer.
  3. Double-click 'Install StudioLaser.command' again."
fi
echo "Using $("$PYTHON" --version) at $PYTHON"

step "Step 2 of 3: Installing StudioLaser (this can take a minute)"
if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON" -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -e ".[desktop]" \
  || fail "The installation step failed." \
"Check your internet connection and run this installer again.
If it keeps failing, please open an issue at
https://github.com/MLags101/StudioLaser/issues with a screenshot."

step "Step 3 of 3: Building the StudioLaser app"
rm -rf StudioLaser.app
osacompile -o StudioLaser.app scripts/launcher.applescript
if [ -f assets/icons/openmopa.icns ]; then
  cp assets/icons/openmopa.icns StudioLaser.app/Contents/Resources/applet.icns
  # Recent macOS osacompile also writes an asset-catalog icon (Assets.car +
  # CFBundleIconName) that takes precedence over applet.icns.
  rm -f StudioLaser.app/Contents/Resources/Assets.car
  /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" StudioLaser.app/Contents/Info.plist >/dev/null 2>&1 || true
  codesign --force --sign - StudioLaser.app >/dev/null 2>&1 || true
  touch StudioLaser.app
fi

printf '\n\033[1;32mDone! StudioLaser is installed.\033[0m\n'
cat <<'EOF'

  * Double-click StudioLaser.app (in this folder) to start StudioLaser.
  * Finder will select StudioLaser.app for you next.
  * Drag that app into your Dock for a permanent one-click launcher.
  * Keep StudioLaser.app inside this folder - it belongs here.
  * If you move this folder somewhere else later, just run this
    installer again.

EOF
open -R StudioLaser.app
finish 0
