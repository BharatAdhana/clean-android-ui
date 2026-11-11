"""Shared safety guardrails, hashing, and backup resolution for Clean MIUI AI."""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Any

# Packages that must NEVER be disabled or uninstalled via non-root ADB because
# removing them causes soft-bricks, bootloops (on MIUI), or loss of calls/UI/Play.
NEVER_REMOVE_EXACT: frozenset[str] = frozenset(
    {
        "android",
        "com.android.systemui",
        "com.android.phone",
        "com.android.server.telecom",
        "com.android.mms",
        "com.android.mms.service",
        "com.android.contacts",
        "com.android.settings",
        "com.android.shell",
        "com.android.keychain",
        "com.android.inputdevices",
        "com.android.externalstorage",
        "com.android.bluetooth",
        "com.android.wifi",
        "com.android.networkstack",
        "com.android.permissioncontroller",
        "com.google.android.permissioncontroller",
        "com.android.packageinstaller",
        "com.google.android.packageinstaller",
        "com.android.vending",
        "com.google.android.gms",
        "com.google.android.gsf",
        "com.google.android.webview",
        "com.android.webview",
        "com.android.camera",
        "com.xiaomi.camera",
        "com.miui.rom",
        "com.miui.core",
        "com.miui.system",
        "com.miui.securitycenter",
        "com.xiaomi.finddevice",
        "com.miui.home",
        "com.mi.android.globallauncher",
        "miui.systemui.plugin",
        "com.miui.powerkeeper",
        "com.xiaomi.biometric",
        "com.goodix.fingerprint",
        "com.fpc.fingerprint",
    }
)

NEVER_REMOVE_PREFIXES: tuple[str, ...] = (
    "com.android.providers.",
    "com.android.internal.",
    "com.qualcomm.qti.telephony",
    "com.mediatek.telephony",
    "com.mediatek.ims",
)


def is_protected_package(pkg: str, dynamic_protected: set[str] | None = None) -> tuple[bool, str | None]:
    """Check whether a package is protected by static OS guardrails or live device state."""
    if pkg in NEVER_REMOVE_EXACT:
        return True, "Core OS / MIUI stability / Play Services / Hardware guardrail"
    for prefix in NEVER_REMOVE_PREFIXES:
        if pkg.startswith(prefix):
            return True, f"Core system provider/telephony prefix ({prefix})"
    if dynamic_protected and pkg in dynamic_protected:
        return True, "Active default Launcher or Input Method (IME) on this device"
    return False, None


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest for a local file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_backup_dir(project_dir: Path, explicit_dir: str | None) -> Path:
    """Locate target backup folder from --backup-dir, backups/LATEST_BACKUP.txt, or newest dir."""
    if explicit_dir:
        path = Path(explicit_dir)
        if not path.is_dir():
            raise FileNotFoundError(f"Backup directory does not exist: {path}")
        return path

    backups_root = project_dir / "backups"
    latest_ptr = backups_root / "LATEST_BACKUP.txt"
    if latest_ptr.is_file():
        raw = latest_ptr.read_text(encoding="utf-8").strip()
        if raw:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = project_dir / candidate
            if candidate.is_dir():
                return candidate

    if backups_root.is_dir():
        dirs = sorted((d for d in backups_root.iterdir() if d.is_dir()), reverse=True)
        if dirs:
            return dirs[0]

    raise FileNotFoundError("No backup directory found under backups/. Run scripts/inspect_and_backup.py first.")


