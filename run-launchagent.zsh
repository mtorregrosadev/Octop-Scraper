#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$SCRIPT_DIR"

for PYTHON in .venv/bin/python venv/bin/python venv313/bin/python; do
  if [[ -x "$PYTHON" ]]; then
    exec "$PYTHON" -u scraper.py --auto
  fi
done

echo "No Python virtual environment found. Create .venv and install requirements.txt." >&2
exit 1
