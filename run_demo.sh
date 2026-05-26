#!/usr/bin/env bash
# One-command launcher for macOS / Linux — creates .venv if missing, installs deps, starts Flask.
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ and retry."
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "Creating virtual environment .venv ..."
  python3 -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate

echo "Installing dependencies..."
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

echo "Starting jobdoc-demo on http://0.0.0.0:5000 ..."
exec python app.py
