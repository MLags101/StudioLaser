#!/bin/bash
# OpenLaser one-step installer for macOS.
#
# Double-click this file in Finder (or run it from Terminal). It:
#   1. finds a suitable Python 3,
#   2. sets up the private Python environment (.venv),
#   3. installs OpenLaser and its dependencies into it,
#   4. builds the double-clickable OpenLaser.app launcher.
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

printf '\n\033[1mOpenLaser installer\033[0m\n'
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
  3. Double-click 'Install OpenLaser.command' again."
fi
echo "Using $("$PYTHON" --version) at $PYTHON"

step "Step 2 of 3: Installing OpenLaser (this can take a minute)"
if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON" -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -e ".[desktop]" \
  || fail "The installation step failed." \
"Check your internet connection and run this installer again.
If it keeps failing, please open an issue at
https://github.com/luizbueno3d/OpenLaser/issues with a screenshot."

step "Step 3 of 3: Building the OpenLaser app"
rm -rf OpenLaser.app
osacompile -o OpenLaser.app scripts/launcher.applescript
if [ -f assets/icons/openmopa.icns ]; then
  cp assets/icons/openmopa.icns OpenLaser.app/Contents/Resources/applet.icns
  # Recent macOS osacompile also writes an asset-catalog icon (Assets.car +
  # CFBundleIconName) that takes precedence over applet.icns.
  rm -f OpenLaser.app/Contents/Resources/Assets.car
  /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" OpenLaser.app/Contents/Info.plist >/dev/null 2>&1 || true
  codesign --force --sign - OpenLaser.app >/dev/null 2>&1 || true
  touch OpenLaser.app
fi

printf '\n\033[1;32mDone! OpenLaser is installed.\033[0m\n'
cat <<'EOF'

  * Double-click OpenLaser.app (in this folder) to start OpenLaser.
  * Finder will select OpenLaser.app for you next.
  * Drag that app into your Dock for a permanent one-click launcher.
  * Keep OpenLaser.app inside this folder - it belongs here.
  * If you move this folder somewhere else later, just run this
    installer again.

EOF
open -R OpenLaser.app
finish 0
