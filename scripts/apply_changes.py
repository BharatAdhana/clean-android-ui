"""Phase 7-8 Controlled & Guardrailed Executor for Clean MIUI AI.

Applies ONLY explicitly approved JSON action plans (`approved: true`), enforces
the hard protected-package safety blocklist before running any ADB mutation,
and logs every action + previous state to changelog.json and CHANGELOG.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from adb_helper import find_adb, resolve_device_serial, run_adb
from safety import is_protected_package, resolve_backup_dir


def detect_dynamic_protected_packages(serial: str, adb_path: str) -> set[str]:
    """Detect active default IME keyboard and home launcher so they cannot be removed."""
    protected: set[str] = set()
    ime_res = run_adb(["shell", "settings", "get", "secure", "default_input_method"], serial=serial, adb_path=adb_path)
    ime_val = ime_res.stdout.strip()
    if "/" in ime_val:
        protected.add(ime_val.split("/", 1)[0].strip())

    home_res = run_adb(
        ["shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"],
        serial=serial,
        adb_path=adb_path,
    )
    for line in home_res.stdout.splitlines():
        if "/" in line and not line.startswith("priority="):
            protected.add(line.strip().split("/", 1)[0].strip())
    return protected


def append_changelog_entry(backup_dir: Path, entry: dict[str, Any]) -> None:
    """Append one modification record to changelog.json and CHANGELOG.md."""
    cl_json = backup_dir / "changelog.json"
    data = json.loads(cl_json.read_text(encoding="utf-8"))
    data.setdefault("modifications", []).append(entry)
    cl_json.write_text(json.dumps(data, indent=2), encoding="utf-8")

    cl_md = backup_dir / "CHANGELOG.md"
    line = (
        f"- `{entry['timestamp']}` | **{entry['type']}** | `{entry.get('target')}` | "
        f"`{entry.get('action')}` | success=`{entry.get('success')}` | `{entry.get('detail', '')}`\n"
    )
    existing = cl_md.read_text(encoding="utf-8")
    placeholder = "_No modifications have been applied yet (baseline inspection and backup only)._"
    if placeholder in existing:
        existing = existing.replace(placeholder, line.rstrip("\n"))
    else:
        existing = existing.rstrip("\n") + "\n" + line
    cl_md.write_text(existing, encoding="utf-8")


def apply_package_action(
    pkg: str,
    action: str,
    inv_by_pkg: dict[str, dict[str, Any]],
    dynamic_protected: set[str],
    serial: str,
    adb_path: str,
    backup_dir: Path,
) -> bool:
    """Validate guardrails and execute one package action for user 0."""
    blocked, reason = is_protected_package(pkg, dynamic_protected=dynamic_protected)
    if blocked:
        raise RuntimeError(f"SAFETY BLOCK: Refusing to modify protected package '{pkg}': {reason}")

    if action == "disable_user":
        subcmd = f"pm disable-user --user 0 {pkg}"
    elif action == "uninstall_user0":
        subcmd = f"pm uninstall -k --user 0 {pkg}"
    else:
        raise ValueError(f"Unsupported package action: {action}")

    baseline = inv_by_pkg.get(pkg, {})
    res = run_adb(["shell", subcmd], serial=serial, adb_path=adb_path)
    out = (res.stdout + "\n" + res.stderr).strip()
    ok = res.returncode == 0 and ("Success" in out or "new state: disabled-user" in out)

    append_changelog_entry(
        backup_dir,
        {
            "timestamp": dt.datetime.now().isoformat(),
            "type": "package",
            "target": pkg,
            "package_name": pkg,
            "action": action,
            "old_installed_user0": baseline.get("installed_user0"),
            "old_enabled_code_user0": baseline.get("enabled_code_user0"),
            "success": ok,
            "detail": out,
        },
    )
    return ok


def apply_setting_action(
    ns: str,
    key: str,
    new_value: str | None,
    settings_snapshot: dict[str, dict[str, str]],
    serial: str,
    adb_path: str,
    backup_dir: Path,
) -> bool:
    """Apply an approved Android setting change and record original value."""
    old_value = settings_snapshot.get(ns, {}).get(key)
    if new_value is None:
        subcmd = f"settings delete {ns} {key}"
    else:
        escaped = new_value.replace("'", "'\"'\"'")
        subcmd = f"settings put {ns} {key} '{escaped}'"

    res = run_adb(["shell", subcmd], serial=serial, adb_path=adb_path)
    out = (res.stdout + "\n" + res.stderr).strip()
    ok = res.returncode == 0 and "SecurityException" not in out and "Exception" not in out

    append_changelog_entry(
        backup_dir,
        {
            "timestamp": dt.datetime.now().isoformat(),
            "type": "setting",
            "target": f"{ns}/{key}",
            "namespace": ns,
            "key": key,
            "old_value": old_value,
            "new_value": new_value,
            "action": "delete" if new_value is None else "put",
            "success": ok,
            "detail": out,
        },
    )
    return ok


def apply_appop_action(
    pkg: str,
    op: str,
    new_mode: str,
    old_mode: str,
    serial: str,
    adb_path: str,
    backup_dir: Path,
) -> bool:
    """Apply an approved AppOps rule and record baseline mode."""
    subcmd = f"cmd appops set {pkg} {op} {new_mode}"
    res = run_adb(["shell", subcmd], serial=serial, adb_path=adb_path)
    out = (res.stdout + "\n" + res.stderr).strip()
    ok = res.returncode == 0 and "Exception" not in out

    append_changelog_entry(
        backup_dir,
        {
            "timestamp": dt.datetime.now().isoformat(),
            "type": "appop",
            "target": f"{pkg}:{op}",
            "package_name": pkg,
            "op": op,
            "old_mode": old_mode,
            "new_mode": new_mode,
            "action": f"set:{new_mode}",
            "success": ok,
            "detail": out,
        },
    )
    return ok


def apply_wm_density_action(
    new_density: str | None,
    settings_snapshot: dict[str, dict[str, str]],
    serial: str,
    adb_path: str,
    backup_dir: Path,
) -> bool:
    """Apply an approved display density change ('reset' or numeric DPI) and log old override."""
    old_override = settings_snapshot.get("secure", {}).get("display_density_forced")
    subcmd = f"wm density {new_density}" if new_density and new_density != "reset" else "wm density reset"
    res = run_adb(["shell", subcmd], serial=serial, adb_path=adb_path)
    out = (res.stdout + "\n" + res.stderr).strip()
    ok = res.returncode == 0 and "SecurityException" not in out and "Exception" not in out

    append_changelog_entry(
        backup_dir,
        {
            "timestamp": dt.datetime.now().isoformat(),
            "type": "wm_density",
            "target": "display_density",
            "old_value": old_override,
            "new_value": new_density or "reset",
            "action": "reset" if (not new_density or new_density == "reset") else f"set:{new_density}",
            "success": ok,
            "detail": out,
        },
    )
    return ok


def validate_plan_safety(plan: dict[str, Any], dynamic_protected: set[str] | None = None) -> None:
    """Validate explicit approval and ensure zero protected packages appear before executing anything."""
    if not plan.get("approved"):
        raise RuntimeError("Refusing to execute plan without 'approved': true.")
    for item in plan.get("packages", []):
        pkg = item["package_name"]
        blocked, reason = is_protected_package(pkg, dynamic_protected=dynamic_protected)
        if blocked:
            raise RuntimeError(f"SAFETY BLOCK in plan: '{pkg}' cannot be modified ({reason}).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply an approved Clean MIUI AI plan")
    parser.add_argument("--plan", required=True, help="Path to approved JSON action plan")
    parser.add_argument("--backup-dir", help="Backup folder (defaults to LATEST_BACKUP.txt)")
    parser.add_argument("--serial", help="Explicit ADB serial")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent.parent
    backup_dir = resolve_backup_dir(project_dir, args.backup_dir)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    inventory = json.loads((backup_dir / "packages_inventory.json").read_text(encoding="utf-8"))
    inv_by_pkg = {row["package_name"]: row for row in inventory}
    settings_snapshot = json.loads((backup_dir / "settings_snapshot.json").read_text(encoding="utf-8"))

    adb_path = find_adb()
    serial = resolve_device_serial(preferred_serial=args.serial)
    dynamic_protected = detect_dynamic_protected_packages(serial, adb_path)
    validate_plan_safety(plan, dynamic_protected=dynamic_protected)

    for item in plan.get("packages", []):
        ok = apply_package_action(
            item["package_name"], item["action"], inv_by_pkg, dynamic_protected, serial, adb_path, backup_dir
        )
        if not ok:
            raise RuntimeError(f"Stopped on unexpected failure for package {item['package_name']}")

    for item in plan.get("settings", []):
        ok = apply_setting_action(
            item["namespace"], item["key"], item.get("new_value"), settings_snapshot, serial, adb_path, backup_dir
        )
        if not ok:
            raise RuntimeError(f"Stopped on unexpected failure for setting {item['namespace']}/{item['key']}")

    for item in plan.get("appops", []):
        ok = apply_appop_action(
            item["package_name"], item["op"], item["new_mode"], item["old_mode"], serial, adb_path, backup_dir
        )
        if not ok:
            raise RuntimeError(f"Stopped on unexpected failure for appop {item['package_name']}:{item['op']}")

    if plan.get("wm_density") is not None:
        ok = apply_wm_density_action(plan["wm_density"], settings_snapshot, serial, adb_path, backup_dir)
        if not ok:
            raise RuntimeError("Stopped on unexpected failure for wm_density")

    print("All approved actions completed and logged.")


if __name__ == "__main__":
    main()
