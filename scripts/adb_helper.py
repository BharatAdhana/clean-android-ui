"""Cross-platform ADB & Android SDK helper with USB/Wireless pairing & discovery."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable


def _candidate_sdk_roots() -> list[Path]:
    """Return plausible Android SDK root directories for Windows, macOS, and Linux."""
    home = Path.home()
    roots: list[Path] = []
    for env_key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(env_key, "").strip()
        if val:
            roots.append(Path(val))

    sys_name = platform.system().lower()
    if sys_name == "windows":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            roots.append(Path(local_app_data) / "Android" / "Sdk")
        roots.extend([Path("C:/Android/Sdk"), Path("C:/platform-tools")])
    elif sys_name == "darwin":
        roots.extend([home / "Library" / "Android" / "sdk", Path("/opt/homebrew/share/android-commandlinetools")])
    else:
        roots.extend([home / "Android" / "Sdk", Path("/opt/android-sdk"), Path("/usr/lib/android-sdk")])
    return roots


def find_adb() -> str:
    """Locate adb executable on PATH or standard Android SDK platform-tools locations."""
    found = shutil.which("adb")
    if found:
        return found

    exe_name = "adb.exe" if platform.system().lower() == "windows" else "adb"
    for root in _candidate_sdk_roots():
        for candidate in (root / "platform-tools" / exe_name, root / exe_name):
            if candidate.is_file():
                return str(candidate)
    raise FileNotFoundError(
        "adb not found on PATH or standard Android SDK locations. "
        "Install Android Platform Tools and add it to PATH."
    )


def find_aapt2() -> str | None:
    """Locate newest aapt2 binary in Android SDK build-tools or PATH, if installed."""
    found = shutil.which("aapt2")
    if found:
        return found

    exe_name = "aapt2.exe" if platform.system().lower() == "windows" else "aapt2"
    for root in _candidate_sdk_roots():
        build_tools = root / "build-tools"
        if not build_tools.is_dir():
            continue
        versions = sorted((p for p in build_tools.iterdir() if p.is_dir()), reverse=True)
        for ver in versions:
            candidate = ver / exe_name
            if candidate.is_file():
                return str(candidate)
    return None


def run_cmd(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run an external command cleanly with UTF-8 decoding."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_adb(
    args: Iterable[str],
    serial: str | None = None,
    timeout: int = 120,
    adb_path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute an ADB command, optionally targeting a specific device serial."""
    exe = adb_path or find_adb()
    cmd = [exe]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return run_cmd(cmd, timeout=timeout)


