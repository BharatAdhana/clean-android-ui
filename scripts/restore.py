"""Dedicated Cross-Platform Restore Engine for Clean MIUI AI.

Restores uninstalled system/preloaded packages, disabled states, verified local
APK backups, settings, AppOps rules, and display density overrides recorded
in changelog.json back to the baseline snapshot.
Supports --dry-run verification before applying any changes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from adb_helper import find_adb, resolve_device_serial, run_adb
from safety import (
    check_device_identity_match,
    read_live_device_identity,
    resolve_backup_dir,
    verify_apk_manifest,
)


def get_live_user0_packages(serial: str, adb_path: str) -> tuple[set[str], set[str]]:
    """Return (installed_user0, disabled_user0) package sets from the connected device."""
    res_inst = run_adb(["shell", "pm list packages --user 0"], serial=serial, adb_path=adb_path)
    res_dis = run_adb(["shell", "pm list packages -d --user 0"], serial=serial, adb_path=adb_path)
    if res_inst.returncode != 0:
        raise RuntimeError(f"Failed to query live packages: {res_inst.stderr.strip()}")
    installed = {
        line[len("package:") :].strip()
        for line in res_inst.stdout.splitlines()
        if line.strip().startswith("package:")
    }
    disabled = {
        line[len("package:") :].strip()
        for line in res_dis.stdout.splitlines()
        if line.strip().startswith("package:")
    }
    return installed, disabled


def verify_backup_apks(backup_dir: Path, apk_manifest: dict[str, Any]) -> dict[str, list[Path]]:
    """Verify SHA-256 and ZIP integrity for every backed-up APK bundle."""
    verified, _ = verify_apk_manifest(backup_dir, apk_manifest)
    return verified


def classify_restoration_mechanism(baseline_entry: dict[str, Any]) -> str:
    """Distinguish system partition re-enablement from local APK reinstallation."""
    return "system_install_existing" if baseline_entry.get("is_system") else "local_apk_install"


def filter_applied_modifications(modifications: list[dict[str, Any]]) -> dict[str, Any]:
    """Filter changelog records to successful edits only, keeping earliest pre-change baseline."""
    ignored_unsuccessful: list[dict[str, Any]] = []
    pkg_mods_by_target: dict[str, dict[str, Any]] = {}
    setting_mods_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    appop_mods_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    wm_first_mod: dict[str, Any] | None = None

    for mod in modifications:
        if mod.get("success") is not True:
            ignored_unsuccessful.append(
                {
                    "type": mod.get("type"),
                    "target": mod.get("target") or mod.get("package_name") or mod.get("key"),
                    "action": mod.get("action"),
                    "detail": mod.get("detail", "Recorded as unsuccessful"),
                }
            )
            continue

        mtype = mod.get("type")
        if mtype == "package":
            pkg = mod.get("package_name") or mod.get("target")
            if pkg and pkg not in pkg_mods_by_target:
                pkg_mods_by_target[pkg] = mod
        elif mtype == "setting":
            skey = (mod["namespace"], mod["key"])
            if skey not in setting_mods_by_key:
                setting_mods_by_key[skey] = mod
        elif mtype == "appop":
            akey = (mod["package_name"], mod["op"])
            if akey not in appop_mods_by_key:
                appop_mods_by_key[akey] = mod
        elif mtype == "wm_density" and wm_first_mod is None:
            wm_first_mod = mod

    return {
        "ignored_unsuccessful": ignored_unsuccessful,
        "packages": list(pkg_mods_by_target.values()),
        "settings": list(setting_mods_by_key.values()),
        "appops": list(appop_mods_by_key.values()),
        "wm_density": [wm_first_mod] if wm_first_mod is not None else [],
    }


def restore_package(
    pkg: str,
    baseline_entry: dict[str, Any],
    verified_apks: dict[str, list[Path]],
    serial: str,
    adb_path: str,
    dry_run: bool,
) -> tuple[bool, str]:
    """Restore a package that was originally installed for Android user 0."""
    install_existing_cmd = ["shell", f"cmd package install-existing --user 0 {pkg}"]
    if baseline_entry.get("is_system") and dry_run:
        return True, f"[DRY-RUN] Would run: adb {' '.join(install_existing_cmd)}"

    if not dry_run:
        existing_res = run_adb(install_existing_cmd, serial=serial, adb_path=adb_path)
        existing_out = (existing_res.stdout + "\n" + existing_res.stderr).strip()
        if existing_res.returncode == 0 and (
            "installed for user" in existing_out.lower()
            or (baseline_entry.get("is_system") and "package" in existing_out.lower())
        ):
            return True, existing_out
        if baseline_entry.get("is_system"):
            return False, f"Exit {existing_res.returncode}: {existing_out}"

    apk_files = verified_apks.get(pkg)
    if not apk_files:
        return False, "No verified local APK backup available for non-system package."

    if len(apk_files) == 1:
        cmd = ["install", "-r", str(apk_files[0])]
    else:
        cmd = ["install-multiple", "-r", *[str(p) for p in apk_files]]

    if dry_run:
        return True, f"[DRY-RUN] Would run: adb {' '.join(cmd)}"

    res = run_adb(cmd, serial=serial, timeout=180, adb_path=adb_path)
    combined = (res.stdout + "\n" + res.stderr).strip()
    if res.returncode == 0 and "Success" in combined:
        return True, combined
    hint = (
        " (Enable Developer options -> 'Install via USB' on Xiaomi/MIUI and retry)"
        if "INSTALL_FAILED_USER_RESTRICTED" in combined
        else ""
    )
    return False, f"Exit {res.returncode}: {combined}{hint}"


def restore_enabled_state(
    pkg: str, target_code: int, serial: str, adb_path: str, dry_run: bool
) -> tuple[bool, str]:
    """Restore package enabled/disabled state for user 0."""
    if target_code == 0:
        subcmd = f"pm default-state --user 0 {pkg}"
    elif target_code in (2, 3):
        subcmd = f"pm disable-user --user 0 {pkg}"
    else:
        subcmd = f"pm enable --user 0 {pkg}"

    if dry_run:
        return True, f"[DRY-RUN] Would run: adb shell {subcmd}"
    res = run_adb(["shell", subcmd], serial=serial, adb_path=adb_path)
    combined = (res.stdout + "\n" + res.stderr).strip()
    if res.returncode == 0 and "SecurityException" not in combined and "Exception" not in combined and "Error:" not in combined:
        return True, combined or "OK"
    return False, f"Exit {res.returncode}: {combined}"


def restore_setting(
    ns: str, key: str, original_value: str | None, serial: str, adb_path: str, dry_run: bool
) -> tuple[bool, str]:
    """Restore a system/secure/global setting value."""
    if original_value is None:
        subcmd = f"settings delete {ns} {key}"
    else:
        escaped = original_value.replace("'", "'\"'\"'")
        subcmd = f"settings put {ns} {key} '{escaped}'"

    if dry_run:
        return True, f"[DRY-RUN] Would run: adb shell {subcmd}"
    res = run_adb(["shell", subcmd], serial=serial, adb_path=adb_path)
    combined = (res.stdout + "\n" + res.stderr).strip()
    if res.returncode == 0 and "SecurityException" not in combined and "Exception" not in combined:
        return True, combined or "OK"
    return False, f"Exit {res.returncode}: {combined}"


def restore_appop(
    pkg: str, op: str, original_mode: str, serial: str, adb_path: str, dry_run: bool
) -> tuple[bool, str]:
    """Restore an AppOps permission/background mode."""
    subcmd = f"cmd appops set {pkg} {op} {original_mode}"
    if dry_run:
        return True, f"[DRY-RUN] Would run: adb shell {subcmd}"
    res = run_adb(["shell", subcmd], serial=serial, adb_path=adb_path)
    combined = (res.stdout + "\n" + res.stderr).strip()
    if res.returncode == 0 and "Exception" not in combined:
        return True, combined or "OK"
    return False, f"Exit {res.returncode}: {combined}"


def restore_wm_density(original_density: str | None, serial: str, adb_path: str, dry_run: bool) -> tuple[bool, str]:
    """Restore overridden display density back to baseline."""
    subcmd = f"wm density {original_density}" if original_density else "wm density reset"
    if dry_run:
        return True, f"[DRY-RUN] Would run: adb shell {subcmd}"
    res = run_adb(["shell", subcmd], serial=serial, adb_path=adb_path)
    combined = (res.stdout + "\n" + res.stderr).strip()
    if res.returncode == 0 and "Exception" not in combined:
        return True, combined or "OK"
    return False, f"Exit {res.returncode}: {combined}"


def find_drifted_packages(
    inventory: list[dict[str, Any]],
    live_installed: set[str],
    live_disabled: set[str],
    serial: str,
    adb_path: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare live device packages against baseline inventory safely."""
    missing_pkgs: list[dict[str, Any]] = []
    state_mismatch_pkgs: list[dict[str, Any]] = []

    for row in inventory:
        pkg = row["package_name"]
        was_installed = bool(row.get("installed_user0", False))
        orig_enabled_code = int(row.get("enabled_code_user0", 0))

        if was_installed and pkg not in live_installed:
            if row.get("is_system") and "HAS_CODE" not in row.get("pkg_flags", []):
                chk = run_adb(["shell", f"dumpsys package {pkg} | grep -m1 'User 0:'"], serial=serial, adb_path=adb_path)
                if "installed=true" in chk.stdout:
                    continue
            missing_pkgs.append(row)
        elif was_installed and pkg in live_installed:
            is_now_disabled = pkg in live_disabled
            orig_is_disabled_u0 = orig_enabled_code in (2, 3)
            if orig_is_disabled_u0 != is_now_disabled:
                chk = run_adb(["shell", f"dumpsys package {pkg} | grep -m1 'User 0:'"], serial=serial, adb_path=adb_path)
                live_code = orig_enabled_code
                for token in chk.stdout.split():
                    if token.startswith("enabled="):
                        val = token.split("=", 1)[1]
                        if val.isdigit():
                            live_code = int(val)
                        break
                if live_code != orig_enabled_code:
                    state_mismatch_pkgs.append(row)

    return missing_pkgs, state_mismatch_pkgs


