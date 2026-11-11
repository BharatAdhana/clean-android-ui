"""Phase 9 Post-Cleanup Health & Functional Verification Script."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adb_helper import find_adb, resolve_device_serial, run_adb
from restore import filter_applied_modifications
from safety import (
    check_device_identity_match,
    read_live_device_identity,
    resolve_backup_dir,
    verify_apk_manifest,
)

CORE_CHECKS = [
    ("System UI", "com.android.systemui"),
    ("Phone / Telephony", "com.android.phone"),
    ("Settings", "com.android.settings"),
    ("Google Play Services", "com.google.android.gms"),
    ("Google Play Store", "com.android.vending"),
]

ALWAYS_REQUIRED_PACKAGES = {
    "com.android.systemui",
    "com.android.phone",
    "com.android.settings",
}


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _extract_inventory_items(inventory_raw: Any) -> list[dict[str, Any]]:
    if isinstance(inventory_raw, list):
        return [item for item in inventory_raw if isinstance(item, dict)]
    if isinstance(inventory_raw, dict):
        pkgs = inventory_raw.get("packages")
        if isinstance(pkgs, list):
            return [item for item in pkgs if isinstance(item, dict)]
    return []


def _pkg_name(entry: dict[str, Any]) -> str:
    return str(entry.get("package_name") or entry.get("package") or "").strip()


def _summarize_from_sources(
    inventory_items: list[dict[str, Any]],
    apk_manifest: dict[str, Any],
    changelog: dict[str, Any],
    exec_report: dict[str, Any] | None,
    valid_apks: dict[str, list[Path]],
    apk_problems: list[dict[str, str]],
) -> dict[str, Any]:
    modifications = changelog.get("modifications", []) if isinstance(changelog, dict) else []
    if not isinstance(modifications, list):
        modifications = []
    filtered = filter_applied_modifications(modifications)

    removed_apps: list[str] = []
    disabled_apps: list[str] = []
    for entry in filtered.get("packages", []):
        if not isinstance(entry, dict):
            continue
        pkg = _pkg_name(entry)
        action = str(entry.get("action") or entry.get("operation") or "").strip()
        if not pkg:
            continue
        if action in {"uninstall_user0", "uninstall"}:
            removed_apps.append(pkg)
        elif action in {"disable_user", "disable"}:
            disabled_apps.append(pkg)

    skipped_apps: list[dict[str, Any]] = []
    for entry in modifications:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").lower()
        action = str(entry.get("action") or "").lower()
        if status == "skipped" or action == "skip":
            skipped_apps.append(entry)

    if isinstance(exec_report, dict):
        if isinstance(exec_report.get("removed_applications"), list) and not removed_apps:
            removed_apps = [
                _pkg_name(item) if isinstance(item, dict) else str(item)
                for item in exec_report["removed_applications"]
                if (isinstance(item, dict) and _pkg_name(item)) or (not isinstance(item, dict) and str(item).strip())
            ]
        if isinstance(exec_report.get("disabled_applications"), list) and not disabled_apps:
            disabled_apps = [
                _pkg_name(item) if isinstance(item, dict) else str(item)
                for item in exec_report["disabled_applications"]
                if (isinstance(item, dict) and _pkg_name(item)) or (not isinstance(item, dict) and str(item).strip())
            ]
        if isinstance(exec_report.get("skipped_applications"), list):
            skipped_apps = list(exec_report["skipped_applications"])

    modified_settings = (
        list(filtered.get("settings", []))
        + list(filtered.get("appops", []))
        + list(filtered.get("wm_density", []))
    )
    if not modified_settings and isinstance(exec_report, dict) and isinstance(exec_report.get("modified_advertisement_settings"), list):
        modified_settings = list(exec_report["modified_advertisement_settings"])
    failed_ops = list(filtered.get("ignored_unsuccessful", []))
    if isinstance(exec_report, dict) and isinstance(exec_report.get("failed_operations"), list):
        for item in exec_report["failed_operations"]:
            if item not in failed_ops:
                failed_ops.append(item)

    backed_up_raw = apk_manifest.get("backed_up_packages", {}) if isinstance(apk_manifest, dict) else {}
    skipped_pull_raw = apk_manifest.get("skipped_or_failed", []) if isinstance(apk_manifest, dict) else []
    excluded_raw = apk_manifest.get("excluded_from_apk_pull", []) if isinstance(apk_manifest, dict) else []
    completeness_summary = apk_manifest.get("completeness_summary", {}) if isinstance(apk_manifest, dict) else {}

    backup_completeness = {
        "verified_apk_packages": len(valid_apks),
        "manifest_backed_up_count": len(backed_up_raw) if isinstance(backed_up_raw, (dict, list)) else 0,
        "skipped_or_failed_apk_pulls": len(skipped_pull_raw) if isinstance(skipped_pull_raw, (dict, list)) else 0,
        "excluded_from_apk_pull": len(excluded_raw) if isinstance(excluded_raw, (dict, list)) else 0,
        "apk_integrity_problems": apk_problems,
        "completeness_summary": completeness_summary,
        "private_app_data_backed_up": False,
        "private_data_note": "Non-root ADB cannot back up internal /data/data application databases, logins, or launcher layouts.",
    }

    return {
        "detected_applications": {
            "total_baseline_packages": len(inventory_items),
        },
        "removed_applications": sorted(set(removed_apps)),
        "disabled_applications": sorted(set(disabled_apps)),
        "skipped_applications": skipped_apps,
        "modified_advertisement_settings": modified_settings,
        "backup_completeness": backup_completeness,
        "failed_operations": failed_ops,
    }


def write_verification_report(backup_dir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = backup_dir / f"verification_report_{ts}.json"
    md_path = backup_dir / f"verification_report_{ts}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    vr = report.get("verification_results", {})
    bc = report.get("backup_completeness", {})
    rr = report.get("restore_readiness", {})
    lines = [
        "# Verification Report",
        "",
        f"- Generated UTC: `{report.get('generated_at_utc', '')}`",
        f"- Backup Directory: `{backup_dir.name}`",
        f"- Overall Status: **{vr.get('overall_status', 'UNKNOWN')}**",
        "",
        "## 1. Detected Applications",
        f"- Baseline detected packages: {report.get('detected_applications', {}).get('total_baseline_packages', 0)}",
        "",
        "## 2. Removed Applications (`uninstall --user 0`)",
        f"- Count: {len(report.get('removed_applications', []))}",
        *(f"  - `{pkg}`" for pkg in report.get("removed_applications", [])),
        "",
        "## 3. Disabled Applications (`disable-user`)",
        f"- Count: {len(report.get('disabled_applications', []))}",
        *(f"  - `{pkg}`" for pkg in report.get("disabled_applications", [])),
        "",
        "## 4. Skipped Applications",
        f"- Count: {len(report.get('skipped_applications', []))}",
        "",
        "## 5. Modified Advertisement, AppOps & Display Settings",
        f"- Applied setting/AppOps/display density changes: {len(report.get('modified_advertisement_settings', []))}",
        "",
        "## 6. Backup Completeness",
        f"- Verified local APK packages: {bc.get('verified_apk_packages', 0)}",
        f"- Excluded from APK pull: {bc.get('excluded_from_apk_pull', 0)}",
        f"- Skipped/failed APK pulls: {bc.get('skipped_or_failed_apk_pulls', 0)}",
        f"- APK integrity problems: {len(bc.get('apk_integrity_problems', []))}",
        f"- Private `/data/data` backup: No ({bc.get('private_data_note', '')})",
        "",
        "## 7. Restore Readiness",
        f"- Restore dry-run status: {'PASS' if rr.get('dry_run_ok') else 'FAIL'}",
        f"- Disabled apps re-enablement: Offline `pm enable` (no APK required)",
        f"- Uninstalled non-system apps: Require verified APK + ADB install permission (`Install via USB` / security toggle on MIUI)",
        "",
        "## 8. Failed Operations",
        f"- Count: {len(report.get('failed_operations', []))}",
        "",
        "## 9. Verification Results",
        f"- Identity check matched: {vr.get('identity_matched')}",
        f"- Active Home Launcher: `{vr.get('active_home_launcher', '')}`",
        f"- Active Keyboard (IME): `{vr.get('active_ime', '')}`",
        f"- Crash buffer lines (tail 20): {vr.get('crash_buffer_lines', 0)}",
        f"- Core package failures: {vr.get('core_failures', 0)}",
        f"- Overall Status: **{vr.get('overall_status', 'UNKNOWN')}**",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify device stability & recovery readiness after cleanup")
    parser.add_argument("--backup-dir", help="Explicit backup dir")
    parser.add_argument("--serial", help="Explicit ADB serial")
    parser.add_argument(
        "--force-device-mismatch",
        action="store_true",
        help="Continue even if live device hardware does not match device_info.json in the backup",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent.parent
    backup_dir = resolve_backup_dir(project_dir, args.backup_dir)

    device_info = _load_json(backup_dir / "device_info.json") or {}
    inventory_items = _extract_inventory_items(_load_json(backup_dir / "packages_inventory.json"))
    apk_manifest = _load_json(backup_dir / "apk_manifest.json") or {}
    changelog = _load_json(backup_dir / "changelog.json") or {}
    exec_reports = sorted(backup_dir.glob("execution_report_*.json"))
    exec_report = _load_json(exec_reports[-1]) if exec_reports else None

    adb_path = find_adb()
    serial = resolve_device_serial(preferred_serial=args.serial)

    identity_matched = True
    identity_message = "No baseline device_info.json present to compare."
    if isinstance(device_info, dict) and device_info:
        live_info = read_live_device_identity(serial, adb_path, run_adb)
        identity_matched, identity_message = check_device_identity_match(device_info, live_info)
        print("=== DEVICE IDENTITY CHECK ===")
        print(f"  {identity_message}")
        if not identity_matched and not args.force_device_mismatch:
            print("ABORTING: Live device does not match baseline backup. Re-run with --force-device-mismatch only if intentional.", file=sys.stderr)
            raise SystemExit(2)

    installed_raw = run_adb(["shell", "pm", "list", "packages", "--user", "0"], serial=serial, adb_path=adb_path).stdout
    installed = {
        l[len("package:") :].strip()
        for l in installed_raw.splitlines()
        if l.strip().startswith("package:")
    }

    baseline_pkgs = {
        _pkg_name(item)
        for item in inventory_items
        if _pkg_name(item) and item.get("installed_user0", item.get("installed", True))
    }

    print("=== CRITICAL SERVICES CHECK ===")
    failures = 0
    core_results: list[dict[str, Any]] = []
    for label, pkg in CORE_CHECKS:
        ok = pkg in installed
        required = pkg in ALWAYS_REQUIRED_PACKAGES or not baseline_pkgs or pkg in baseline_pkgs
        if not required and not ok:
            status_text = "SKIPPED (not present at baseline)"
        else:
            status_text = "OK" if ok else "MISSING!"
            if not ok:
                failures += 1
        print(f"  - {label} (`{pkg}`): {status_text}")
        core_results.append({"label": label, "package": pkg, "required": required, "present": ok, "status": status_text})

    home_out = run_adb(
        ["shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"],
        serial=serial,
        adb_path=adb_path,
    ).stdout.strip()
    print(f"  - Active Home Launcher: {home_out}")

    ime_out = run_adb(["shell", "settings", "get", "secure", "default_input_method"], serial=serial, adb_path=adb_path).stdout.strip()
    print(f"  - Active Keyboard (IME): {ime_out}")

    crash_tail = run_adb(["logcat", "-b", "crash", "-d", "-t", "20"], serial=serial, adb_path=adb_path).stdout.strip()
    crash_lines = len(crash_tail.splitlines()) if crash_tail else 0
    print(f"  - Crash buffer lines (tail 20): {crash_lines}")

    valid_apks, apk_problems = verify_apk_manifest(backup_dir, apk_manifest) if isinstance(apk_manifest, dict) and apk_manifest else ({}, [])
    if apk_problems:
        failures += len(apk_problems)

    print("\n=== RESTORE DRY-RUN CHECK ===")
    dry_cmd = [sys.executable, str(project_dir / "scripts" / "restore.py"), "--dry-run", "--backup-dir", str(backup_dir), "--serial", serial]
    if args.force_device_mismatch:
        dry_cmd.append("--force-device-mismatch")
    restore_res = subprocess.run(
        dry_cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(restore_res.stdout.strip())
    dry_run_ok = restore_res.returncode == 0
    if not dry_run_ok:
        print(restore_res.stderr.strip(), file=sys.stderr)
        failures += 1

    summary = _summarize_from_sources(
        inventory_items=inventory_items,
        apk_manifest=apk_manifest if isinstance(apk_manifest, dict) else {},
        changelog=changelog if isinstance(changelog, dict) else {},
        exec_report=exec_report,
        valid_apks=valid_apks,
        apk_problems=apk_problems,
    )
    core_failures = sum(1 for r in core_results if r["required"] and not r["present"])
    overall_ok = failures == 0

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "backup_dir": str(backup_dir),
        "latest_execution_report": exec_reports[-1].name if exec_reports else None,
        **summary,
        "restore_readiness": {
            "dry_run_ok": dry_run_ok,
            "verified_apk_packages": len(valid_apks),
            "apk_integrity_problems": apk_problems,
            "note": "Dry-run checks local backup files and plans only; real restore also requires ADB permissions on the live device.",
        },
        "verification_results": {
            "identity_matched": identity_matched,
            "identity_message": identity_message,
            "core_checks": core_results,
            "core_failures": core_failures,
            "active_home_launcher": home_out,
            "active_ime": ime_out,
            "crash_buffer_lines": crash_lines,
            "restore_dry_run_ok": dry_run_ok,
            "overall_status": "PASS" if overall_ok else "FAIL",
        },
    }
    json_path, md_path = write_verification_report(backup_dir, report)
    print(f"\nVerification report saved:\n  - {json_path}\n  - {md_path}")

    raise SystemExit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
