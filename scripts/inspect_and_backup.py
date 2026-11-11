"""Phase 1-3 Read-Only Inspection, Inventory, Settings Audit, and Verified APK Backup.

Performs zero modifications on the connected Android device.
Exports a timestamped backup snapshot under backups/<timestamp>_<model>/ suitable
for verification and one-click restore on Windows, macOS, or Linux.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from adb_helper import find_aapt2, find_adb, resolve_device_serial, run_adb, run_cmd
from safety import (
    detect_os_family,
    is_protected_package,
    sanitize_filename_part,
    sha256_file,
    verify_apk_zip,
)

ENABLED_STATE_LABELS = {
    0: "DEFAULT",
    1: "ENABLED",
    2: "DISABLED",
    3: "DISABLED_USER",
    4: "DISABLED_UNTIL_USED",
}

KEY_AUDIT_PACKAGES = [
    "com.miui.msa.global",
    "com.miui.analytics",
    "com.miui.daemon",
    "com.xiaomi.joyose",
    "com.xiaomi.glgm",
    "com.xiaomi.mipicks",
    "com.xiaomi.discover",
    "com.mi.globalminusscreen",
    "com.mi.globalbrowser",
    "com.android.browser",
    "com.miui.videoplayer",
    "com.miui.player",
    "com.miui.global.packageinstaller",
    "com.miui.packageinstaller",
    "com.android.thememanager",
    "com.mi.android.globalFileexplorer",
    "com.miui.securitycenter",
    "com.miui.cleanmaster",
    "com.miui.cleaner",
    "com.miui.cloudservice",
    "com.miui.cloudbackup",
    "com.miui.micloudsync",
    "com.xiaomi.xmsf",
    "com.xiaomi.xmsfkeeper",
    "com.miui.android.fashiongallery",
    "com.miui.yellowpage",
    "com.miui.hybrid",
    "com.miui.hybrid.accessory",
    "com.miui.bugreport",
    "com.miui.miservice",
    "com.xiaomi.payment",
    "com.mipay.wallet.in",
    "com.miui.weather2",
    "com.miui.notes",
    "com.miui.compass",
    "com.miui.calculator",
    "com.xiaomi.calendar",
    "com.xiaomi.scanner",
    "com.miui.screenrecorder",
    "com.xiaomi.midrop",
    "com.xiaomi.smarthome",
    "com.facebook.system",
    "com.facebook.appmanager",
    "com.facebook.services",
]


def adb_shell(serial: str, cmd: str, adb_path: str, timeout: int = 120) -> str:
    """Run an adb shell command and return stdout, raising on fatal connection/execution error."""
    res = run_adb(["shell", cmd], serial=serial, timeout=timeout, adb_path=adb_path)
    if res.returncode != 0 and not res.stdout.strip():
        raise RuntimeError(f"adb shell '{cmd}' failed ({res.returncode}): {res.stderr.strip()}")
    return res.stdout


def parse_pm_list_f(output: str) -> dict[str, str]:
    """Parse `pm list packages -f` output into {package_name: apk_path}."""
    result: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("package:") or "=" not in line:
            continue
        apk_path, pkg = line[len("package:") :].rsplit("=", 1)
        result[pkg.strip()] = apk_path.strip()
    return result


def parse_pm_list_simple(output: str) -> set[str]:
    """Parse `pm list packages` output into a set of package names."""
    result: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            result.add(line[len("package:") :].strip())
    return result


def parse_dumpsys_packages(raw_text: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Parse `dumpsys package packages` into (active_packages, hidden_system_packages)."""
    active: dict[str, dict[str, Any]] = {}
    hidden: dict[str, dict[str, Any]] = {}
    section = "active"
    current_pkg: str | None = None
    current: dict[str, Any] | None = None

    pkg_re = re.compile(r"^\s*Package \[([^\]]+)\]")
    ver_re = re.compile(r"versionCode=(\d+)\s+minSdk=(\d+)\s+targetSdk=(\d+)")
    user_re = re.compile(r"^\s*User (\d+):\s*(.+)$")

    def flush() -> None:
        nonlocal current_pkg, current
        if current_pkg and current is not None:
            target = active if section == "active" else hidden
            target[current_pkg] = current
        current_pkg = None
        current = None

    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped == "Packages:":
            flush()
            section = "active"
            continue
        if stripped == "Hidden system packages:":
            flush()
            section = "hidden"
            continue

        m_pkg = pkg_re.match(line)
        if m_pkg:
            flush()
            current_pkg = m_pkg.group(1)
            current = {
                "package_name": current_pkg,
                "code_path": None,
                "version_code": None,
                "version_name": None,
                "min_sdk": None,
                "target_sdk": None,
                "installer_package": None,
                "pkg_flags": [],
                "users": {},
            }
            continue

        if current is None:
            continue

        if stripped.startswith("codePath="):
            current["code_path"] = stripped.split("=", 1)[1].strip()
        elif stripped.startswith("versionCode="):
            m_ver = ver_re.search(stripped)
            if m_ver:
                current["version_code"] = int(m_ver.group(1))
                current["min_sdk"] = int(m_ver.group(2))
                current["target_sdk"] = int(m_ver.group(3))
        elif stripped.startswith("versionName="):
            current["version_name"] = stripped.split("=", 1)[1].strip()
        elif stripped.startswith("installerPackageName="):
            current["installer_package"] = stripped.split("=", 1)[1].strip()
        elif stripped.startswith("pkgFlags=["):
            inner = stripped[len("pkgFlags=[") :].rstrip("]").strip()
            current["pkg_flags"] = [tok for tok in inner.split() if tok]
        else:
            m_user = user_re.match(line)
            if m_user:
                uid = int(m_user.group(1))
                user_info: dict[str, Any] = {}
                for token in m_user.group(2).strip().split():
                    if "=" not in token:
                        continue
                    k, v = token.split("=", 1)
                    if v in ("true", "false"):
                        user_info[k] = v == "true"
                    elif v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
                        user_info[k] = int(v)
                    else:
                        user_info[k] = v
                if user_info:
                    current["users"][str(uid)] = user_info

    flush()
    return active, hidden