def verify_live_setting_restored(
    ns: str, key: str, expected_value: str | None, serial: str, adb_path: str
) -> tuple[bool, str]:
    """Query device setting live and verify it matches expected baseline value."""
    res = run_adb(["shell", f"settings get {ns} {key}"], serial=serial, adb_path=adb_path)
    actual = res.stdout.strip()
    if res.returncode != 0:
        return False, f"Failed to read setting {ns}/{key}: {res.stderr.strip()}"
    if expected_value is None:
        ok = actual in ("null", "")
        return ok, f"actual='{actual}' (expected deleted/'null')"
    return actual == expected_value, f"actual='{actual}' vs expected='{expected_value}'"


def verify_live_appop_restored(
    pkg: str, op: str, expected_mode: str, serial: str, adb_path: str
) -> tuple[bool, str]:
    """Query live AppOps mode and confirm it matches target mode."""
    res = run_adb(["shell", f"cmd appops get {pkg} {op}"], serial=serial, adb_path=adb_path)
    combined = (res.stdout + "\n" + res.stderr).strip()
    if res.returncode != 0:
        return False, f"Failed to query appop {pkg}:{op}: {combined}"
    if expected_mode == "default":
        ok = ("default" in combined.lower()) or ("No operations." in combined)
    else:
        ok = expected_mode in combined.lower()
    return ok, combined


