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

SERIAL_ARGS=()
ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  if [[ "${ARGS[$i]}" == "--serial" && $((i + 1)) -lt ${#ARGS[@]} ]]; then
    SERIAL_ARGS=(--serial "${ARGS[$((i + 1))]}")
  fi
done

echo "========================================================================"
echo "  Clean MIUI AI - One-Click Backup & Restore Verification"
echo "  Existing OS only (No root / No ROM flash)."
echo "========================================================================"

"$PY_CMD" scripts/inspect_and_backup.py "$@"

echo ""
echo "========================================================================"
echo "  Verifying backup integrity & dry-run restore plan..."
echo "========================================================================"

"$PY_CMD" scripts/restore.py "${SERIAL_ARGS[@]}" --dry-run

echo ""
echo "[OK] Backup created and dry-run restore verified!"
echo "Latest snapshot recorded in backups/LATEST_BACKUP.txt."