def classify_package(pkg: str, is_system: bool, code_path: str | None) -> str:
    """Classify package origin across Xiaomi, Samsung, BBK/OnePlus, Google, AOSP, and third-party."""
    low = pkg.lower()
    if low.startswith("com.google.") or low in ("com.android.vending", "com.android.chrome"):
        return "google"
    if low.startswith(("com.miui.", "com.xiaomi.", "com.mi.", "com.redmi.", "com.mipay.", "com.duokan.", "com.mfashiongallery.")) or "xiaomi" in low or "miui" in low:
        return "manufacturer_xiaomi"
    if low.startswith(("com.samsung.", "com.sec.")):
        return "manufacturer_samsung" if is_system else "third_party_user"
    if low.startswith(("com.oplus.", "com.coloros.", "com.oneplus.", "com.heytap.", "com.realme.", "com.oppo.")):
        return "manufacturer_bbk" if is_system else "third_party_user"
    if low.startswith(("com.mediatek.", "com.mtk.")) or "mediatek" in low:
        return "manufacturer_mediatek"
    if not is_system:
        return "third_party_user"
    if (
        low == "android"
        or low.startswith(("android.", "com.android.", "org.codeaurora.", "com.qualcomm."))
        or (code_path and any(p in code_path for p in ("/apex/", "/system/framework", "/vendor/overlay")))
    ):
        return "system_aosp"
    return "third_party_preinstalled"


def parse_settings_list(raw_text: str) -> dict[str, str]:
    """Parse `settings list <namespace>` lines cleanly."""
    parsed: dict[str, str] = {}
    for line in raw_text.splitlines():
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        parsed[key] = val
    return parsed


