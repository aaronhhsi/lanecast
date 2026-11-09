#!/bin/sh
# lanecast installer for macOS and Linux. Paste this into a terminal:
#
#   curl -fsSL https://raw.githubusercontent.com/aaronhhsi/lanecast/main/install.sh | sh
#
# Everything goes in one folder, ~/lanecast (app/, env/, bin/, data/), and lanecast
# starts at login. On a Mac you may be asked to install Apple's command line tools
# (for git) or to allow an install: both are fine. Running it again updates an install.
#
# For testing: LANECAST_DIR, LANECAST_SOURCE (copy the code from this folder instead
# of cloning), LANECAST_LANEQ_BIN (use this laneq binary).
set -eu
ROOT="${LANECAST_DIR:-$HOME/lanecast}"
REPO="${LANECAST_REPO:-https://github.com/aaronhhsi/lanecast.git}"
step() { printf '\n==> %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "Installing lanecast into $ROOT"

step "Python"
PY=""
for c in python3 python; do
  if have "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  if [ "$(uname)" = Darwin ] && have brew; then brew install python@3.12 && PY=python3.12
  elif have apt-get; then sudo apt-get update && sudo apt-get install -y python3 python3-venv && PY=python3
  elif have dnf; then sudo dnf install -y python3 && PY=python3
  else echo "Python 3.10 or newer is needed: install it from python.org, then run this again."; exit 1; fi
fi
# Debian/Ubuntu ship venv separately
if ! "$PY" -c 'import venv, ensurepip' 2>/dev/null && have apt-get; then sudo apt-get install -y python3-venv; fi
echo "Using $(command -v "$PY")"

step "Git"
if ! have git; then
  if [ "$(uname)" = Darwin ]; then xcode-select --install || true; echo "Finish Apple's install window, then run this again."; exit 1
  elif have apt-get; then sudo apt-get install -y git
  elif have dnf; then sudo dnf install -y git
  else echo "Install git, then run this again."; exit 1; fi
fi

step "The GitHub CLI (to make the outbox repo for you)"
if ! have gh; then
  if have brew; then brew install gh || true
  else echo "Not installed (optional): see https://cli.github.com. The dashboard explains the other way."; fi
fi

step "lanecast's code"
mkdir -p "$ROOT"
if [ -n "${LANECAST_SOURCE:-}" ]; then
  rm -rf "$ROOT/app"; mkdir -p "$ROOT/app"
  (cd "$LANECAST_SOURCE" && tar cf - --exclude=.git --exclude=__pycache__ --exclude='*.egg-info' .) | (cd "$ROOT/app" && tar xf -)
elif [ -d "$ROOT/app/.git" ]; then git -C "$ROOT/app" pull --ff-only
else git clone --depth 1 "$REPO" "$ROOT/app"; fi

step "Its Python packages (in its own environment)"
[ -x "$ROOT/env/bin/python" ] || "$PY" -m venv "$ROOT/env"
"$ROOT/env/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$ROOT/env/bin/python" -m pip install --quiet --disable-pip-version-check -e "$ROOT/app"

step "laneq, start at login, and the first start"
# stdin is the script when piped from curl: give the questions the terminal instead
if [ -t 0 ]; then LANECAST_DIR="$ROOT" "$ROOT/env/bin/python" -m lanecast install
else LANECAST_DIR="$ROOT" "$ROOT/env/bin/python" -m lanecast install < /dev/tty; fi

echo
echo "Done. lanecast is in $ROOT"
echo "Control it with: lanecast start | stop | status | update | uninstall   (or $ROOT/lanecast ...)"
