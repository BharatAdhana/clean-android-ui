"""Phase 9 Post-Cleanup Health & Functional Verification Script."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from adb_helper import find_adb, resolve_device_serial, run_adb
from safety import resolve_backup_dir

CORE_CHECKS = [
    ("System UI", "com.android.systemui"),
    ("Phone / Telephony", "com.android.phone"),
    ("Settings", "com.android.settings"),
    ("Google Play Services", "com.google.android.gms"),
    ("Google Play Store", "com.android.vending"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify device stability & recovery readiness after cleanup")
    parser.add_argument("--backup-dir", help="Explicit backup dir")
    parser.add_argument("--serial", help="Explicit ADB serial")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent.parent
    backup_dir = resolve_backup_dir(project_dir, args.backup_dir)

    adb_path = find_adb()
    serial = resolve_device_serial(preferred_serial=args.serial)

    installed_raw = run_adb(["shell", "pm", "list", "packages", "--user", "0"], serial=serial, adb_path=adb_path).stdout
    installed = {
        l[len("package:") :].strip()
        for l in installed_raw.splitlines()
        if l.strip().startswith("package:")
    }

    print("=== CRITICAL SERVICES CHECK ===")
    failures = 0
    for label, pkg in CORE_CHECKS:
        ok = pkg in installed
        print(f"  - {label} (`{pkg}`): {'OK' if ok else 'MISSING!'}")
        if not ok:
            failures += 1

    home_out = run_adb(
        ["shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"],
        serial=serial,
        adb_path=adb_path,
    ).stdout.strip()
    print(f"  - Active Home Launcher: {home_out}")

    ime_out = run_adb(["shell", "settings", "get", "secure", "default_input_method"], serial=serial, adb_path=adb_path).stdout.strip()
    print(f"  - Active Keyboard (IME): {ime_out}")

    crash_tail = run_adb(["logcat", "-b", "crash", "-d", "-t", "20"], serial=serial, adb_path=adb_path).stdout.strip()
    print(f"  - Crash buffer lines (tail 20): {len(crash_tail.splitlines()) if crash_tail else 0}")

    print("\n=== RESTORE DRY-RUN CHECK ===")
    restore_res = subprocess.run(
        [sys.executable, str(project_dir / "scripts" / "restore.py"), "--dry-run", "--backup-dir", str(backup_dir), "--serial", serial],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(restore_res.stdout.strip())
    if restore_res.returncode != 0:
        print(restore_res.stderr.strip(), file=sys.stderr)
        failures += 1

    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
