#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if command -v python3 >/dev/null 2>&1; then
  PY_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PY_CMD="python"
else
  echo "[ERROR] Python 3.9+ was not found on PATH." >&2
  exit 1
fi

echo "========================================================================"
echo "  Clean MIUI AI - One-Click Restore"
echo "  Restores packages, settings, appops, and display density from backup."
echo "========================================================================"

"$PY_CMD" scripts/restore.py "$@"
