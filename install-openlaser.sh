#!/usr/bin/env bash
# OpenLaser installer for Linux.
#
# Run it from a terminal inside the OpenLaser folder:
#     bash install-openlaser.sh
# It:
#   1. finds Python 3.10 or newer,
#   2. sets up OpenLaser's private Python environment (.venv) and its dependencies,
#   3. adds OpenLaser to your applications menu,
#   4. offers to install the USB permission rule for the JCZ/LMC MOPA laser.
#
# Safe to run again at any time (after moving this folder, or to update).

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PROJECT_DIR="$(pwd)"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m%s\033[0m\n%s\n' "$1" "${2:-}"; exit 1; }

printf '\n\033[1mOpenLaser installer\033[0m\n'
printf 'Installing into: %s\n' "$PROJECT_DIR"

step "Step 1 of 4: Looking for Python 3.10 or newer"
PYTHON=""
for candidate in python3 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  fail "Python 3.10 or newer was not found." \
"Install it with your package manager, then run this installer again:
  Ubuntu / Debian / Mint:  sudo apt install python3 python3-venv
  Fedora:                  sudo dnf install python3
  Arch / Manjaro:          sudo pacman -S python"
fi
echo "Using $("$PYTHON" --version) at $PYTHON"
if ! "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  PY_MINOR="$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  fail "Python is installed, but its 'venv' module is missing." \
"On Ubuntu / Debian / Mint install it with:
  sudo apt install python${PY_MINOR}-venv
then run this installer again."
fi

step "Step 2 of 4: Installing OpenLaser (this can take a minute)"
if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON" -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -e ".[desktop]" \
  || fail "The installation step failed." \
"Check your internet connection and run this installer again.
If it keeps failing, please open an issue at
https://github.com/luizbueno3d/OpenLaser/issues with the text above."

step "Step 3 of 4: Adding OpenLaser to your applications menu"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS_DIR"
cat > "$APPS_DIR/openlaser.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=OpenLaser
Comment=Prepare and run laser and plotter jobs
Exec="$PROJECT_DIR/.venv/bin/openlaser-desktop"
Path=$PROJECT_DIR
Icon=$PROJECT_DIR/assets/icons/openmopa-256.png
Terminal=false
Categories=Graphics;Engineering;
EOF
chmod +x "$APPS_DIR/openlaser.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
echo "Added: $APPS_DIR/openlaser.desktop"

step "Step 4 of 4: USB permission for the JCZ/LMC MOPA laser"
RULE_TARGET=/etc/udev/rules.d/99-openlaser.rules
if [ -f "$RULE_TARGET" ] && cmp -s scripts/99-openlaser.rules "$RULE_TARGET"; then
  echo "The USB permission rule is already installed."
elif [ "${OPENLASER_SKIP_UDEV:-0}" = "1" ] || [ ! -t 0 ]; then
  echo "Skipped. If you use a JCZ/LMC MOPA laser, install the rule later with:"
  echo "  sudo cp scripts/99-openlaser.rules $RULE_TARGET && sudo udevadm control --reload-rules && sudo udevadm trigger"
else
  echo "Only needed for the JCZ/LMC MOPA laser (GRBL machines do not need it)."
  read -r -p "Install the USB permission rule now? It asks for your password. [Y/n] " answer
  if [[ ! "$answer" =~ ^[Nn] ]]; then
    sudo cp scripts/99-openlaser.rules "$RULE_TARGET"
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "Installed. Unplug and replug the laser's USB cable once."
  else
    echo "Skipped. You can run this installer again later."
  fi
fi

printf '\n\033[1;32mDone! OpenLaser is installed.\033[0m\n'
cat <<'EOF'

  * Open your applications menu and start "OpenLaser".
    The OpenLaser desktop workspace opens.
  * Tip: right-click OpenLaser in the menu or dock and choose
    "Add to Favorites" / "Pin" for one-click launching.
  * To quit OpenLaser, close its window.
  * If you move this folder somewhere else later, run this installer again.

EOF