def verify_live_wm_density_restored(expected_override: str | None, serial: str, adb_path: str) -> tuple[bool, str]:
    """Query live display density and verify override was restored or cleared."""
    res = run_adb(["shell", "wm", "density"], serial=serial, adb_path=adb_path)
    out = res.stdout.strip()
    if res.returncode != 0:
        return False, f"wm density failed: {res.stderr.strip()}"
    if expected_override:
        ok = f"Override density: {expected_override}" in out
    else:
        ok = "Override density:" not in out
    return ok, out


def verify_live_post_restore(
    restored_pkg_rows: list[dict[str, Any]],
    restored_settings: list[dict[str, Any]],
    restored_appops: list[dict[str, Any]],
    restored_wm: list[dict[str, Any]],
    serial: str,
    adb_path: str,
) -> dict[str, Any]:
    """Verify restored packages, enabled states, settings, appops, and wm density live on device."""
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    if restored_pkg_rows:
        live_inst, live_dis = get_live_user0_packages(serial, adb_path)
        for row in restored_pkg_rows:
            pkg = row["package_name"]
            orig_code = int(row.get("enabled_code_user0", 0))
            if pkg not in live_inst:
                failed.append({"type": "package", "package_name": pkg, "reason": "Still missing from pm list packages --user 0"})
                continue
            expect_disabled = orig_code in (2, 3)
            actual_disabled = pkg in live_dis
            if expect_disabled != actual_disabled:
                failed.append(
                    {
                        "type": "package_state",
                        "package_name": pkg,
                        "reason": f"Expected disabled={expect_disabled}, live disabled={actual_disabled}",
                    }
                )
            else:
                passed.append({"type": "package", "package_name": pkg, "enabled_code_user0": orig_code})

    for s in restored_settings:
        ok, detail = verify_live_setting_restored(s["namespace"], s["key"], s.get("restored_to"), serial, adb_path)
        rec = {"type": "setting", "target": f"{s['namespace']}/{s['key']}", "detail": detail}
        (passed if ok else failed).append(rec)

    for a in restored_appops:
        ok, detail = verify_live_appop_restored(a["package_name"], a["op"], a["restored_to"], serial, adb_path)
        rec = {"type": "appop", "target": f"{a['package_name']}:{a['op']}", "detail": detail}
        (passed if ok else failed).append(rec)

    for w in restored_wm:
        ok, detail = verify_live_wm_density_restored(w.get("restored_to"), serial, adb_path)
        rec = {"type": "wm_density", "target": "display_density", "detail": detail}
        (passed if ok else failed).append(rec)

    return {"passed": passed, "failed": failed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean MIUI AI - One-Click Restore")
    parser.add_argument("--backup-dir", help="Specific backup folder path")
    parser.add_argument(
        "--project-dir",
        help="Repository/project directory containing backups/ (defaults to script parent)",
    )
    parser.add_argument("--serial", help="Explicit ADB serial")
    parser.add_argument("--dry-run", action="store_true", help="Verify backup integrity & simulate plan without modifying device")
    parser.add_argument("--yes", action="store_true", help="Run non-interactively")
    parser.add_argument(
        "--scope",
        choices=("auto", "applied", "snapshot"),
        default="auto",
        help="'applied' restores only successful changelog modifications; 'snapshot' restores all baseline drift; 'auto' uses applied when changelog has entries",
    )
    parser.add_argument(
        "--force-device-mismatch",
        action="store_true",
        help="Allow restore even if connected phone properties do not match backup device_info.json",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip post-restore live device verification queries",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve() if args.project_dir else Path(__file__).resolve().parent.parent
    backup_dir = resolve_backup_dir(project_dir, args.backup_dir)

    inventory: list[dict[str, Any]] = json.loads((backup_dir / "packages_inventory.json").read_text(encoding="utf-8"))
    inv_by_pkg = {row["package_name"]: row for row in inventory}
    apk_manifest: dict[str, Any] = json.loads((backup_dir / "apk_manifest.json").read_text(encoding="utf-8"))
    changelog: dict[str, Any] = json.loads((backup_dir / "changelog.json").read_text(encoding="utf-8"))
    device_info: dict[str, Any] = (
        json.loads((backup_dir / "device_info.json").read_text(encoding="utf-8"))
        if (backup_dir / "device_info.json").is_file()
        else {}
    )

    print(f"Using backup: {backup_dir}")
    verified_apks, corrupt_apks = verify_apk_manifest(backup_dir, apk_manifest)
    total_backed_apks = len(apk_manifest.get("backed_up_packages", {}))
    print(f"Verified local APK backups: {len(verified_apks)}/{total_backed_apks}")
    if corrupt_apks:
        for bad in corrupt_apks:
            print(f"  [BACKUP INTEGRITY ERROR] {bad['package_name']}: {bad['reason']}")

    adb_path = find_adb()
    serial = resolve_device_serial(preferred_serial=args.serial, model_hint=device_info.get("model"))
    print(f"Target device: {serial}")

    if device_info:
        live_id = read_live_device_identity(serial, adb_path, run_adb)
        id_ok, id_msg = check_device_identity_match(device_info, live_id)
        if not id_ok and not args.force_device_mismatch:
            raise RuntimeError(f"DEVICE IDENTITY MISMATCH: {id_msg} (Pass --force-device-mismatch to override.)")

    raw_mods = changelog.get("modifications", [])
    applied_bundle = filter_applied_modifications(raw_mods)
    effective_scope = args.scope if args.scope != "auto" else ("applied" if raw_mods else "snapshot")

    live_installed, live_disabled = get_live_user0_packages(serial, adb_path)
    snapshot_missing, snapshot_mismatch = find_drifted_packages(inventory, live_installed, live_disabled, serial, adb_path)

    if effective_scope == "applied":
        applied_pkg_names = {m.get("package_name") or m.get("target") for m in applied_bundle["packages"]}
        missing_pkgs = [r for r in snapshot_missing if r["package_name"] in applied_pkg_names]
        state_mismatch_pkgs = [r for r in snapshot_mismatch if r["package_name"] in applied_pkg_names]
        external_drift = [
            r["package_name"]
            for r in (snapshot_missing + snapshot_mismatch)
            if r["package_name"] not in applied_pkg_names
        ]
    else:
        missing_pkgs = snapshot_missing
        state_mismatch_pkgs = snapshot_mismatch
        external_drift = []

    setting_mods = applied_bundle["settings"]
    appop_mods = applied_bundle["appops"]
    wm_mods = applied_bundle["wm_density"]
    ignored_unsuccessful = applied_bundle["ignored_unsuccessful"]

    print(f"Restore scope: {effective_scope} (ignored unsuccessful changelog entries: {len(ignored_unsuccessful)})")
    print(f"Packages needing reinstall: {len(missing_pkgs)}")
    print(f"Package enable states to restore: {len(state_mismatch_pkgs)}")
    print(f"Applied settings / appops / density changes to revert: {len(setting_mods)} / {len(appop_mods)} / {len(wm_mods)}")
    if external_drift:
        print(f"Note: {len(external_drift)} unrelated drift packages untouched under scope='applied' (use --scope snapshot to include).")

    has_restore_targets = bool(missing_pkgs or state_mismatch_pkgs or setting_mods or appop_mods or wm_mods)
    if not args.dry_run and not args.yes:
        if not has_restore_targets:
            if corrupt_apks:
                print(
                    f"[INTEGRITY FAILURE] Backup APK integrity check failed ({len(corrupt_apks)} corrupt/missing APK(s)); "
                    "cannot report a clean restore state."
                )
            else:
                print("Nothing to restore.")
        else:
            ans = input("Proceed with restoring device state from backup? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                print("Aborted by user.")
                return

    restored_pkg_rows_for_verify: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "timestamp": dt.datetime.now().isoformat(),
        "mode": "dry_run" if args.dry_run else "live",
        "dry_run": args.dry_run,
        "simulated": bool(args.dry_run),
        "scope_used": effective_scope,
        "overall_status": "unknown",
        "backup_dir": str(backup_dir),
        "serial": serial,
        "backup_integrity": {
            "apk_backups_verified": len(verified_apks),
            "apk_backups_total": total_backed_apks,
            "corrupt_or_missing_apks": corrupt_apks,
        },
        "ignored_unsuccessful_operations": ignored_unsuccessful,
        "external_drift_untouched": external_drift,
        "restored_packages_ok": [],
        "restored_packages_failed": [],
        "restored_states_ok": [],
        "restored_states_failed": [],
        "restored_settings_ok": [],
        "restored_settings_failed": [],
        "restored_appops_ok": [],
        "restored_appops_failed": [],
        "restored_wm_ok": [],
        "restored_wm_failed": [],
        "verification_results": None,
        "already_at_baseline_user0": sum(1 for r in inventory if r.get("installed_user0")) - len(snapshot_missing) - len(snapshot_mismatch),
    }

    for row in missing_pkgs:
        pkg = row["package_name"]
        mechanism = classify_restoration_mechanism(row)
        ok, msg = restore_package(pkg, row, verified_apks, serial, adb_path, dry_run=args.dry_run)
        rec = {"package_name": pkg, "restoration_mechanism": mechanism, "message": msg}
        if ok:
            report["restored_packages_ok"].append(rec)
            restored_pkg_rows_for_verify.append(row)
            orig_code = int(row.get("enabled_code_user0", 0))
            if orig_code in (2, 3):
                s_ok, s_msg = restore_enabled_state(pkg, orig_code, serial, adb_path, dry_run=args.dry_run)
                s_rec = {"package_name": pkg, "target_enabled_code": orig_code, "message": s_msg}
                (report["restored_states_ok"] if s_ok else report["restored_states_failed"]).append(s_rec)
        else:
            report["restored_packages_failed"].append(rec)

    for row in state_mismatch_pkgs:
        pkg = row["package_name"]
        orig_code = int(row.get("enabled_code_user0", 0))
        ok, msg = restore_enabled_state(pkg, orig_code, serial, adb_path, dry_run=args.dry_run)
        rec = {
            "package_name": pkg,
            "restoration_mechanism": "pm_enable_or_default_state",
            "target_enabled_code": orig_code,
            "message": msg,
        }
        (report["restored_states_ok"] if ok else report["restored_states_failed"]).append(rec)
        if ok:
            restored_pkg_rows_for_verify.append(inv_by_pkg.get(pkg, row))

    for mod in reversed(setting_mods):
        ok, msg = restore_setting(mod["namespace"], mod["key"], mod.get("old_value"), serial, adb_path, args.dry_run)
        rec = {"namespace": mod["namespace"], "key": mod["key"], "restored_to": mod.get("old_value"), "message": msg}
        (report["restored_settings_ok"] if ok else report["restored_settings_failed"]).append(rec)

    for mod in reversed(appop_mods):
        ok, msg = restore_appop(mod["package_name"], mod["op"], mod["old_mode"], serial, adb_path, args.dry_run)
        rec = {"package_name": mod["package_name"], "op": mod["op"], "restored_to": mod["old_mode"], "message": msg}
        (report["restored_appops_ok"] if ok else report["restored_appops_failed"]).append(rec)

    for mod in reversed(wm_mods):
        ok, msg = restore_wm_density(mod.get("old_value"), serial, adb_path, args.dry_run)
        rec = {"restored_to": mod.get("old_value"), "message": msg}
        (report["restored_wm_ok"] if ok else report["restored_wm_failed"]).append(rec)

    if not args.dry_run and not args.skip_verify:
        report["verification_results"] = verify_live_post_restore(
            restored_pkg_rows=restored_pkg_rows_for_verify,
            restored_settings=report["restored_settings_ok"],
            restored_appops=report["restored_appops_ok"],
            restored_wm=report["restored_wm_ok"],
            serial=serial,
            adb_path=adb_path,
        )

    total_ok = sum(
        len(report[k])
        for k in ("restored_packages_ok", "restored_states_ok", "restored_settings_ok", "restored_appops_ok", "restored_wm_ok")
    )
    total_failed = sum(
        len(report[k])
        for k in (
            "restored_packages_failed",
            "restored_states_failed",
            "restored_settings_failed",
            "restored_appops_failed",
            "restored_wm_failed",
        )
    )
    verify_failed = len((report["verification_results"] or {}).get("failed", []))
    integrity_failed = len(corrupt_apks)

    has_any_failure = (total_failed + verify_failed + integrity_failed) > 0
    if args.dry_run:
        report["overall_status"] = "dry_run_issues_found" if has_any_failure else "dry_run_ready"
    elif not has_any_failure:
        report["overall_status"] = "success"
    elif total_ok > 0:
        report["overall_status"] = "partial_failure"
    else:
        report["overall_status"] = "failed"

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "_dryrun" if args.dry_run else ""
    json_report = backup_dir / f"restoration_report_{ts}{suffix}.json"
    md_report = backup_dir / f"restoration_report_{ts}{suffix}.md"
    json_report.write_text(json.dumps(report, indent=2), encoding="utf-8")

    mode_label = "DRY RUN (SIMULATED ONLY)" if args.dry_run else "LIVE RESTORE"
    action_noun = "Planned" if args.dry_run else "Restored"
    md_lines = [
        f"# Restoration Report ({mode_label})",
        "",
        f"- **Overall Status:** `{report['overall_status']}`",
        f"- **Timestamp:** {report['timestamp']}",
        f"- **Scope:** `{effective_scope}`",
        f"- **Backup Used:** `{backup_dir}`",
        f"- **Device Serial:** `{serial}`",
        f"- **Verified APK Backups:** {len(verified_apks)}/{total_backed_apks} (corrupt/missing: {integrity_failed})",
        f"- **Ignored Unsuccessful Changelog Records:** {len(ignored_unsuccessful)}",
        f"- **{action_noun} Packages:** {len(report['restored_packages_ok'])} ok / {len(report['restored_packages_failed'])} failed",
        f"- **{action_noun} States:** {len(report['restored_states_ok'])} ok / {len(report['restored_states_failed'])} failed",
        f"- **{action_noun} Settings:** {len(report['restored_settings_ok'])} ok / {len(report['restored_settings_failed'])} failed",
        f"- **{action_noun} AppOps:** {len(report['restored_appops_ok'])} ok / {len(report['restored_appops_failed'])} failed",
        f"- **{action_noun} Display Density:** {len(report['restored_wm_ok'])} ok / {len(report['restored_wm_failed'])} failed",
    ]
    if report["verification_results"] is not None:
        md_lines.append(
            f"- **Post-Restore Live Verification:** {len(report['verification_results']['passed'])} passed / "
            f"{len(report['verification_results']['failed'])} failed"
        )
    elif args.dry_run:
        md_lines.append("- **Post-Restore Live Verification:** Skipped (`--dry-run` does not modify device or test MIUI write permissions)")

    md_lines.extend(
        [
            "",
            "> **Scope Note:** Non-root ADB restores system packages (`cmd package install-existing`), disabled states (`pm enable`/`pm default-state`), locally backed-up APKs, and logged `settings`/`appops`/`wm density`. Private `/data/data` internal app data is NOT backed up without root.",
            "",
        ]
    )
    if corrupt_apks:
        md_lines.append("## Backup Integrity Failures")
        for c in corrupt_apks:
            md_lines.append(f"- `{json.dumps(c)}`")
        md_lines.append("")

    for section, title in [
        ("ignored_unsuccessful_operations", "Ignored Unsuccessful Operations (Not Applied)"),
        ("restored_packages_ok", f"{action_noun} Packages (OK)"),
        ("restored_packages_failed", f"{action_noun} Packages (FAILED)"),
        ("restored_states_ok", f"{action_noun} Package States (OK)"),
        ("restored_states_failed", f"{action_noun} Package States (FAILED)"),
        ("restored_settings_ok", f"{action_noun} Settings (OK)"),
        ("restored_settings_failed", f"{action_noun} Settings (FAILED)"),
        ("restored_appops_ok", f"{action_noun} AppOps (OK)"),
        ("restored_appops_failed", f"{action_noun} AppOps (FAILED)"),
        ("restored_wm_ok", f"{action_noun} Display Density (OK)"),
        ("restored_wm_failed", f"{action_noun} Display Density (FAILED)"),
    ]:
        items = report[section]
        if items:
            md_lines.append(f"## {title}")
            for item in items:
                md_lines.append(f"- `{json.dumps(item)}`")
            md_lines.append("")

    if report["verification_results"] and report["verification_results"]["failed"]:
        md_lines.append("## Post-Restore Verification Failures")
        for item in report["verification_results"]["failed"]:
            md_lines.append(f"- `{json.dumps(item)}`")
        md_lines.append("")

    md_report.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Restoration status: {report['overall_status']} | Report written to: {md_report}")
    if corrupt_apks:
        print(
            f"[INTEGRITY FAILURE] {len(corrupt_apks)} backup APK(s) failed verification. Review {md_report}.",
            file=sys.stderr,
        )
    if has_any_failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
