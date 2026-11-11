"""Phase 7-8 Controlled & Guardrailed Executor for Clean MIUI AI.

Applies ONLY explicitly approved JSON action plans (`approved: true`), enforces
the hard protected-package safety blocklist before running any ADB mutation,
and logs every action + previous state to changelog.json and CHANGELOG.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from adb_helper import find_adb, resolve_device_serial, run_adb
from safety import (
    check_device_identity_match,
    is_protected_package,
    read_live_device_identity,
    resolve_backup_dir,
    verify_apk_manifest,
)

_PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")


def detect_dynamic_protected_packages(serial: str, adb_path: str) -> set[str]:
    """Detect active default IME, Home launcher, Dialer, and SMS packages dynamically."""
    protected: set[str] = set()
    ime_res = run_adb(["shell", "settings", "get", "secure", "default_input_method"], serial=serial, adb_path=adb_path)
    ime_val = ime_res.stdout.strip()
    if "/" in ime_val:
        pkg = ime_val.split("/", 1)[0].strip()
        if _PKG_RE.match(pkg):
            protected.add(pkg)

    home_res = run_adb(
        ["shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"],
        serial=serial,
        adb_path=adb_path,
    )
    for line in home_res.stdout.splitlines():
        if "/" in line and not line.startswith("priority="):
            pkg = line.strip().split("/", 1)[0].strip()
            if _PKG_RE.match(pkg):
                protected.add(pkg)

    for role in ("android.app.role.DIALER", "android.app.role.SMS"):
        role_res = run_adb(["shell", "cmd", "role", "get-role-holders", role], serial=serial, adb_path=adb_path)
        if role_res.returncode == 0:
            for token in role_res.stdout.strip().split():
                clean_tok = token.strip().strip("[](),")
                if _PKG_RE.match(clean_tok):
                    protected.add(clean_tok)
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


def validate_backup_before_apply(
    plan: dict[str, Any],
    backup_dir: Path,
    inv_by_pkg: dict[str, dict[str, Any]],
    apk_manifest: dict[str, Any],
    allow_unrecoverable: bool = False,
) -> dict[str, list[Path]]:
    """Verify local APK integrity and ensure non-system removals remain recoverable."""
    verified_apks, corrupt_apks = verify_apk_manifest(backup_dir, apk_manifest)
    if corrupt_apks:
        bad = ", ".join(f"{c['package_name']} ({c['reason']})" for c in corrupt_apks)
        raise RuntimeError(f"BACKUP INTEGRITY FAILURE before apply: {bad}")

    for item in plan.get("packages", []):
        pkg = item["package_name"]
        action = item.get("action")
        if action != "uninstall_user0":
            continue
        row = inv_by_pkg.get(pkg)
        if row and row.get("installed_user0") and not row.get("is_system") and pkg not in verified_apks:
            if not allow_unrecoverable:
                raise RuntimeError(
                    f"UNRECOVERABLE REMOVAL BLOCKED: '{pkg}' is a non-system package with no verified local APK "
                    "backup. Use action 'disable_user' or take a full APK backup first (or pass --allow-unrecoverable-uninstall)."
                )
    return verified_apks


def write_execution_report(backup_dir: Path, report: dict[str, Any]) -> Path:
    """Write timestamped execution_report JSON & Markdown inside backup_dir."""
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = backup_dir / f"execution_report_{ts}.json"
    md_path = backup_dir / f"execution_report_{ts}.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    vr = report.get("verification_results", {})
    bc = report.get("backup_completeness", {})
    rr = report.get("restore_readiness", {})
    md = [
        f"# Execution Report ({report['overall_status'].upper()})",
        "",
        f"- **Timestamp:** {report['timestamp']}",
        f"- **Device Serial:** `{report['serial']}`",
        f"- **Backup Dir:** `{report['backup_dir']}`",
        "",
        "## 1. Detected Applications",
        f"- Total baseline packages: {report['detected_applications']['total_baseline_packages']}",
        f"- Installed user 0: {report['detected_applications']['installed_user0']}",
        "",
        "## 2. Removed Applications (`uninstall_user0`)",
        f"- Count: {len(report['removed_applications'])}",
        *(f"- `{json.dumps(entry)}`" for entry in report["removed_applications"]),
        "",
        "## 3. Disabled Applications (`disable_user`)",
        f"- Count: {len(report['disabled_applications'])}",
        *(f"- `{json.dumps(entry)}`" for entry in report["disabled_applications"]),
        "",
        "## 4. Skipped Applications",
        f"- Count: {len(report['skipped_applications'])}",
        *(f"- `{json.dumps(entry)}`" for entry in report["skipped_applications"]),
        "",
        "## 5. Modified Advertisement, AppOps & Display Settings",
        f"- Count: {len(report['modified_advertisement_settings'])}",
        *(f"- `{json.dumps(entry)}`" for entry in report["modified_advertisement_settings"]),
        "",
        "## 6. Backup Completeness",
        f"- Verified APK Backups: {bc.get('apk_backups_verified', bc.get('verified_apks_live_check', 0))}",
        f"- Skipped/Failed APK Pulls: {bc.get('apk_backups_skipped_or_failed', 0)}",
        f"- Private `/data/data` backed up: {bc.get('private_app_data_backed_up', False)}",
        "",
        "## 7. Restore Readiness",
        f"- Status: {rr.get('status', 'unknown')}",
        *(
            [f"- Unrecoverable Removed Packages (No Local APK Backup): {', '.join(rr['unrecoverable_packages'])}"]
            if rr.get("unrecoverable_packages")
            else []
        ),
        f"- Note: {rr.get('note', '')}",
        "",
        "## 8. Failed Operations",
        f"- Count: {len(report['failed_operations'])}",
        *(f"- `{json.dumps(entry)}`" for entry in report["failed_operations"]),
        "",
        "## 9. Verification Results",
        f"- Overall Status: **{vr.get('overall_status', 'PENDING_VERIFY_DEVICE')}**",
        f"- Post-change `verify_device.py` executed: {vr.get('post_change_verification_ran', False)}",
        f"- Note: {vr.get('note', '')}",
        "",
    ]

    md_path.write_text("\n".join(md), encoding="utf-8")
    return md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply an approved Clean MIUI AI plan")
    parser.add_argument("--plan", required=True, help="Path to approved JSON action plan")
    parser.add_argument("--backup-dir", help="Backup folder (defaults to LATEST_BACKUP.txt)")
    parser.add_argument("--serial", help="Explicit ADB serial")
    parser.add_argument(
        "--force-device-mismatch",
        action="store_true",
        help="Allow running even if connected phone properties do not match backup device_info.json",
    )
    parser.add_argument(
        "--allow-unrecoverable-uninstall",
        action="store_true",
        help="Allow uninstall_user0 on non-system packages without verified local APK backups",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent.parent
    backup_dir = resolve_backup_dir(project_dir, args.backup_dir)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    inventory = json.loads((backup_dir / "packages_inventory.json").read_text(encoding="utf-8"))
    inv_by_pkg = {row["package_name"]: row for row in inventory}
    settings_snapshot = json.loads((backup_dir / "settings_snapshot.json").read_text(encoding="utf-8"))
    apk_manifest = json.loads((backup_dir / "apk_manifest.json").read_text(encoding="utf-8"))
    device_info = (
        json.loads((backup_dir / "device_info.json").read_text(encoding="utf-8"))
        if (backup_dir / "device_info.json").is_file()
        else {}
    )

    adb_path = find_adb()
    serial = resolve_device_serial(
        preferred_serial=args.serial,
        model_hint=device_info.get("model"),
    )

    live_id = read_live_device_identity(serial, adb_path, run_adb)
    baseline_ref = plan.get("target_device") or device_info
    if baseline_ref:
        id_ok, id_msg = check_device_identity_match(baseline_ref, live_id)
        if not id_ok and not args.force_device_mismatch:
            raise RuntimeError(f"DEVICE IDENTITY MISMATCH: {id_msg} (Pass --force-device-mismatch to override.)")

    dynamic_protected = detect_dynamic_protected_packages(serial, adb_path)
    validate_plan_safety(plan, dynamic_protected=dynamic_protected)
    verified_apks = validate_backup_before_apply(
        plan=plan,
        backup_dir=backup_dir,
        inv_by_pkg=inv_by_pkg,
        apk_manifest=apk_manifest,
        allow_unrecoverable=args.allow_unrecoverable_uninstall,
    )

    report: dict[str, Any] = {
        "timestamp": dt.datetime.now().isoformat(),
        "serial": serial,
        "backup_dir": str(backup_dir),
        "plan_path": str(args.plan),
        "overall_status": "failed",
        "detected_applications": {
            "total_baseline_packages": len(inventory),
            "installed_user0": sum(1 for r in inventory if r.get("installed_user0")),
        },
        "removed_applications": [],
        "disabled_applications": [],
        "skipped_applications": [
            {"package_name": s["package_name"], "reason": f"Prerequisite withheld: {s.get('note', '')}"}
            for s in plan.get("skipped_prerequisites_review", [])
        ],
        "modified_advertisement_settings": [],
        "backup_completeness": {
            **apk_manifest.get("completeness_summary", {}),
            "verified_apks_live_check": len(verified_apks),
            "private_app_data_backed_up": False,
        },
        "restore_readiness": {
            "status": "ready" if len(verified_apks) == len(apk_manifest.get("backed_up_packages", {})) else "degraded",
            "note": (
                "System apps recover via install-existing; disabled apps via pm enable; "
                "non-system APKs require verified local APK + Xiaomi 'Install via USB'."
            ),
        },
        "failed_operations": [],
        "verification_results": {
            "post_change_verification_ran": False,
            "overall_status": "FAILED",
            "note": "Run scripts/verify_device.py after apply_changes.py to verify live device health and restore dry-run readiness.",
        },
    }

    unrecoverable_removals: list[str] = []
    error_to_raise: RuntimeError | None = None
    try:
        for item in plan.get("packages", []):
            pkg = item["package_name"]
            action = item["action"]
            row = inv_by_pkg.get(pkg)
            if not row or not row.get("installed_user0"):
                report["skipped_applications"].append(
                    {"package_name": pkg, "action": action, "reason": "Not installed for user 0 in baseline snapshot"}
                )
                continue
            if action == "disable_user" and row.get("enabled_code_user0") in (2, 3):
                report["skipped_applications"].append(
                    {"package_name": pkg, "action": action, "reason": "Already disabled for user 0 in baseline snapshot"}
                )
                continue

            ok = apply_package_action(pkg, action, inv_by_pkg, dynamic_protected, serial, adb_path, backup_dir)
            if not ok:
                report["failed_operations"].append({"type": "package", "package_name": pkg, "action": action})
                error_to_raise = RuntimeError(f"Stopped on unexpected failure for package {pkg}")
                break

            has_local_apk = pkg in verified_apks
            if action == "uninstall_user0":
                if row.get("is_system"):
                    recovery_source = "system_partition"
                    recoverable = True
                elif has_local_apk:
                    recovery_source = "local_apk_backup"
                    recoverable = True
                else:
                    recovery_source = "unrecoverable_no_apk_backup"
                    recoverable = False
                    unrecoverable_removals.append(pkg)
            else:
                recovery_source = "pm_enable_state"
                recoverable = True

            entry: dict[str, Any] = {
                "package_name": pkg,
                "action": action,
                "is_system": bool(row.get("is_system")),
                "recovery_source": recovery_source,
                "recoverable": recoverable,
            }
            if not recoverable:
                entry["recovery_warning"] = (
                    "Uninstalled via --allow-unrecoverable-uninstall without a verified local APK backup; "
                    "cannot be restored locally from this backup."
                )
            if action == "uninstall_user0":
                report["removed_applications"].append(entry)
            else:
                report["disabled_applications"].append(entry)

        if error_to_raise is None:
            for item in plan.get("settings", []):
                ok = apply_setting_action(
                    item["namespace"], item["key"], item.get("new_value"), settings_snapshot, serial, adb_path, backup_dir
                )
                if not ok:
                    report["failed_operations"].append(
                        {"type": "setting", "target": f"{item['namespace']}/{item['key']}"}
                    )
                    error_to_raise = RuntimeError(
                        f"Stopped on unexpected failure for setting {item['namespace']}/{item['key']}"
                    )
                    break
                report["modified_advertisement_settings"].append(
                    {"type": "setting", "namespace": item["namespace"], "key": item["key"], "new_value": item.get("new_value")}
                )

        if error_to_raise is None:
            for item in plan.get("appops", []):
                ok = apply_appop_action(
                    item["package_name"], item["op"], item["new_mode"], item["old_mode"], serial, adb_path, backup_dir
                )
                if not ok:
                    report["failed_operations"].append(
                        {"type": "appop", "target": f"{item['package_name']}:{item['op']}"}
                    )
                    error_to_raise = RuntimeError(
                        f"Stopped on unexpected failure for appop {item['package_name']}:{item['op']}"
                    )
                    break
                report["modified_advertisement_settings"].append(
                    {"type": "appop", "package_name": item["package_name"], "op": item["op"], "new_mode": item["new_mode"]}
                )

        if error_to_raise is None and plan.get("wm_density") is not None:
            ok = apply_wm_density_action(plan["wm_density"], settings_snapshot, serial, adb_path, backup_dir)
            if not ok:
                report["failed_operations"].append({"type": "wm_density", "target": "display_density"})
                error_to_raise = RuntimeError("Stopped on unexpected failure for wm_density")
            else:
                report["modified_advertisement_settings"].append(
                    {"type": "wm_density", "new_value": plan["wm_density"]}
                )

        if error_to_raise is None:
            report["overall_status"] = "success"
            report["verification_results"]["overall_status"] = "PENDING_VERIFY_DEVICE"
        elif report["removed_applications"] or report["disabled_applications"] or report["modified_advertisement_settings"]:
            report["overall_status"] = "partial_failure"
            report["verification_results"]["overall_status"] = "FAILED"
        else:
            report["overall_status"] = "failed"
            report["verification_results"]["overall_status"] = "FAILED"
    finally:
        if unrecoverable_removals:
            report["restore_readiness"] = {
                "status": "unrecoverable_removals_present",
                "unrecoverable_packages": list(unrecoverable_removals),
                "allow_unrecoverable_uninstall_used": bool(args.allow_unrecoverable_uninstall),
                "note": (
                    f"{len(unrecoverable_removals)} non-system package(s) were uninstalled via "
                    "--allow-unrecoverable-uninstall without verified local APK backup and CANNOT be "
                    "restored locally from this backup: "
                    + ", ".join(unrecoverable_removals)
                ),
            }
        md_path = write_execution_report(backup_dir, report)
        print(f"Execution report written to: {md_path}")

    if error_to_raise is not None:
        raise error_to_raise

    print(
        f"Completed ({report['overall_status']}): "
        f"removed={len(report['removed_applications'])}, "
        f"disabled={len(report['disabled_applications'])}, "
        f"skipped={len(report['skipped_applications'])}, "
        f"settings_appops={len(report['modified_advertisement_settings'])}."
    )


if __name__ == "__main__":
    main()