def should_pull_apk(entry: dict[str, Any]) -> bool:
    """Decide whether to back up a package's APK locally for restore assurance."""
    pkg = entry["package_name"]
    protected, _ = is_protected_package(pkg)
    if protected:
        return False
    cat = entry["category"]
    if cat in ("third_party_user", "third_party_preinstalled"):
        return True
    if pkg in KEY_AUDIT_PACKAGES:
        return True
    return cat.startswith("manufacturer_")


def create_unique_backup_dir(project_dir: Path, model: str | None) -> Path:
    """Create a unique timestamped backup folder without ever overwriting an existing snapshot."""
    model_tag = sanitize_filename_part(model or "ANDROID")
    base_name = f"{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{model_tag}"
    backups_root = project_dir / "backups"
    backups_root.mkdir(parents=True, exist_ok=True)
    candidate = backups_root / base_name
    counter = 2
    while candidate.exists():
        candidate = backups_root / f"{base_name}_{counter}"
        counter += 1
    candidate.mkdir(parents=True, exist_ok=False)
    (candidate / "apks").mkdir(parents=True, exist_ok=True)
    return candidate


def classify_apk_exclusion(entry: dict[str, Any], skip_apk_pull: bool) -> dict[str, Any]:
    """Explain why an inventory package was not pulled as a local APK and how it recovers."""
    pkg = entry["package_name"]
    is_system = bool(entry.get("is_system"))
    installed_u0 = bool(entry.get("installed_user0"))
    protected, _ = is_protected_package(pkg)

    if not installed_u0:
        reason = "Not installed for user 0 at baseline"
        recoverability = "already_uninstalled_user0"
    elif skip_apk_pull:
        reason = "Skipped globally via --skip-apk-pull (metadata-only snapshot)"
        recoverability = (
            "system_partition_install_existing" if is_system else "not_backed_up_unrecoverable_via_local_apk"
        )
    elif protected:
        reason = "Protected core/system package (excluded by safety policy)"
        recoverability = (
            "system_partition_install_existing" if is_system else "not_backed_up_unrecoverable_via_local_apk"
        )
    else:
        reason = "Excluded by should_pull_apk policy (Google/AOSP or unaudited system package)"
        recoverability = (
            "system_partition_install_existing" if is_system else "not_backed_up_unrecoverable_via_local_apk"
        )

    return {
        "package_name": pkg,
        "category": entry.get("category"),
        "is_system": is_system,
        "installed_user0": installed_u0,
        "reason": reason,
        "recoverability": recoverability,
    }


def extract_apk_label(apk_path: Path, aapt2_path: str | None) -> str | None:
    """Extract human-readable application label via aapt2 when available."""
    if not aapt2_path or not apk_path.is_file():
        return None
    res = run_cmd([aapt2_path, "dump", "badging", str(apk_path)], timeout=30)
    if res.returncode != 0:
        return None
    for prefix in ("application-label:", "application-label-en"):
        for line in res.stdout.splitlines():
            if line.startswith(prefix):
                return line.split(":", 1)[1].strip().strip("'")
    return None


def collect_device_info(serial: str, adb_path: str) -> dict[str, Any]:
    """Run Phase 1 device, OS, UI scaling, and permission-state inspection."""
    props_to_read = {
        "model": "ro.product.model",
        "brand": "ro.product.brand",
        "manufacturer": "ro.product.manufacturer",
        "device": "ro.product.device",
        "product_name": "ro.product.name",
        "android_version": "ro.build.version.release",
        "sdk_int": "ro.build.version.sdk",
        "build_id": "ro.build.id",
        "security_patch": "ro.build.version.security_patch",
        "miui_ui_version": "ro.miui.ui.version.name",
        "hyperos_version": "ro.mi.os.version.name",
        "rom_incremental": "ro.build.version.incremental",
        "ro_sf_lcd_density": "ro.sf.lcd_density",
        "hardware": "ro.hardware",
        "board_platform": "ro.board.platform",
        "persist_security_adbinput": "persist.security.adbinput",
        "persist_security_adbinstall": "persist.security.adbinstall",
    }
    shell_parts = [f"echo '{k}='$(getprop {prop})" for k, prop in props_to_read.items()]
    prop_out = adb_shell(serial, "; ".join(shell_parts), adb_path)
    info: dict[str, Any] = {"serial": serial, "captured_at": dt.datetime.now().isoformat()}
    for line in prop_out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()

    info["wm_size"] = run_adb(["shell", "wm", "size"], serial=serial, adb_path=adb_path).stdout.strip()
    info["wm_density"] = run_adb(["shell", "wm", "density"], serial=serial, adb_path=adb_path).stdout.strip()
    info["default_home_activity"] = run_adb(
        ["shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"],
        serial=serial,
        adb_path=adb_path,
    ).stdout.strip()
    info["storage_df_data"] = adb_shell(serial, "df -h /data", adb_path).strip()
    info["users_raw"] = adb_shell(serial, "pm list users", adb_path).strip()
    return info


