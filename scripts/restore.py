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
import zipfile
from pathlib import Path
from typing import Any

from adb_helper import find_adb, resolve_device_serial, run_adb
from safety import resolve_backup_dir, sha256_file


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
    verified: dict[str, list[Path]] = {}
    for pkg, meta in apk_manifest.get("backed_up_packages", {}).items():
        paths: list[Path] = []
        valid = True
        for fmeta in meta.get("files", []):
            fpath = backup_dir / fmeta["local_rel_path"]
            if not fpath.is_file() or sha256_file(fpath) != fmeta["sha256"]:
                valid = False
                break
            try:
                with zipfile.ZipFile(fpath, "r") as zf:
                    if zf.testzip() is not None:
                        valid = False
                        break
            except Exception:  # noqa: BLE001
                valid = False
                break
            paths.append(fpath)
        if valid and paths:
            verified[pkg] = paths
    return verified


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
    if res.returncode == 0:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean MIUI AI - One-Click Restore")
    parser.add_argument("--backup-dir", help="Specific backup folder path")
    parser.add_argument("--serial", help="Explicit ADB serial")
    parser.add_argument("--dry-run", action="store_true", help="Verify backup & plan restore without modifying device")
    parser.add_argument("--yes", action="store_true", help="Run non-interactively")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent.parent
    backup_dir = resolve_backup_dir(project_dir, args.backup_dir)

    inventory: list[dict[str, Any]] = json.loads((backup_dir / "packages_inventory.json").read_text(encoding="utf-8"))
    apk_manifest: dict[str, Any] = json.loads((backup_dir / "apk_manifest.json").read_text(encoding="utf-8"))
    changelog: dict[str, Any] = json.loads((backup_dir / "changelog.json").read_text(encoding="utf-8"))

    print(f"Using backup: {backup_dir}")
    verified_apks = verify_backup_apks(backup_dir, apk_manifest)
    total_backed_apks = len(apk_manifest.get("backed_up_packages", {}))
    print(f"Verified local APK backups: {len(verified_apks)}/{total_backed_apks}")

    adb_path = find_adb()
    serial = resolve_device_serial(preferred_serial=args.serial)
    print(f"Target device: {serial}")

    live_installed, live_disabled = get_live_user0_packages(serial, adb_path)
    missing_pkgs, state_mismatch_pkgs = find_drifted_packages(inventory, live_installed, live_disabled, serial, adb_path)

    modifications = changelog.get("modifications", [])
    setting_mods = [m for m in modifications if m.get("type") == "setting"]
    appop_mods = [m for m in modifications if m.get("type") == "appop"]
    wm_mods = [m for m in modifications if m.get("type") == "wm_density"]

    print(f"Packages needing restore: {len(missing_pkgs)}")
    print(f"Package enable states to restore: {len(state_mismatch_pkgs)}")
    print(f"Logged settings / appops / density changes to revert: {len(setting_mods)} / {len(appop_mods)} / {len(wm_mods)}")

    if not args.dry_run and not args.yes:
        if not (missing_pkgs or state_mismatch_pkgs or setting_mods or appop_mods or wm_mods):
            print("Device already matches baseline snapshot.")
        else:
            ans = input("Proceed with restoring device state from backup? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                print("Aborted by user.")
                return

    report: dict[str, Any] = {
        "timestamp": dt.datetime.now().isoformat(),
        "dry_run": args.dry_run,
        "backup_dir": str(backup_dir),
        "serial": serial,
        "apk_backups_verified": len(verified_apks),
        "apk_backups_total": total_backed_apks,
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
        "already_at_baseline": len(inventory) - len(missing_pkgs) - len(state_mismatch_pkgs),
    }

    for row in missing_pkgs:
        pkg = row["package_name"]
        ok, msg = restore_package(pkg, row, verified_apks, serial, adb_path, dry_run=args.dry_run)
        rec = {"package_name": pkg, "message": msg}
        if ok:
            report["restored_packages_ok"].append(rec)
            orig_code = int(row.get("enabled_code_user0", 0))
            if orig_code in (2, 3):
                s_ok, s_msg = restore_enabled_state(pkg, orig_code, serial, adb_path, dry_run=args.dry_run)
                if not s_ok:
                    report["restored_states_failed"].append({"package_name": pkg, "message": s_msg})
        else:
            report["restored_packages_failed"].append(rec)

    for row in state_mismatch_pkgs:
        pkg = row["package_name"]
        orig_code = int(row.get("enabled_code_user0", 0))
        ok, msg = restore_enabled_state(pkg, orig_code, serial, adb_path, dry_run=args.dry_run)
        rec = {"package_name": pkg, "target_enabled_code": orig_code, "message": msg}
        (report["restored_states_ok"] if ok else report["restored_states_failed"]).append(rec)

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

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "_dryrun" if args.dry_run else ""
    json_report = backup_dir / f"restoration_report_{ts}{suffix}.json"
    md_report = backup_dir / f"restoration_report_{ts}{suffix}.md"
    json_report.write_text(json.dumps(report, indent=2), encoding="utf-8")

    md_lines = [
        f"# Restoration Report ({'DRY RUN' if args.dry_run else 'LIVE'})",
        "",
        f"- **Timestamp:** {report['timestamp']}",
        f"- **Backup Used:** `{backup_dir}`",
        f"- **Device Serial:** `{serial}`",
        f"- **Verified APK Backups:** {len(verified_apks)}/{total_backed_apks}",
        f"- **Packages Already at Baseline:** {report['already_at_baseline']}",
        f"- **Restored Packages:** {len(report['restored_packages_ok'])} ok / {len(report['restored_packages_failed'])} failed",
        f"- **Restored States:** {len(report['restored_states_ok'])} ok / {len(report['restored_states_failed'])} failed",
        f"- **Restored Settings:** {len(report['restored_settings_ok'])} ok / {len(report['restored_settings_failed'])} failed",
        f"- **Restored AppOps:** {len(report['restored_appops_ok'])} ok / {len(report['restored_appops_failed'])} failed",
        f"- **Restored Display Density:** {len(report['restored_wm_ok'])} ok / {len(report['restored_wm_failed'])} failed",
        "",
        "> **Scope Note:** Non-root ADB restores system packages (`cmd package install-existing`), disabled states (`pm enable`/`pm default-state`), locally backed-up APKs, and logged `settings`/`appops`/`wm density`. Private `/data/data` app files cannot be backed up or restored without root if an app was uninstalled without `-k`.",
        "",
    ]
    for section, title in [
        ("restored_packages_ok", "Restored Packages (OK)"),
        ("restored_packages_failed", "Restored Packages (FAILED)"),
        ("restored_states_ok", "Restored Package States (OK)"),
        ("restored_states_failed", "Restored Package States (FAILED)"),
        ("restored_settings_ok", "Restored Settings (OK)"),
        ("restored_settings_failed", "Restored Settings (FAILED)"),
        ("restored_appops_ok", "Restored AppOps (OK)"),
        ("restored_appops_failed", "Restored AppOps (FAILED)"),
        ("restored_wm_ok", "Restored Display Density (OK)"),
        ("restored_wm_failed", "Restored Display Density (FAILED)"),
    ]:
        items = report[section]
        if items:
            md_lines.append(f"## {title}")
            for item in items:
                md_lines.append(f"- `{json.dumps(item)}`")
            md_lines.append("")

    md_report.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Restoration report written to: {md_report}")


if __name__ == "__main__":
    main()
