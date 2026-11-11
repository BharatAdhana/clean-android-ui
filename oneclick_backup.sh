#!/usr/bin/env bash
set -euo pipefail

CALLER_PWD="$PWD"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

resolve_caller_path() {
  local raw="$1"
  if [[ -z "${raw//[[:space:]]/}" ]]; then
    printf '%s' ""
    return
  fi
  if [[ "$raw" == /* || "$raw" =~ ^[A-Za-z]:[/\\] ]]; then
    printf '%s' "$raw"
  else
    printf '%s' "$CALLER_PWD/$raw"
  fi
}

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
PROJECT_DIR_ARGS=()
BACKUP_ARGS=()
RESOLVED_PROJ_DIR=""
HELP_MODE=0
ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  arg="${ARGS[$i]}"
  if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
    HELP_MODE=1
    BACKUP_ARGS+=("$arg")
  elif [[ "$arg" == "--serial" ]]; then
    if [[ $((i + 1)) -lt ${#ARGS[@]} ]]; then
      val="${ARGS[$((i + 1))]}"
      SERIAL_ARGS=(--serial "$val")
      BACKUP_ARGS+=(--serial "$val")
      i=$((i + 1))
    else
      BACKUP_ARGS+=("$arg")
    fi
  elif [[ "$arg" == --serial=* ]]; then
    val="${arg#--serial=}"
    SERIAL_ARGS=(--serial "$val")
    BACKUP_ARGS+=("$arg")
  elif [[ "$arg" == "--project-dir" ]]; then
    if [[ $((i + 1)) -lt ${#ARGS[@]} ]]; then
      val="$(resolve_caller_path "${ARGS[$((i + 1))]}")"
      RESOLVED_PROJ_DIR="$val"
      PROJECT_DIR_ARGS=(--project-dir "$val")
      BACKUP_ARGS+=(--project-dir "$val")
      i=$((i + 1))
    else
      BACKUP_ARGS+=("$arg")
    fi
  elif [[ "$arg" == --project-dir=* ]]; then
    val="$(resolve_caller_path "${arg#--project-dir=}")"
    RESOLVED_PROJ_DIR="$val"
    PROJECT_DIR_ARGS=(--project-dir "$val")
    BACKUP_ARGS+=(--project-dir "$val")
  else
    BACKUP_ARGS+=("$arg")
  fi
done

if [[ "$HELP_MODE" -eq 1 ]]; then
  "$PY_CMD" scripts/inspect_and_backup.py ${BACKUP_ARGS[@]+"${BACKUP_ARGS[@]}"}
  exit $?
fi

echo "========================================================================"
echo "  Clean MIUI AI - One-Click Backup & Restore Verification"
echo "  Existing OS only (No root / No ROM flash)."
echo "========================================================================"

"$PY_CMD" scripts/inspect_and_backup.py ${BACKUP_ARGS[@]+"${BACKUP_ARGS[@]}"}

echo ""
echo "========================================================================"
echo "  Verifying backup integrity & dry-run restore plan..."
echo "========================================================================"

"$PY_CMD" scripts/restore.py ${SERIAL_ARGS[@]+"${SERIAL_ARGS[@]}"} ${PROJECT_DIR_ARGS[@]+"${PROJECT_DIR_ARGS[@]}"} --dry-run

echo ""
echo "[OK] Backup created and dry-run restore verified!"
if [[ -n "$RESOLVED_PROJ_DIR" ]]; then
  echo "Latest snapshot recorded in $RESOLVED_PROJ_DIR/backups/LATEST_BACKUP.txt."
else
  echo "Latest snapshot recorded in backups/LATEST_BACKUP.txt."
fi