def parse_adb_devices(output: str) -> list[dict[str, str]]:
    """Parse `adb devices -l` output into device metadata records across all states."""
    devices: list[dict[str, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        meta: dict[str, str] = {"serial": serial, "state": state}
        for token in parts[2:]:
            if ":" in token:
                k, v = token.split(":", 1)
                meta[k] = v
        devices.append(meta)
    return devices


def list_connected_devices(adb_path: str | None = None, only_ready: bool = True) -> list[dict[str, str]]:
    """Return attached ADB devices (`state == 'device'` when only_ready=True)."""
    res = run_adb(["devices", "-l"], adb_path=adb_path)
    if res.returncode != 0:
        raise RuntimeError(f"adb devices failed: {res.stderr.strip()}")
    devices = parse_adb_devices(res.stdout)
    if only_ready:
        return [d for d in devices if d.get("state") == "device"]
    return devices


def discover_mdns_endpoints(adb_path: str | None = None) -> list[str]:
    """Inspect `adb mdns services` and return candidate TLS wireless connect endpoints."""
    res = run_adb(["mdns", "services"], adb_path=adb_path)
    endpoints: list[str] = []
    for line in res.stdout.splitlines():
        if "_adb-tls-connect._tcp" in line:
            parts = line.split()
            if parts:
                endpoints.append(parts[-1].strip())
    return endpoints


def pair_wireless(endpoint: str, code: str, adb_path: str | None = None) -> tuple[bool, str]:
    """Pair with Android 11+ Wireless debugging using IP:port and 6-digit pairing code."""
    res = run_adb(["pair", endpoint.strip(), code.strip()], timeout=30, adb_path=adb_path)
    combined = (res.stdout + "\n" + res.stderr).strip()
    ok = res.returncode == 0 and ("successfully paired" in combined.lower() or "paired to" in combined.lower())
    return ok, combined


def connect_wireless(endpoint: str, adb_path: str | None = None) -> tuple[bool, str]:
    """Connect to Wireless ADB endpoint (`IP:port` or mDNS service name)."""
    res = run_adb(["connect", endpoint.strip()], timeout=30, adb_path=adb_path)
    combined = (res.stdout + "\n" + res.stderr).strip()
    ok = res.returncode == 0 and ("connected to" in combined.lower() or "already connected" in combined.lower())
    return ok, combined


def resolve_device_serial(preferred_serial: str | None = None, model_hint: str | None = None) -> str:
    """Resolve a single authorized ADB serial via USB, active wireless session, or mDNS auto-connect."""
    adb_path = find_adb()
    all_seen = list_connected_devices(adb_path=adb_path, only_ready=False)
    ready = [d for d in all_seen if d.get("state") == "device"]

    if not ready:
        for endpoint in discover_mdns_endpoints(adb_path=adb_path):
            connect_wireless(endpoint, adb_path=adb_path)
        all_seen = list_connected_devices(adb_path=adb_path, only_ready=False)
        ready = [d for d in all_seen if d.get("state") == "device"]

    if not ready:
        unauthorized = [d["serial"] for d in all_seen if d.get("state") == "unauthorized"]
        if unauthorized:
            raise RuntimeError(
                f"Device attached but UNAUTHORIZED ({', '.join(unauthorized)}). "
                "Unlock the phone and tap 'Allow USB/Wireless debugging' on screen."
            )
        raise RuntimeError(
            "No authorized ADB devices found. Connect via USB or Wireless debugging on the same Wi-Fi."
        )

    if preferred_serial:
        for dev in ready:
            if dev["serial"] == preferred_serial:
                return dev["serial"]
        raise RuntimeError(
            f"Requested ADB serial '{preferred_serial}' is not ready. Available: {[d['serial'] for d in ready]}"
        )

    if model_hint:
        hinted = [d for d in ready if d.get("model") == model_hint]
        if len(hinted) == 1:
            return hinted[0]["serial"]

    if len(ready) == 1:
        return ready[0]["serial"]

    raise RuntimeError(
        "Multiple ADB devices connected; pass --serial explicitly: "
        + ", ".join(f"{d['serial']} (model={d.get('model', '?')})" for d in ready)
    )


def _cli_doctor() -> int:
    """Print local tool paths, mDNS services, and all visible ADB targets."""
    try:
        adb_path = find_adb()
        print(f"adb: {adb_path}")
    except FileNotFoundError as exc:
        print(f"adb: MISSING ({exc})", file=sys.stderr)
        return 1

    aapt2_path = find_aapt2()
    print(f"aapt2: {aapt2_path or 'not found (APK version/package still works; app label lookup falls back)'}")

    devices = list_connected_devices(adb_path=adb_path, only_ready=False)
    print(f"attached_devices: {len(devices)}")
    for d in devices:
        print(f"  - serial={d['serial']} state={d['state']} model={d.get('model', '?')} device={d.get('device', '?')}")

    mdns = discover_mdns_endpoints(adb_path=adb_path)
    print(f"mdns_wireless_services: {mdns}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-platform ADB connection & diagnostics tool")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check adb, aapt2, connected devices, and mDNS wireless endpoints")

    p_pair = sub.add_parser("pair", help="Pair Android 11+ Wireless debugging endpoint with 6-digit code")
    p_pair.add_argument("--endpoint", required=True, help="IP:pairing_port shown on phone")
    p_pair.add_argument("--code", required=True, help="6-digit Wi-Fi pairing code")

    p_conn = sub.add_parser("connect", help="Connect Wireless ADB endpoint or auto-discover mDNS")
    p_conn.add_argument("--endpoint", help="IP:port from Wireless debugging screen (omit with --mdns)")
    p_conn.add_argument("--mdns", action="store_true", help="Auto-connect via adb mdns services")

    args = parser.parse_args()
    if args.command == "doctor":
        raise SystemExit(_cli_doctor())
    if args.command == "pair":
        ok, msg = pair_wireless(args.endpoint, args.code)
        print(msg)
        raise SystemExit(0 if ok else 1)
    if args.command == "connect":
        adb_path = find_adb()
        if args.mdns and not args.endpoint:
            eps = discover_mdns_endpoints(adb_path=adb_path)
            if not eps:
                print("No _adb-tls-connect._tcp mDNS services found.", file=sys.stderr)
                raise SystemExit(1)
            any_ok = False
            for ep in eps:
                ok, msg = connect_wireless(ep, adb_path=adb_path)
                print(msg)
                any_ok = any_ok or ok
            raise SystemExit(0 if any_ok else 1)
        if not args.endpoint:
            print("Pass --endpoint IP:PORT or --mdns.", file=sys.stderr)
            raise SystemExit(1)
        ok, msg = connect_wireless(args.endpoint, adb_path=adb_path)
        print(msg)
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