def sanitize_filename_part(value: str, fallback: str = "ANDROID") -> str:
    """Sanitize a device model or identifier for cross-platform folder names."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip()).strip("._-")
    return cleaned or fallback


def verify_apk_zip(apk_path: Path) -> tuple[bool, str | None]:
    """Verify APK integrity as a valid ZIP archive containing AndroidManifest.xml."""
    try:
        with zipfile.ZipFile(apk_path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                return False, f"corrupt entry: {bad}"
            if "AndroidManifest.xml" not in zf.namelist():
                return False, "missing AndroidManifest.xml"
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def verify_apk_manifest(
    backup_dir: Path, apk_manifest: dict[str, Any]
) -> tuple[dict[str, list[Path]], list[dict[str, str]]]:
    """Verify SHA-256 and ZIP integrity for every package recorded in apk_manifest.json."""
    verified: dict[str, list[Path]] = {}
    corrupt_or_missing: list[dict[str, str]] = []
    for pkg, meta in apk_manifest.get("backed_up_packages", {}).items():
        files = meta.get("files", [])
        if not files:
            corrupt_or_missing.append({"package_name": pkg, "reason": "No APK files listed in manifest"})
            continue
        paths: list[Path] = []
        fail_reason: str | None = None
        for fmeta in files:
            rel = fmeta.get("local_rel_path", "")
            fpath = backup_dir / rel
            if not rel or not fpath.is_file():
                fail_reason = f"Missing backed-up APK file: {rel or '<empty>'}"
                break
            if sha256_file(fpath) != fmeta.get("sha256"):
                fail_reason = f"SHA-256 mismatch for {rel}"
                break
            ok_zip, zip_err = verify_apk_zip(fpath)
            if not ok_zip:
                fail_reason = f"Invalid/corrupt APK ZIP ({rel}): {zip_err}"
                break
            paths.append(fpath)
        if fail_reason is None and paths:
            verified[pkg] = paths
        else:
            corrupt_or_missing.append({"package_name": pkg, "reason": fail_reason or "Unknown verification failure"})
    return verified, corrupt_or_missing


def detect_os_family(device_info: dict[str, Any]) -> str:
    """Detect broad Android skin family from device properties."""
    if (device_info.get("hyperos_version") or "").strip():
        return "hyperos"
    if (device_info.get("miui_ui_version") or "").strip():
        return "miui"
    return "android"


def is_tested_device(
    device_info: dict[str, Any], tested_devices: list[dict[str, Any]] | None
) -> tuple[bool, str]:
    """Check whether device_info matches any hardware-tested entry declared by a profile."""
    if not tested_devices:
        return False, "Profile declares no hardware-tested reference devices."

    model = (device_info.get("model") or "").strip()
    android_ver = (device_info.get("android_version") or "").strip()
    miui_ver = (device_info.get("miui_ui_version") or "").strip()
    hyperos_ver = (device_info.get("hyperos_version") or "").strip()
    rom_inc = (device_info.get("rom_incremental") or "").strip()

    for ref in tested_devices:
        if ref.get("model") and model != ref["model"]:
            continue
        if ref.get("android_version") and android_ver != str(ref["android_version"]):
            continue
        if ref.get("miui_ui_version") and miui_ver != ref["miui_ui_version"]:
            continue
        if ref.get("hyperos_version") and hyperos_ver != ref["hyperos_version"]:
            continue
        if ref.get("rom_contains") and ref["rom_contains"] not in rom_inc:
            continue
        label = ref.get("marketing_name") or model or "reference device"
        return True, f"Matches hardware-tested baseline: {label} ({model}, Android {android_ver}, {rom_inc})"

    return False, (
        f"Untested hardware/OS variant: {device_info.get('brand', '?')} {model or '?'} "
        f"(Android {android_ver or '?'}, MIUI={miui_ver or '-'}, HyperOS={hyperos_ver or '-'}, ROM={rom_inc or '?'})"
    )


def evaluate_profile_compatibility(device_info: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Validate whether a cleanup profile matches the target device OEM and OS family."""
    compat = profile.get("compatibility") if isinstance(profile.get("compatibility"), dict) else {}
    target_brands = [b.lower() for b in compat.get("target_brands", [])]
    target_os = [o.lower() for o in compat.get("target_os_families", [])]

    brand = (device_info.get("brand") or "").strip().lower()
    manufacturer = (device_info.get("manufacturer") or "").strip().lower()
    os_family = detect_os_family(device_info)
    tested_devices_list = compat.get("tested_devices") or []
    requires_untested_opt_in = bool(tested_devices_list or compat.get("require_untested_opt_in"))
    tested, tested_reason = is_tested_device(device_info, tested_devices_list)

    brand_ok = not target_brands or brand in target_brands or manufacturer in target_brands
    os_ok = not target_os or os_family in target_os
    compatible = brand_ok and os_ok

    warnings: list[str] = []
    if not compatible:
        reason = (
            f"Incompatible profile '{profile.get('profile_name')}': device is "
            f"{device_info.get('brand', '?')} / {device_info.get('manufacturer', '?')} ({os_family}), "
            f"expected brands={target_brands or ['any']} and OS={target_os or ['any']}."
        )
    elif tested:
        reason = tested_reason
    else:
        reason = f"Device matches profile OEM/OS family ({brand or manufacturer}/{os_family}), but is UNTESTED."
        warnings.append(tested_reason)
        warnings.extend(compat.get("untested_notes", []))

    return {
        "compatible": compatible,
        "tested": tested,
        "requires_untested_opt_in": requires_untested_opt_in,
        "device_brand": device_info.get("brand") or manufacturer,
        "device_model": device_info.get("model"),
        "os_family": os_family,
        "reason": reason,
        "warnings": warnings,
    }


def check_device_identity_match(
    baseline_info: dict[str, Any], live_info: dict[str, Any], strict_serial: bool = False
) -> tuple[bool, str]:
    """Confirm connected live device matches baseline backup hardware identity (USB/Wi-Fi safe)."""
    checks = []
    for field in ("brand", "model", "device", "android_version"):
        base_val = (baseline_info.get(field) or "").strip()
        live_val = (live_info.get(field) or "").strip()
        if base_val and live_val and base_val.lower() != live_val.lower():
            checks.append(f"{field}: backup='{base_val}' vs live='{live_val}'")

    if strict_serial:
        base_s = (baseline_info.get("serial") or "").strip()
        live_s = (live_info.get("serial") or "").strip()
        if base_s and live_s and base_s != live_s:
            checks.append(f"serial: backup='{base_s}' vs live='{live_s}'")

    if checks:
        return False, "Connected device mismatch against backup baseline: " + "; ".join(checks)
    return True, (
        f"Verified hardware identity match: {live_info.get('brand', '?')} {live_info.get('model', '?')} "
        f"(Android {live_info.get('android_version', '?')})"
    )


def read_live_device_identity(serial: str, adb_path: str, run_adb_fn: Any) -> dict[str, str]:
    """Read minimal hardware identity properties from a live ADB device."""
    prop_map = {
        "model": "ro.product.model",
        "brand": "ro.product.brand",
        "manufacturer": "ro.product.manufacturer",
        "device": "ro.product.device",
        "android_version": "ro.build.version.release",
        "miui_ui_version": "ro.miui.ui.version.name",
        "hyperos_version": "ro.mi.os.version.name",
        "rom_incremental": "ro.build.version.incremental",
    }
    cmd = "; ".join(f"echo '{k}='$(getprop {p})" for k, p in prop_map.items())
    res = run_adb_fn(["shell", cmd], serial=serial, adb_path=adb_path)
    out: dict[str, str] = {"serial": serial}
    for line in res.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out