def collect_ad_tracking_audit(
    serial: str, adb_path: str, settings_snapshot: dict[str, dict[str, str]], installed_pkgs: set[str]
) -> dict[str, Any]:
    """Capture baseline ad/telemetry settings and AppOps modes without modifying anything."""
    interesting_settings: dict[str, dict[str, str | None]] = {"global": {}, "secure": {}, "system": {}}
    keywords = (
        "ad",
        "msa",
        "analytics",
        "recommend",
        "personalized",
        "private_dns",
        "usage",
        "diag",
        "telemetry",
        "upload_log",
        "user_experience",
        "control_panel",
        "font_scale",
        "density",
        "notification_style",
        "default_input_method",
    )
    for ns, kv in settings_snapshot.items():
        for k, v in sorted(kv.items()):
            low_k = k.lower()
            if any(tok in low_k for tok in keywords):
                interesting_settings[ns][k] = v

    pkg_states: dict[str, Any] = {}
    for pkg in KEY_AUDIT_PACKAGES:
        if pkg not in installed_pkgs:
            pkg_states[pkg] = {"present": False}
            continue
        appops_res = run_adb(["shell", "cmd", "appops", "get", pkg], serial=serial, adb_path=adb_path)
        pkg_states[pkg] = {"present": True, "appops": appops_res.stdout.strip()}

    return {
        "captured_at": dt.datetime.now().isoformat(),
        "audit_scope_note": (
            f"AppOps captured only for {len(KEY_AUDIT_PACKAGES)} key packages (mostly Xiaomi/Meta audit targets) "
            "that are present on the device; remaining installed packages were not AppOps-dumped."
        ),
        "interesting_settings": interesting_settings,
        "key_package_audit": pkg_states,
    }


