"""Shared safety guardrails, hashing, and backup resolution for Clean MIUI AI."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

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