def write_inventory_files(
    backup_dir: Path, inventory: list[dict[str, Any]], by_category: dict[str, list[str]]
) -> None:
    """Write packages_inventory.json, packages_by_category.json, and packages_inventory.csv."""
    (backup_dir / "packages_inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    (backup_dir / "packages_by_category.json").write_text(json.dumps(by_category, indent=2), encoding="utf-8")
    csv_fields = [
        "package_name",
        "app_label",
        "category",
        "is_system",
        "is_updated_system_app",
        "installed_user0",
        "enabled_code_user0",
        "enabled_label_user0",
        "version_code",
        "version_name",
        "installer_package",
        "code_path",
        "factory_system_code_path",
    ]
    with (backup_dir / "packages_inventory.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fields)
        writer.writeheader()
        for row in inventory:
            writer.writerow({k: row.get(k) for k in csv_fields})


def pull_apks_with_verification(
    inventory: list[dict[str, Any]],
    backup_dir: Path,
    serial: str,
    adb_path: str,
    aapt2_path: str | None,
    skip_apk_pull: bool,
    max_apk_mb: float,
) -> dict[str, Any]:
    """Pull candidate APKs, reject corrupt ZIPs, and record explicit exclusions for unbacked apps."""
    apks_dir = backup_dir / "apks"
    apks_dir.mkdir(parents=True, exist_ok=True)
    apk_manifest: dict[str, Any] = {
        "skip_apk_pull_flag": bool(skip_apk_pull),
        "max_apk_mb": max_apk_mb,
        "backed_up_packages": {},
        "skipped_or_failed": {},
        "excluded_from_apk_pull": {},
    }

    candidates = [] if skip_apk_pull else [e for e in inventory if should_pull_apk(e)]
    if candidates:
        print(f"[Phase 3] Backing up APKs for {len(candidates)} candidate packages...", flush=True)
    max_bytes = int(max_apk_mb * 1024 * 1024)

    for idx, entry in enumerate(candidates, start=1):
        pkg = entry["package_name"]
        pm_path_raw = run_adb(["shell", "pm", "path", pkg], serial=serial, adb_path=adb_path).stdout
        remote_paths = [
            line[len("package:") :].strip()
            for line in pm_path_raw.splitlines()
            if line.strip().startswith("package:")
        ]
        entry["pm_paths"] = remote_paths
        if not remote_paths:
            apk_manifest["skipped_or_failed"][pkg] = "No pm path returned (not installed for user 0 or inaccessible)"
            continue

        pkg_dir = apks_dir / pkg
        pkg_dir.mkdir(parents=True, exist_ok=True)
        pulled_files: list[dict[str, Any]] = []
        pkg_failed = False

        for rpath in remote_paths:
            size_raw = run_adb(
                ["shell", f"stat -c '%s' '{rpath}' 2>/dev/null || echo -1"], serial=serial, adb_path=adb_path
            ).stdout.strip()
            try:
                remote_size = int(size_raw.splitlines()[0].strip())
            except ValueError:
                remote_size = -1
            if max_bytes > 0 and remote_size > max_bytes:
                apk_manifest["skipped_or_failed"][pkg] = (
                    f"Skipped {rpath} ({remote_size / (1024 * 1024):.1f} MB > {max_apk_mb} MB limit)"
                )
                pkg_failed = True
                break

            local_file = pkg_dir / Path(rpath).name
            try:
                pull_res = run_adb(["pull", rpath, str(local_file)], serial=serial, timeout=90, adb_path=adb_path)
            except subprocess.TimeoutExpired:
                apk_manifest["skipped_or_failed"][pkg] = f"adb pull timed out (>90s) for {rpath}"
                pkg_failed = True
                break

            if pull_res.returncode != 0 or not local_file.is_file():
                apk_manifest["skipped_or_failed"][pkg] = f"adb pull failed for {rpath}: {pull_res.stderr.strip()}"
                pkg_failed = True
                break

            ok_zip, zip_err = verify_apk_zip(local_file)
            if not ok_zip:
                apk_manifest["skipped_or_failed"][pkg] = f"Corrupt/invalid APK ZIP ({rpath}): {zip_err}"
                pkg_failed = True
                break

            pulled_files.append(
                {
                    "remote_path": rpath,
                    "local_rel_path": local_file.relative_to(backup_dir).as_posix(),
                    "size_bytes": local_file.stat().st_size,
                    "sha256": sha256_file(local_file),
                    "zip_valid": True,
                    "zip_error": None,
                }
            )
            if local_file.name == "base.apk" or len(remote_paths) == 1:
                label = extract_apk_label(local_file, aapt2_path)
                if label:
                    entry["app_label"] = label

        if pkg_failed or not pulled_files:
            shutil.rmtree(pkg_dir, ignore_errors=True)
            if pkg not in apk_manifest["skipped_or_failed"]:
                apk_manifest["skipped_or_failed"][pkg] = "No valid APK files pulled"
        else:
            apk_manifest["backed_up_packages"][pkg] = {
                "app_label": entry["app_label"],
                "category": entry["category"],
                "is_system": entry["is_system"],
                "version_code": entry["version_code"],
                "version_name": entry["version_name"],
                "files": pulled_files,
                "recoverability": "local_apk_verified"
                if not entry["is_system"]
                else "local_apk_and_system_partition",
            }
        if idx % 10 == 0 or idx == len(candidates):
            print(f"  ...processed {idx}/{len(candidates)} packages", flush=True)

    for entry in inventory:
        pkg = entry["package_name"]
        if pkg in apk_manifest["backed_up_packages"] or pkg in apk_manifest["skipped_or_failed"]:
            continue
        apk_manifest["excluded_from_apk_pull"][pkg] = classify_apk_exclusion(entry, skip_apk_pull)

    apk_manifest["completeness_summary"] = {
        "total_packages_in_inventory": len(inventory),
        "candidates_attempted": len(candidates),
        "apk_backups_verified": len(apk_manifest["backed_up_packages"]),
        "apk_backups_skipped_or_failed": len(apk_manifest["skipped_or_failed"]),
        "apk_excluded_by_policy": len(apk_manifest["excluded_from_apk_pull"]),
        "private_app_data_backed_up": False,
    }
    return apk_manifest


def write_summary_json(
    backup_dir: Path,
    device_info: dict[str, Any],
    settings_snapshot: dict[str, dict[str, str]],
    ad_audit: dict[str, Any],
    inventory: list[dict[str, Any]],
    by_category: dict[str, list[str]],
    apk_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Write machine-readable backup summary.json (including completeness and limitations)."""
    default_ime = settings_snapshot.get("secure", {}).get("default_input_method", "")
    active_ime_pkg = default_ime.split("/", 1)[0].strip() if "/" in default_ime else ""
    summary = {
        "created_at": dt.datetime.now().isoformat(),
        "backup_dir": str(backup_dir),
        "device": {
            "serial": device_info.get("serial"),
            "brand": device_info.get("brand"),
            "manufacturer": device_info.get("manufacturer"),
            "model": device_info.get("model"),
            "device": device_info.get("device"),
            "android_version": device_info.get("android_version"),
            "sdk_int": device_info.get("sdk_int"),
            "miui_ui_version": device_info.get("miui_ui_version"),
            "hyperos_version": device_info.get("hyperos_version"),
            "rom_incremental": device_info.get("rom_incremental"),
            "os_family": detect_os_family(device_info),
        },
        "ui_metrics": {
            "wm_size": device_info.get("wm_size"),
            "wm_density": device_info.get("wm_density"),
            "ro_sf_lcd_density": device_info.get("ro_sf_lcd_density"),
            "font_scale": settings_snapshot.get("system", {}).get("font_scale"),
            "active_home_activity": device_info.get("default_home_activity"),
            "active_ime_package": active_ime_pkg,
        },
        "package_counts_by_category": {
            cat: {
                "total": len(rows),
                "installed_user0": sum(1 for r in inventory if r["category"] == cat and r.get("installed_user0")),
                "disabled_user0": sum(
                    1 for r in inventory if r["category"] == cat and r.get("enabled_code_user0") in (2, 3)
                ),
            }
            for cat, rows in sorted(by_category.items())
        },
        "backup_completeness": apk_manifest.get("completeness_summary", {}),
        "appops_audited_packages": sum(
            1 for v in ad_audit.get("key_package_audit", {}).values() if v.get("present")
        ),
        "backup_scope_limitations": [
            "Private /data/data internal app databases, logins, user files, and home screen layout are NOT backed up without root.",
            "System packages recover via 'cmd package install-existing --user 0' (if retained on system partition).",
            "Non-system packages uninstalled for user 0 can only be reinstalled locally if listed under apk_manifest.backed_up_packages (MIUI also requires Developer options -> Install via USB).",
            "Protected core/system packages and Google/unaudited AOSP packages are excluded from local APK pulling; see apk_manifest.excluded_from_apk_pull.",
        ],
    }
    (backup_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean MIUI AI - Phase 1-3 Read-Only Inspection & Backup")
    parser.add_argument("--serial", help="Explicit ADB serial (optional)")
    parser.add_argument(
        "--project-dir",
        default=str(Path(__file__).resolve().parent.parent),
        help="Repository root directory",
    )
    parser.add_argument("--skip-apk-pull", action="store_true", help="Skip pulling APK files (metadata only)")
    parser.add_argument(
        "--max-apk-mb",
        type=float,
        default=120.0,
        help="Skip individual APKs larger than this size in MB",
    )
    args = parser.parse_args()
    if args.project_dir is not None and not str(args.project_dir).strip():
        parser.error("--project-dir cannot be empty")

    adb_path = find_adb()
    aapt2_path = find_aapt2()
    serial = resolve_device_serial(preferred_serial=args.serial)

    project_dir = Path(args.project_dir).resolve()
    print(f"[Phase 1] Connected device: {serial}")
    device_info = collect_device_info(serial, adb_path)

    backup_dir = create_unique_backup_dir(project_dir, device_info.get("model"))
    (backup_dir / "device_info.json").write_text(json.dumps(device_info, indent=2), encoding="utf-8")

    print(
        f"[Phase 1] Device: {device_info.get('brand')} {device_info.get('model')} "
        f"(Android {device_info.get('android_version')}, ROM={device_info.get('rom_incremental')})"
    )

    print("[Phase 2] Exporting complete package inventory...")
    pm_all_f = parse_pm_list_f(adb_shell(serial, "pm list packages -f -u", adb_path))
    pm_sys = parse_pm_list_simple(adb_shell(serial, "pm list packages -s -u", adb_path))
    pm_3rd = parse_pm_list_simple(adb_shell(serial, "pm list packages -3 -u", adb_path))
    pm_dis = parse_pm_list_simple(adb_shell(serial, "pm list packages -d -u", adb_path))
    pm_user0 = parse_pm_list_simple(adb_shell(serial, "pm list packages --user 0", adb_path))

    dumpsys_raw = adb_shell(serial, "dumpsys package packages", adb_path, timeout=180)
    active_pkgs, hidden_sys_pkgs = parse_dumpsys_packages(dumpsys_raw)

    all_pkg_names = sorted(set(pm_all_f.keys()) | set(active_pkgs.keys()))
    inventory: list[dict[str, Any]] = []
    by_category: dict[str, list[str]] = {}

    for pkg in all_pkg_names:
        d = active_pkgs.get(pkg, {})
        h = hidden_sys_pkgs.get(pkg, {})
        flags = d.get("pkg_flags", [])
        is_system = (pkg in pm_sys) or ("SYSTEM" in flags)
        is_updated_sys = "UPDATED_SYSTEM_APP" in flags or (pkg in hidden_sys_pkgs)
        code_path = d.get("code_path") or pm_all_f.get(pkg)
        user0_data = d.get("users", {}).get("0", {})
        enabled_code = int(user0_data.get("enabled", 0))
        installed_user0 = bool(user0_data.get("installed", pkg in pm_user0))
        category = classify_package(pkg, is_system=is_system, code_path=code_path)

        entry: dict[str, Any] = {
            "package_name": pkg,
            "app_label": None,
            "category": category,
            "is_system": is_system,
            "is_updated_system_app": is_updated_sys,
            "is_third_party_pm": pkg in pm_3rd,
            "is_disabled_pm": pkg in pm_dis,
            "installed_user0": installed_user0,
            "enabled_code_user0": enabled_code,
            "enabled_label_user0": ENABLED_STATE_LABELS.get(enabled_code, f"UNKNOWN({enabled_code})"),
            "version_code": d.get("version_code"),
            "version_name": d.get("version_name"),
            "min_sdk": d.get("min_sdk"),
            "target_sdk": d.get("target_sdk"),
            "installer_package": d.get("installer_package"),
            "code_path": code_path,
            "pm_list_apk_path": pm_all_f.get(pkg),
            "factory_system_code_path": h.get("code_path") if h else None,
            "pkg_flags": flags,
            "pm_paths": [],
        }
        inventory.append(entry)
        by_category.setdefault(category, []).append(pkg)

    print("[Phase 2] Exporting system/secure/global settings...")
    settings_snapshot: dict[str, dict[str, str]] = {}
    for ns in ("system", "secure", "global"):
        raw = adb_shell(serial, f"settings list {ns}", adb_path)
        (backup_dir / f"settings_{ns}.txt").write_text(raw, encoding="utf-8")
        settings_snapshot[ns] = parse_settings_list(raw)
    (backup_dir / "settings_snapshot.json").write_text(
        json.dumps(settings_snapshot, indent=2, sort_keys=True), encoding="utf-8"
    )

    ad_audit = collect_ad_tracking_audit(serial, adb_path, settings_snapshot, set(all_pkg_names))
    (backup_dir / "ad_tracking_audit.json").write_text(json.dumps(ad_audit, indent=2), encoding="utf-8")
    write_inventory_files(backup_dir, inventory, by_category)

    apk_manifest = pull_apks_with_verification(
        inventory=inventory,
        backup_dir=backup_dir,
        serial=serial,
        adb_path=adb_path,
        aapt2_path=aapt2_path,
        skip_apk_pull=args.skip_apk_pull,
        max_apk_mb=args.max_apk_mb,
    )

    (backup_dir / "apk_manifest.json").write_text(json.dumps(apk_manifest, indent=2), encoding="utf-8")
    write_inventory_files(backup_dir, inventory, by_category)
    write_summary_json(backup_dir, device_info, settings_snapshot, ad_audit, inventory, by_category, apk_manifest)

    completeness = apk_manifest.get("completeness_summary", {})
    changelog = {
        "backup_dir": str(backup_dir),
        "created_at": dt.datetime.now().isoformat(),
        "device": {
            "serial": serial,
            "brand": device_info.get("brand"),
            "model": device_info.get("model"),
            "android_version": device_info.get("android_version"),
            "rom_version": device_info.get("rom_incremental"),
        },
        "total_packages": len(inventory),
        "apk_backups_verified": completeness.get("apk_backups_verified", 0),
        "apk_backups_skipped": completeness.get("apk_backups_skipped_or_failed", 0),
        "apk_excluded_by_policy": completeness.get("apk_excluded_by_policy", 0),
        "modifications": [],
    }
    (backup_dir / "changelog.json").write_text(json.dumps(changelog, indent=2), encoding="utf-8")
    (backup_dir / "CHANGELOG.md").write_text(
        "\n".join(
            [
                f"# Change Log — {backup_dir.name}",
                "",
                f"- **Created:** {changelog['created_at']}",
                f"- **Device:** {device_info.get('brand')} {device_info.get('model')} (Android {device_info.get('android_version')}, {device_info.get('rom_incremental')})",
                f"- **Total Packages Recorded:** {len(inventory)}",
                f"- **Verified Local APK Backups:** {completeness.get('apk_backups_verified', 0)}",
                f"- **Skipped / Failed APK Pulls:** {completeness.get('apk_backups_skipped_or_failed', 0)}",
                f"- **Excluded from Local APK Pull (Policy / System):** {completeness.get('apk_excluded_by_policy', 0)}",
                "",
                "## Modifications",
                "",
                "_No modifications have been applied yet (baseline inspection and backup only)._",
                "",
            ]
        ),
        encoding="utf-8",
    )

    latest_ptr = project_dir / "backups" / "LATEST_BACKUP.txt"
    latest_ptr.write_text(backup_dir.relative_to(project_dir).as_posix(), encoding="utf-8")

    print("\n=== BACKUP SUMMARY ===")
    print(f"Backup directory: {backup_dir}")
    print(f"Total packages recorded: {len(inventory)}")
    for k, v in sorted(by_category.items()):
        print(f"  - {k}: {len(v)}")
    print(f"APKs backed up & ZIP-verified: {completeness.get('apk_backups_verified', 0)}")
    print(f"APKs skipped/failed during pull: {completeness.get('apk_backups_skipped_or_failed', 0)}")
    print(f"Packages excluded by policy/system-only: {completeness.get('apk_excluded_by_policy', 0)}")


if __name__ == "__main__":
    main()
