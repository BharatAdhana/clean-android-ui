"""Unit tests for Clean MIUI AI core parsers, safety gates, profile logic, and restore verification."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import adb_helper  # noqa: E402
import analyze_backup  # noqa: E402
import apply_changes  # noqa: E402
import inspect_and_backup  # noqa: E402
import restore  # noqa: E402
import safety  # noqa: E402


def _make_valid_apk(path: Path, payload: bytes = b"manifest-data") -> str:
    """Create a minimal valid ZIP APK containing AndroidManifest.xml and return its SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("AndroidManifest.xml", payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestAdbHelper(unittest.TestCase):
    def test_parse_adb_devices_mixed_states(self) -> None:
        raw = "\n".join(
            [
                "List of devices attached",
                "192.168.1.25:41234     device product:rosemary_in model:M2101K7BI device:rosemary",
                "USB123456              unauthorized usb:1-2",
                "192.168.1.99:5555      offline",
                "",
            ]
        )
        parsed = adb_helper.parse_adb_devices(raw)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0]["serial"], "192.168.1.25:41234")
        self.assertEqual(parsed[0]["state"], "device")
        self.assertEqual(parsed[0]["model"], "M2101K7BI")
        self.assertEqual(parsed[1]["state"], "unauthorized")
        self.assertEqual(parsed[2]["state"], "offline")


class TestSafetyAndPaths(unittest.TestCase):
    def test_protected_packages_static_and_dynamic(self) -> None:
        blocked_sys, _ = safety.is_protected_package("com.android.systemui")
        self.assertTrue(blocked_sys)

        blocked_prefix, _ = safety.is_protected_package("com.android.providers.telephony")
        self.assertTrue(blocked_prefix)

        blocked_ime, reason = safety.is_protected_package(
            "com.google.android.inputmethod.latin",
            dynamic_protected={"com.google.android.inputmethod.latin"},
        )
        self.assertTrue(blocked_ime)
        self.assertIn("Launcher", reason or "")

        allowed, _ = safety.is_protected_package("com.miui.msa.global")
        self.assertFalse(allowed)

    def test_resolve_backup_dir_relative_latest_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bdir = root / "backups" / "20261111_120000_MODEL"
            bdir.mkdir(parents=True)
            (root / "backups" / "LATEST_BACKUP.txt").write_text(
                "backups/20261111_120000_MODEL", encoding="utf-8"
            )
            resolved = safety.resolve_backup_dir(root, None)
            self.assertEqual(resolved, bdir)

    def test_sanitize_filename_part(self) -> None:
        self.assertEqual(safety.sanitize_filename_part("Redmi Note 10S!"), "Redmi_Note_10S")
        self.assertEqual(safety.sanitize_filename_part("   ", fallback="ANDROID"), "ANDROID")


class TestInspectionParsers(unittest.TestCase):
    def test_pm_list_and_settings_parsers(self) -> None:
        pm_f = "package:/data/app/~~abc==/com.whatsapp-xyz==/base.apk=com.whatsapp\n"
        self.assertEqual(
            inspect_and_backup.parse_pm_list_f(pm_f),
            {"com.whatsapp": "/data/app/~~abc==/com.whatsapp-xyz==/base.apk"},
        )
        pm_s = "package:com.android.chrome\npackage:com.google.android.apps.nbu.files\n"
        self.assertEqual(
            inspect_and_backup.parse_pm_list_simple(pm_s),
            {"com.android.chrome", "com.google.android.apps.nbu.files"},
        )
        settings_raw = "font_scale=1.0\nuser_key=a=b=c\ninvalid_line\n"
        self.assertEqual(
            inspect_and_backup.parse_settings_list(settings_raw),
            {"font_scale": "1.0", "user_key": "a=b=c"},
        )

    def test_classify_package_cross_oem(self) -> None:
        self.assertEqual(inspect_and_backup.classify_package("com.miui.msa.global", True, "/system/app/MSA"), "manufacturer_xiaomi")
        self.assertEqual(inspect_and_backup.classify_package("com.google.android.gms", True, "/system/priv-app/GmsCore"), "google")
        self.assertEqual(inspect_and_backup.classify_package("com.samsung.android.bixby.agent", True, "/system/priv-app"), "manufacturer_samsung")
        self.assertEqual(inspect_and_backup.classify_package("com.oplus.camera", True, "/system/priv-app"), "manufacturer_bbk")
        self.assertEqual(inspect_and_backup.classify_package("com.android.systemui", True, "/system_ext/priv-app"), "system_aosp")
        self.assertEqual(inspect_and_backup.classify_package("com.example.app", False, "/data/app/foo"), "third_party_user")

    def test_should_pull_apk_and_verify_zip(self) -> None:
        self.assertFalse(
            inspect_and_backup.should_pull_apk({"package_name": "com.android.systemui", "category": "system_aosp"})
        )
        self.assertTrue(
            inspect_and_backup.should_pull_apk({"package_name": "com.miui.msa.global", "category": "manufacturer_xiaomi"})
        )

        with tempfile.TemporaryDirectory() as tmp:
            good_apk = Path(tmp) / "good.apk"
            _make_valid_apk(good_apk)
            self.assertEqual(inspect_and_backup.verify_apk_zip(good_apk), (True, None))

            bad_zip = Path(tmp) / "no_manifest.apk"
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("classes.dex", b"123")
            ok, reason = inspect_and_backup.verify_apk_zip(bad_zip)
            self.assertFalse(ok)
            self.assertIn("AndroidManifest.xml", reason or "")


class TestProfileAnalysisAndSafetyGates(unittest.TestCase):
    def test_profiles_are_valid_json(self) -> None:
        for fname in ("xiaomi_miui_hyperos_clean.json", "generic_android_audit.json"):
            data = json.loads((REPO_ROOT / "profiles" / fname).read_text(encoding="utf-8"))
            self.assertIn("tier_a_safe_bloat_and_ads", data)

    def test_evaluate_profile_checks_prerequisites_and_active_ime(self) -> None:
        profile = json.loads((REPO_ROOT / "profiles" / "xiaomi_miui_hyperos_clean.json").read_text(encoding="utf-8"))
        inventory = [
            {
                "package_name": "com.miui.msa.global",
                "app_label": "msa",
                "installed_user0": True,
                "enabled_code_user0": 0,
                "is_system": True,
            },
            {
                "package_name": "com.miui.android.fashiongallery",
                "app_label": "Glance",
                "installed_user0": True,
                "enabled_code_user0": 3,  # already disabled (rule action is disable_user)
                "is_system": False,
            },
            {
                "package_name": "com.facemoji.lite.xiaomi",
                "app_label": "Emoji Keyboard",
                "installed_user0": True,
                "enabled_code_user0": 0,
                "is_system": False,
            },
            {
                "package_name": "com.mi.android.globalFileexplorer",
                "app_label": "File Manager",
                "installed_user0": True,
                "enabled_code_user0": 0,
                "is_system": True,
            },
            {
                "package_name": "com.google.android.apps.nbu.files",
                "app_label": "Files by Google",
                "installed_user0": True,
                "enabled_code_user0": 0,
                "is_system": False,
            },
        ]
        settings_snap = {"secure": {"default_input_method": "com.google.android.inputmethod.latin/.LatinIME"}}
        res = analyze_backup.evaluate_profile_candidates(
            inventory=inventory,
            profile=profile,
            settings_snapshot=settings_snap,
            ad_audit={"key_package_audit": {}},
            include_tiers=("tier_a_safe_bloat_and_ads", "tier_b_google_stock_replacement"),
        )
        proposed_names = {p["package_name"] for p in res["proposed_packages"]}
        skipped_names = {s["package_name"] for s in res["skipped_prerequisites"]}

        self.assertIn("com.miui.msa.global", proposed_names)
        self.assertNotIn("com.miui.android.fashiongallery", proposed_names)  # already disabled_user (code 3)
        self.assertIn("com.mi.android.globalFileexplorer", proposed_names)  # Files by Google present
        self.assertIn("com.facemoji.lite.xiaomi", skipped_names)  # Gboard absent -> skipped!

    def test_validate_plan_safety_blocks_unapproved_and_protected(self) -> None:
        with self.assertRaises(RuntimeError):
            apply_changes.validate_plan_safety({"approved": False, "packages": []})

        with self.assertRaises(RuntimeError):
            apply_changes.validate_plan_safety(
                {"approved": True, "packages": [{"package_name": "com.android.systemui", "action": "disable_user"}]}
            )

        with self.assertRaises(RuntimeError):
            apply_changes.validate_plan_safety(
                {"approved": True, "packages": [{"package_name": "com.custom.launcher", "action": "disable_user"}]},
                dynamic_protected={"com.custom.launcher"},
            )

        apply_changes.validate_plan_safety(
            {"approved": True, "packages": [{"package_name": "com.miui.msa.global", "action": "uninstall_user0"}]}
        )


class TestRestoreVerificationAndDrift(unittest.TestCase):
    def test_verify_backup_apks_hash_and_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp)
            apk = bdir / "apks" / "com.demo.app" / "base.apk"
            digest = _make_valid_apk(apk)
            manifest = {
                "backed_up_packages": {
                    "com.demo.app": {
                        "files": [{"local_rel_path": "apks/com.demo.app/base.apk", "sha256": digest}]
                    }
                }
            }
            verified = restore.verify_backup_apks(bdir, manifest)
            self.assertEqual(list(verified.keys()), ["com.demo.app"])

            # Corrupt hash should fail verification
            manifest["backed_up_packages"]["com.demo.app"]["files"][0]["sha256"] = "0" * 64
            self.assertEqual(restore.verify_backup_apks(bdir, manifest), {})

    def test_find_drifted_packages_and_dry_run_commands(self) -> None:
        inventory = [
            {
                "package_name": "com.miui.msa.global",
                "installed_user0": True,
                "enabled_code_user0": 0,
                "is_system": True,
                "pkg_flags": ["SYSTEM", "HAS_CODE"],
            },
            {
                "package_name": "com.shared.static.lib",
                "installed_user0": True,
                "enabled_code_user0": 0,
                "is_system": True,
                "pkg_flags": ["SYSTEM"],  # shared static library (no HAS_CODE)
            },
        ]

        def fake_run_adb(args: list[str], serial: str | None = None, adb_path: str | None = None):
            return SimpleNamespace(returncode=0, stdout="User 0: ceDataInode=1 installed=true enabled=0", stderr="")

        with patch.object(restore, "run_adb", side_effect=fake_run_adb):
            missing, mismatch = restore.find_drifted_packages(
                inventory=inventory,
                live_installed=set(),
                live_disabled=set(),
                serial="TESTSERIAL",
                adb_path="adb",
            )
        self.assertEqual([r["package_name"] for r in missing], ["com.miui.msa.global"])
        self.assertEqual(mismatch, [])

        ok_sys, msg_sys = restore.restore_package(
            "com.miui.msa.global",
            {"is_system": True},
            {},
            "TESTSERIAL",
            "adb",
            dry_run=True,
        )
        self.assertTrue(ok_sys)
        self.assertIn("install-existing", msg_sys)

        ok_apk, msg_apk = restore.restore_package(
            "com.facemoji.lite.xiaomi",
            {"is_system": False},
            {"com.facemoji.lite.xiaomi": [Path("base.apk")]},
            "TESTSERIAL",
            "adb",
            dry_run=True,
        )
        self.assertTrue(ok_apk)
        self.assertIn("install -r", msg_apk)


# ---------------------------------------------------------------------------
# Phase 2, 3, 5, 6 Reliability, Compatibility, Reporting & End-to-End Tests
# ---------------------------------------------------------------------------

import io
import shlex
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import verify_device

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_valid_apk(path: Path, payload: bytes = b"fake-dex-and-manifest") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("AndroidManifest.xml", payload)
        zf.writestr("classes.dex", b"dex\n035\x00" + payload)
    return safety.sha256_file(path)


class _FakeAdbResult:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeAndroidDevice:
    """In-memory mock Android device answering ADB commands consistently."""

    def __init__(
        self,
        *,
        brand: str = "Xiaomi",
        manufacturer: str = "Xiaomi",
        model: str = "M2101K7BI",
        device: str = "rosemary",
        android_version: str = "13",
        miui_version_name: str = "V14",
        miui_incremental: str = "V14.0.11.0.TKLINXM",
        fingerprint: str = "Redmi/rosemary_in/rosemary:13/TP1A.220624.014/V14.0.11.0.TKLINXM:user/release-keys",
    ) -> None:
        self.props = {
            "ro.product.brand": brand,
            "ro.product.manufacturer": manufacturer,
            "ro.product.model": model,
            "ro.product.device": device,
            "ro.build.version.release": android_version,
            "ro.miui.ui.version.name": miui_version_name,
            "ro.build.version.incremental": miui_incremental,
            "ro.mi.os.version.name": "",
            "ro.build.fingerprint": fingerprint,
            "ro.build.version.sdk": "33",
        }
        self.packages: dict[str, dict[str, Any]] = {
            "com.android.systemui": {"path": "/system/priv-app/SystemUI/SystemUI.apk", "system": True, "enabled": True},
            "com.android.phone": {"path": "/system/priv-app/TeleService/TeleService.apk", "system": True, "enabled": True},
            "com.android.settings": {"path": "/system/priv-app/Settings/Settings.apk", "system": True, "enabled": True},
            "com.google.android.gms": {"path": "/product/priv-app/GmsCore/GmsCore.apk", "system": True, "enabled": True},
            "com.android.vending": {"path": "/product/priv-app/Phonesky/Phonesky.apk", "system": True, "enabled": True},
            "com.miui.home": {"path": "/system/priv-app/MiuiHome/MiuiHome.apk", "system": True, "enabled": True},
            "com.google.android.inputmethod.latin": {"path": "/data/app/gboard/base.apk", "system": False, "enabled": True},
            "com.google.android.dialer": {"path": "/product/priv-app/GoogleDialer/GoogleDialer.apk", "system": True, "enabled": True},
            "com.google.android.apps.messaging": {"path": "/product/app/PrebuiltBugle/PrebuiltBugle.apk", "system": True, "enabled": True},
            "com.android.chrome": {"path": "/product/app/Chrome/Chrome.apk", "system": True, "enabled": True},
            "com.miui.msa.global": {"path": "/system/app/MSA-Global/MSA-Global.apk", "system": True, "enabled": True},
            "com.mipay.wallet.in": {"path": "/data/app/mipay/base.apk", "system": False, "enabled": True},
        }
        self.settings: dict[tuple[str, str], str] = {
            ("global", "personalized_ad_enabled"): "1",
            ("secure", "default_input_method"): "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME",
        }
        self.appops: dict[tuple[str, str], str] = {
            ("com.miui.msa.global", "POST_NOTIFICATION"): "allow",
        }
        self.wm_physical_density = "440"
        self.wm_override_density: str | None = None
        self.fail_packages: set[str] = set()
        self.fail_uninstall_packages: set[str] = set()
        self.silent_ignore_settings: bool = False
        self.install_restricted: bool = False
        self.system_partition_packages: set[str] = {
            pkg for pkg, info in self.packages.items() if info["system"]
        }

    def run_adb(self, args: list[str], serial: str | None = None, adb_path: str | None = None, check: bool = False, **kwargs: Any) -> _FakeAdbResult:
        res = self._dispatch(args)
        if check and res.returncode != 0:
            raise RuntimeError(f"Mock ADB error ({args}): {res.stderr or res.stdout}")
        return res

    def _dispatch(self, args: list[str]) -> _FakeAdbResult:
        if len(args) >= 2 and args[0] == "logcat":
            return _FakeAdbResult(stdout="")
        if args and args[0] == "pull" and len(args) >= 3:
            _make_valid_apk(Path(args[2]))
            return _FakeAdbResult(stdout="1 file pulled.\n")
        if args and args[0] in ("install", "install-multiple"):
            if self.install_restricted:
                return _FakeAdbResult(stderr="Failure [INSTALL_FAILED_USER_RESTRICTED]", returncode=1)
            apk_parent_pkg = Path(args[-1]).parent.name
            self.packages[apk_parent_pkg] = {"path": f"/data/app/{apk_parent_pkg}/base.apk", "system": False, "enabled": True}
            return _FakeAdbResult(stdout="Success\n")
        if not args or args[0] != "shell":
            return _FakeAdbResult(stdout="")

        sh_raw = args[1:]
        if len(sh_raw) == 1 and "$(getprop " in sh_raw[0]:
            import re as _re

            lines = []
            for m in _re.finditer(r"echo '([^=]+)='\$\(getprop ([^)]+)\)", sh_raw[0]):
                k, prop = m.group(1), m.group(2)
                lines.append(f"{k}={self.props.get(prop, '')}")
            return _FakeAdbResult(stdout="\n".join(lines) + "\n")

        sh = shlex.split(" ".join(sh_raw)) if len(sh_raw) == 1 else list(sh_raw)
        if sh[:2] == ["cmd", "appops"]:
            sh = sh[1:]
        if sh[:2] == ["cmd", "package"] and len(sh) >= 3 and sh[2] == "install-existing":
            sh = ["pm"] + sh[2:]

        if sh[:1] == ["getprop"]:
            if len(sh) == 2:
                return _FakeAdbResult(stdout=self.props.get(sh[1], "") + "\n")
            lines = [f"[{k}]: [{v}]" for k, v in self.props.items() if v]
            return _FakeAdbResult(stdout="\n".join(lines) + "\n")

        if sh[:3] == ["cmd", "package", "resolve-activity"]:
            return _FakeAdbResult(stdout="com.miui.home/.launcher.Launcher\n")

        if sh[:3] == ["cmd", "role", "get-role-holders"]:
            role = sh[3] if len(sh) > 3 else ""
            if role == "android.app.role.DIALER":
                return _FakeAdbResult(stdout="com.google.android.dialer\n")
            if role == "android.app.role.SMS":
                return _FakeAdbResult(stdout="com.google.android.apps.messaging\n")
            return _FakeAdbResult(stdout="")

        if sh[:3] == ["pm", "list", "packages"]:
            show_d = "-d" in sh
            show_s = "-s" in sh
            show_3 = "-3" in sh
            lines = []
            for pkg, info in sorted(self.packages.items()):
                if show_d and info["enabled"]:
                    continue
                if show_s and not info["system"]:
                    continue
                if show_3 and info["system"]:
                    continue
                if "-f" in sh:
                    lines.append(f"package:{info['path']}={pkg}")
                else:
                    lines.append(f"package:{pkg}")
            return _FakeAdbResult(stdout="\n".join(lines) + "\n")

        if sh[:2] == ["pm", "path"] and len(sh) >= 3:
            pkg = sh[2]
            info = self.packages.get(pkg)
            if not info:
                return _FakeAdbResult(stdout="", returncode=1)
            return _FakeAdbResult(stdout=f"package:{info['path']}\n")

        if sh[:2] == ["pm", "list"] and len(sh) >= 3 and sh[2] == "users":
            return _FakeAdbResult(stdout="Users:\n\tUserInfo{0:Owner:4c13} running\n")

        if sh[:2] == ["stat", "-c"]:
            return _FakeAdbResult(stdout="1024\n")

        if sh[:2] == ["pm", "disable-user"]:
            pkg = sh[-1]
            if pkg in self.fail_packages:
                return _FakeAdbResult(stderr=f"Error disabling {pkg}", returncode=1)
            if pkg in self.packages:
                self.packages[pkg]["enabled"] = False
            return _FakeAdbResult(stdout=f"Package {pkg} new state: disabled-user\n")

        if sh[:2] in (["pm", "enable"], ["pm", "default-state"]):
            pkg = sh[-1]
            if pkg in self.fail_packages:
                return _FakeAdbResult(stderr=f"SecurityException: failed to enable {pkg}", returncode=1)
            if pkg in self.packages:
                self.packages[pkg]["enabled"] = True
                return _FakeAdbResult(stdout=f"Package {pkg} new state: enabled\n")
            return _FakeAdbResult(stderr=f"Unknown package: {pkg}", returncode=1)

        if sh[:2] == ["pm", "uninstall"]:
            pkg = sh[-1]
            if pkg in self.fail_uninstall_packages:
                return _FakeAdbResult(stderr="Failure [DELETE_FAILED_INTERNAL_ERROR]", returncode=1)
            if pkg in self.packages:
                del self.packages[pkg]
                return _FakeAdbResult(stdout="Success\n")
            return _FakeAdbResult(stdout="Failure [not installed for 0]", returncode=1)

        if sh[:2] == ["pm", "install-existing"]:
            pkg = sh[-1]
            if pkg in self.fail_packages or pkg not in self.system_partition_packages:
                return _FakeAdbResult(stdout="Failure [not present on system]", returncode=1)
            self.packages[pkg] = {"path": f"/system/app/{pkg}/{pkg}.apk", "system": True, "enabled": True}
            return _FakeAdbResult(stdout=f"Package {pkg} installed for user: 0\n")

        if sh[:2] == ["dumpsys", "package"] and len(sh) >= 3:
            pkg = sh[2]
            if pkg == "packages" and len(sh) == 3:
                blocks = ["Packages:\n"]
                for p_name, info in sorted(self.packages.items()):
                    ecode = 0 if info["enabled"] else 3
                    flags = "SYSTEM" if info["system"] else ""
                    blocks.append(
                        f"  Package [{p_name}] (deadbeef):\n"
                        f"    codePath={Path(info['path']).parent.as_posix()}\n"
                        f"    versionCode=100 minSdk=29 targetSdk=33\n"
                        f"    versionName=1.0\n"
                        f"    pkgFlags=[ {flags} ]\n"
                        f"    User 0: ceDataInode=1234 installed=true hidden=false enabled={ecode}\n"
                    )
                return _FakeAdbResult(stdout="".join(blocks))
            info = self.packages.get(pkg)
            if not info:
                return _FakeAdbResult(stdout="    User 0: installed=false\n")
            ecode = 0 if info["enabled"] else 3
            return _FakeAdbResult(stdout=f"    User 0: ceDataInode=1234 installed=true hidden=false enabled={ecode}\n")

        if sh[:2] == ["settings", "list"] and len(sh) >= 3:
            ns = sh[2]
            lines = [f"{k}={v}" for (n, k), v in sorted(self.settings.items()) if n == ns]
            return _FakeAdbResult(stdout="\n".join(lines) + ("\n" if lines else ""))

        if sh[:2] == ["settings", "get"]:
            ns, key = sh[2], sh[3]
            return _FakeAdbResult(stdout=self.settings.get((ns, key), "null") + "\n")

        if sh[:2] == ["settings", "put"]:
            ns, key, val = sh[2], sh[3], sh[4]
            if not self.silent_ignore_settings:
                self.settings[(ns, key)] = val
            return _FakeAdbResult(stdout="")

        if sh[:2] == ["settings", "delete"]:
            ns, key = sh[2], sh[3]
            self.settings.pop((ns, key), None)
            return _FakeAdbResult(stdout="")

        if sh[:2] == ["appops", "set"]:
            pkg, op, mode = sh[2], sh[3], sh[4]
            self.appops[(pkg, op)] = mode
            return _FakeAdbResult(stdout="")

        if sh[:2] == ["appops", "get"] and len(sh) >= 3:
            pkg = sh[2]
            if len(sh) >= 4:
                op = sh[3]
                mode = self.appops.get((pkg, op), "default")
                return _FakeAdbResult(stdout=f"{op}: {mode}\n")
            pkg_ops = [f"{o}: {m}" for (p, o), m in sorted(self.appops.items()) if p == pkg]
            return _FakeAdbResult(stdout="\n".join(pkg_ops) + ("\n" if pkg_ops else ""))

        if sh[:2] == ["wm", "density"]:
            if len(sh) == 2:
                out = f"Physical density: {self.wm_physical_density}\n"
                if self.wm_override_density:
                    out += f"Override density: {self.wm_override_density}\n"
                return _FakeAdbResult(stdout=out)
            if sh[2] == "reset":
                self.wm_override_density = None
            else:
                self.wm_override_density = sh[2]
            return _FakeAdbResult(stdout="")

        if sh[:2] == ["wm", "size"]:
            return _FakeAdbResult(stdout="Physical size: 1080x2400\n")

        if sh[:1] == ["df"]:
            return _FakeAdbResult(stdout="Filesystem Size Used Avail Use% Mounted on\n/dev/block/dm-5 100G 40G 60G 40% /data\n")

        return _FakeAdbResult(stdout="")


class TestInspectAndBackupRegressions(unittest.TestCase):
    def test_create_unique_backup_dir_avoids_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            first = inspect_and_backup.create_unique_backup_dir(project, "M2101K7BI")
            second = inspect_and_backup.create_unique_backup_dir(project, "M2101K7BI")
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())

    def test_pull_apks_rejects_corrupt_zip_and_logs_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp)
            inventory = [
                {
                    "package_name": "com.example.corrupt",
                    "app_label": "Corrupt Example",
                    "version_code": "100",
                    "version_name": "1.0",
                    "category": inspect_and_backup.classify_package("com.example.corrupt", is_system=False, code_path="/data/app/corrupt/base.apk"),
                    "is_system": False,
                    "installed_user0": True,
                    "enabled": True,
                    "apk_paths": ["/data/app/corrupt/base.apk"],
                },
                {
                    "package_name": "com.android.systemui",
                    "app_label": "System UI",
                    "version_code": "33",
                    "version_name": "13",
                    "category": inspect_and_backup.classify_package("com.android.systemui", is_system=True, code_path="/system/priv-app/SystemUI/SystemUI.apk"),
                    "is_system": True,
                    "installed_user0": True,
                    "enabled": True,
                    "apk_paths": ["/system/priv-app/SystemUI/SystemUI.apk"],
                },
                {
                    "package_name": "com.example.good",
                    "app_label": "Good Example",
                    "version_code": "100",
                    "version_name": "1.0",
                    "category": inspect_and_backup.classify_package("com.example.good", is_system=False, code_path="/data/app/good/base.apk"),
                    "is_system": False,
                    "installed_user0": True,
                    "enabled": True,
                    "apk_paths": ["/data/app/good/base.apk"],
                },
            ]

            def fake_run_adb(args: list[str], **kwargs: Any) -> _FakeAdbResult:
                if args and args[0] == "shell":
                    cmd = " ".join(args[1:])
                    if "pm path com.example.good" in cmd:
                        return _FakeAdbResult(stdout="package:/data/app/good/base.apk\n")
                    if "pm path com.example.corrupt" in cmd:
                        return _FakeAdbResult(stdout="package:/data/app/corrupt/base.apk\n")
                    if "stat -c" in cmd:
                        return _FakeAdbResult(stdout="1024\n")
                if args and args[0] == "pull":
                    remote_path, local_path = args[1], Path(args[2])
                    if "corrupt" in remote_path:
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        local_path.write_bytes(b"NOT_A_VALID_ZIP_PAYLOAD")
                    else:
                        _make_valid_apk(local_path)
                return _FakeAdbResult(stdout="")

            with mock.patch.object(inspect_and_backup, "run_adb", side_effect=fake_run_adb), redirect_stdout(io.StringIO()):
                manifest = inspect_and_backup.pull_apks_with_verification(
                    inventory=inventory,
                    backup_dir=backup_dir,
                    serial="MOCK123",
                    adb_path="adb",
                    aapt2_path=None,
                    skip_apk_pull=False,
                    max_apk_mb=120,
                )

            self.assertIn("com.example.good", manifest["backed_up_packages"], msg=f"manifest={manifest}")
            self.assertNotIn("com.example.corrupt", manifest["backed_up_packages"])
            self.assertIn("com.example.corrupt", manifest["skipped_or_failed"])
            self.assertIn("com.android.systemui", manifest["excluded_from_apk_pull"])
            self.assertFalse((backup_dir / "apks" / "com.example.corrupt").exists())
            self.assertIn("completeness_summary", manifest)

            by_cat: dict[str, list[dict[str, Any]]] = {}
            for row in inventory:
                by_cat.setdefault(row["category"], []).append(row)
            summary = inspect_and_backup.write_summary_json(
                backup_dir=backup_dir,
                device_info={"brand": "Redmi", "model": "M2101K7BI", "android_version": "13", "wm_density": "440", "default_home_activity": "com.miui.home/.Launcher"},
                settings_snapshot={"system": {"font_scale": "1.0"}, "secure": {"default_input_method": "com.google.android.inputmethod.latin/.LatinIME"}},
                ad_audit={"audit_scope_note": "Fixed KEY_AUDIT_PACKAGES only", "key_package_audit": {}},
                inventory=inventory,
                by_category=by_cat,
                apk_manifest=manifest,
            )
            self.assertTrue((backup_dir / "summary.json").is_file())
            self.assertEqual(summary["device"]["model"], "M2101K7BI")
            self.assertEqual(summary["backup_completeness"]["apk_backups_verified"], 1)

    def test_inspect_and_backup_cli_project_dir_with_spaces_and_validation(self) -> None:
        dev = _FakeAndroidDevice()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spaced_proj = root / "my backup dir" / "sub folder"

            with (
                mock.patch.object(sys, "argv", ["inspect_and_backup.py", "--project-dir", "   "]),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as empty_exit:
                    inspect_and_backup.main()
                self.assertEqual(empty_exit.exception.code, 2)

            with (
                mock.patch.object(inspect_and_backup, "find_adb", return_value="adb"),
                mock.patch.object(inspect_and_backup, "find_aapt2", return_value=None),
                mock.patch.object(inspect_and_backup, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(inspect_and_backup, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "inspect_and_backup.py",
                        "--serial",
                        "MOCK123",
                        "--project-dir",
                        str(spaced_proj),
                    ],
                ),
                redirect_stdout(io.StringIO()),
            ):
                rc_backup = inspect_and_backup.main()

            self.assertIsNone(rc_backup)
            latest_ptr = spaced_proj / "backups" / "LATEST_BACKUP.txt"
            self.assertTrue(latest_ptr.is_file())
            rel_latest = latest_ptr.read_text(encoding="utf-8").strip()
            backup_dir = spaced_proj / rel_latest
            self.assertTrue(backup_dir.is_dir())
            self.assertTrue((backup_dir / "device_info.json").is_file())
            self.assertTrue((backup_dir / "summary.json").is_file())
            apk_manifest = json.loads((backup_dir / "apk_manifest.json").read_text(encoding="utf-8"))
            self.assertIn("com.mipay.wallet.in", apk_manifest["backed_up_packages"])

            with (
                mock.patch.object(restore, "find_adb", return_value="adb"),
                mock.patch.object(restore, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(restore, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(
                    sys,
                    "argv",
                    ["restore.py", "--project-dir", str(spaced_proj), "--serial", "MOCK123", "--dry-run"],
                ),
                redirect_stdout(io.StringIO()) as dry_out,
            ):
                rc_restore = restore.main()
            self.assertIn(rc_restore, (None, 0))
            self.assertIn(backup_dir.name, dry_out.getvalue())


class TestCompatibilityAndAnalyzeRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.xiaomi_profile = json.loads(
            (REPO_ROOT / "profiles" / "xiaomi_miui_hyperos_clean.json").read_text(encoding="utf-8")
        )

    def test_os_family_and_compatibility_matrix(self) -> None:
        tested_info = {
            "brand": "Redmi",
            "manufacturer": "Xiaomi",
            "model": "M2101K7BI",
            "android_version": "13",
            "miui_ui_version": "V14",
            "rom_incremental": "V14.0.11.0.TKLINXM",
        }
        tested_res = safety.evaluate_profile_compatibility(tested_info, self.xiaomi_profile)
        self.assertTrue(tested_res["compatible"], msg=str(tested_res))
        self.assertTrue(tested_res["tested"], msg=str(tested_res))

        hyperos_info = {
            "brand": "Xiaomi",
            "manufacturer": "Xiaomi",
            "model": "23127PN0CG",
            "android_version": "14",
            "hyperos_version": "OS1.0.2.0.UNCMIXM",
        }
        self.assertEqual(safety.detect_os_family(hyperos_info), "hyperos")
        untested_res = safety.evaluate_profile_compatibility(hyperos_info, self.xiaomi_profile)
        self.assertTrue(untested_res["compatible"])
        self.assertFalse(untested_res["tested"])
        self.assertTrue(untested_res["warnings"])

        samsung_info = {
            "brand": "samsung",
            "manufacturer": "samsung",
            "model": "SM-S911B",
            "android_version": "14",
        }
        cross_res = safety.evaluate_profile_compatibility(samsung_info, self.xiaomi_profile)
        self.assertFalse(cross_res["compatible"])

    def test_analyze_backup_cli_gating_downgrade_and_optional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp)
            inventory = [
                {"package_name": "com.miui.msa.global", "category": "system_app", "is_system": True, "installed_user0": True, "enabled": True},
                {"package_name": "com.mipay.wallet.in", "category": "third_party", "is_system": False, "installed_user0": True, "enabled": True},
                {"package_name": "com.google.android.apps.safetyhub", "category": "system_app", "is_system": True, "installed_user0": True, "enabled": True},
            ]
            (bdir / "packages_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
            (bdir / "settings_snapshot.json").write_text(json.dumps({}), encoding="utf-8")
            (bdir / "ad_tracking_audit.json").write_text(json.dumps({}), encoding="utf-8")
            (bdir / "apk_manifest.json").write_text(json.dumps({"backed_up_packages": {}}), encoding="utf-8")

            # Cross-OEM Samsung blocked with exit code 2
            (bdir / "device_info.json").write_text(
                json.dumps({"brand": "samsung", "manufacturer": "samsung", "model": "SM-S911B", "android_version": "14"}),
                encoding="utf-8",
            )
            profile_path = str(REPO_ROOT / "profiles" / "xiaomi_miui_hyperos_clean.json")
            draft_out = Path(tmp) / "draft.json"
            with (
                mock.patch.object(sys, "argv", ["analyze_backup.py", "--backup-dir", str(bdir), "--profile", profile_path, "--emit-draft-plan", str(draft_out)]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    analyze_backup.main()
                self.assertEqual(ctx.exception.code, 2)

            # Untested Xiaomi/POCO HyperOS MUST be blocked unless --allow-untested-device is explicitly passed
            (bdir / "device_info.json").write_text(
                json.dumps({"brand": "POCO", "manufacturer": "Xiaomi", "model": "2311DRK48G", "android_version": "14", "hyperos_version": "OS1.0"}),
                encoding="utf-8",
            )
            untested_out = io.StringIO()
            untested_err = io.StringIO()
            with (
                mock.patch.object(sys, "argv", ["analyze_backup.py", "--backup-dir", str(bdir), "--profile", profile_path, "--emit-draft-plan", str(draft_out)]),
                redirect_stdout(untested_out),
                redirect_stderr(untested_err),
            ):
                with self.assertRaises(SystemExit) as ctx_untested:
                    analyze_backup.main()
                self.assertEqual(ctx_untested.exception.code, 2)
            combined_untested_msg = (untested_out.getvalue() + untested_err.getvalue()).upper()
            self.assertIn("UNTESTED", combined_untested_msg)
            self.assertIn("--ALLOW-UNTESTED-DEVICE", combined_untested_msg)

            with (
                mock.patch.object(sys, "argv", ["analyze_backup.py", "--backup-dir", str(bdir), "--profile", profile_path, "--allow-untested-device", "--include-optional", "--emit-draft-plan", str(draft_out)]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                analyze_backup.main()
            plan = json.loads(draft_out.read_text(encoding="utf-8"))
            self.assertFalse(plan["approved"])
            self.assertFalse(plan["compatibility"]["tested"])
            self.assertTrue(plan["compatibility"]["warnings"])
            actions = {item.get("package_name") or item.get("package"): item["action"] for item in plan["packages"]}
            self.assertIn("com.google.android.apps.safetyhub", actions)

            # Generic Android audit profile runs on Samsung with UNTESTED warning and flags universal Meta stubs only
            samsung_inv = inventory + [
                {"package_name": "com.facebook.system", "category": "system_app", "is_system": True, "installed_user0": True, "enabled": True},
            ]
            (bdir / "packages_inventory.json").write_text(json.dumps(samsung_inv), encoding="utf-8")
            (bdir / "device_info.json").write_text(
                json.dumps({"brand": "samsung", "manufacturer": "samsung", "model": "SM-S911B", "android_version": "14"}),
                encoding="utf-8",
            )
            generic_profile_path = str(REPO_ROOT / "profiles" / "generic_android_audit.json")
            generic_draft = Path(tmp) / "generic_draft.json"
            with (
                mock.patch.object(sys, "argv", ["analyze_backup.py", "--backup-dir", str(bdir), "--profile", generic_profile_path, "--emit-draft-plan", str(generic_draft)]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                analyze_backup.main()
            g_plan = json.loads(generic_draft.read_text(encoding="utf-8"))
            self.assertEqual([p["package_name"] for p in g_plan["packages"]], ["com.facebook.system"])

            # Non-system uninstall_user0 without backed-up APK downgrades to disable_user
            synthetic_profile = {
                "profile_name": "synthetic_downgrade_test",
                "compatibility": {"target_brands": ["xiaomi", "redmi"], "target_os_families": ["miui"]},
                "tier_a_safe_bloat": [{"package_name": "com.mipay.wallet.in", "action": "uninstall_user0", "reason": "test"}],
            }
            ev = analyze_backup.evaluate_profile_candidates(
                inventory=inventory,
                profile=synthetic_profile,
                settings_snapshot={},
                ad_audit={},
                include_tiers=["tier_a_safe_bloat"],
                apk_manifest={"backed_up_packages": {}},
            )
            self.assertEqual(ev["proposed_packages"][0]["action"], "disable_user")
            self.assertTrue(ev["backup_warnings"])


class TestApplyChangesRegressions(unittest.TestCase):
    def test_dynamic_dialer_sms_protection_and_device_mismatch_gate(self) -> None:
        dev = _FakeAndroidDevice()
        dyn = apply_changes.detect_dynamic_protected_packages("MOCK123", "adb")
        # Wait: ensure detect_dynamic_protected_packages uses our mocked run_adb
        with mock.patch.object(apply_changes, "run_adb", side_effect=dev.run_adb):
            dyn = apply_changes.detect_dynamic_protected_packages("MOCK123", "adb")
        self.assertIn("com.miui.home", dyn)
        self.assertIn("com.google.android.inputmethod.latin", dyn)
        self.assertIn("com.google.android.dialer", dyn)
        self.assertIn("com.google.android.apps.messaging", dyn)

    def test_validate_backup_blocks_unbacked_non_system_uninstall_and_device_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp)
            dev = _FakeAndroidDevice(brand="samsung", manufacturer="samsung", model="SM-S911B")
            (bdir / "device_info.json").write_text(
                json.dumps({"brand": "Xiaomi", "manufacturer": "Xiaomi", "model": "M2101K7BI", "serial": "MOCK123"}),
                encoding="utf-8",
            )
            (bdir / "packages_inventory.json").write_text(
                json.dumps([{"package_name": "com.mipay.wallet.in", "is_system": False, "enabled": True}]),
                encoding="utf-8",
            )
            (bdir / "apk_manifest.json").write_text(json.dumps({"backed_up_packages": {}}), encoding="utf-8")
            (bdir / "settings_snapshot.json").write_text(json.dumps({}), encoding="utf-8")
            plan_path = bdir / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "approved": True,
                        "packages": [{"package_name": "com.mipay.wallet.in", "action": "uninstall_user0"}],
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(apply_changes, "find_adb", return_value="adb"),
                mock.patch.object(apply_changes, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(apply_changes, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["apply_changes.py", "--plan", str(plan_path), "--backup-dir", str(bdir)]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises((SystemExit, RuntimeError)):
                    apply_changes.main()

            with self.assertRaises(RuntimeError):
                apply_changes.validate_backup_before_apply(
                    plan={"approved": True, "packages": [{"package_name": "com.mipay.wallet.in", "action": "uninstall_user0"}]},
                    backup_dir=bdir,
                    inv_by_pkg={"com.mipay.wallet.in": {"package_name": "com.mipay.wallet.in", "is_system": False, "installed_user0": True}},
                    apk_manifest={"backed_up_packages": {}},
                    allow_unrecoverable=False,
                )

            # allow_unrecoverable permits unbacked non-system uninstall_user0 when no corrupt APKs exist...
            verified_empty = apply_changes.validate_backup_before_apply(
                plan={"approved": True, "packages": [{"package_name": "com.mipay.wallet.in", "action": "uninstall_user0"}]},
                backup_dir=bdir,
                inv_by_pkg={"com.mipay.wallet.in": {"package_name": "com.mipay.wallet.in", "is_system": False, "installed_user0": True}},
                apk_manifest={"backed_up_packages": {}},
                allow_unrecoverable=True,
            )
            self.assertEqual(verified_empty, {})

            # ...but NEVER permits corrupt/tampered APK backups to bypass integrity verification
            corrupt_apk = bdir / "apks" / "com.example.tampered" / "base.apk"
            corrupt_apk.parent.mkdir(parents=True, exist_ok=True)
            corrupt_apk.write_bytes(b"NOT_A_VALID_APK")
            with self.assertRaises(RuntimeError) as ctx_corrupt:
                apply_changes.validate_backup_before_apply(
                    plan={"approved": True, "packages": []},
                    backup_dir=bdir,
                    inv_by_pkg={},
                    apk_manifest={
                        "backed_up_packages": {
                            "com.example.tampered": {
                                "files": [{"local_rel_path": "apks/com.example.tampered/base.apk", "sha256": "0" * 64}]
                            }
                        }
                    },
                    allow_unrecoverable=True,
                )
            self.assertIn("BACKUP INTEGRITY FAILURE", str(ctx_corrupt.exception))

    def test_unrecoverable_non_system_uninstall_reports_explicit_unrecoverable_status(self) -> None:
        dev = _FakeAndroidDevice()
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp)
            (bdir / "device_info.json").write_text(
                json.dumps(
                    {
                        "brand": "Xiaomi",
                        "manufacturer": "Xiaomi",
                        "model": "M2101K7BI",
                        "serial": "MOCK123",
                        "wm_density": "440",
                    }
                ),
                encoding="utf-8",
            )
            (bdir / "packages_inventory.json").write_text(
                json.dumps(
                    [
                        {
                            "package_name": "com.mipay.wallet.in",
                            "is_system": False,
                            "installed_user0": True,
                            "enabled": True,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (bdir / "apk_manifest.json").write_text(json.dumps({"backed_up_packages": {}}), encoding="utf-8")
            (bdir / "settings_snapshot.json").write_text(
                json.dumps({"global": {}, "secure": {}, "system": {}}), encoding="utf-8"
            )
            (bdir / "changelog.json").write_text(json.dumps({"modifications": []}), encoding="utf-8")
            (bdir / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
            plan_path = bdir / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "approved": True,
                        "packages": [
                            {
                                "package_name": "com.mipay.wallet.in",
                                "action": "uninstall_user0",
                                "reason": "explicitly approved unrecoverable removal",
                            }
                        ],
                        "settings": [],
                        "appops": [],
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(apply_changes, "find_adb", return_value="adb"),
                mock.patch.object(apply_changes, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(apply_changes, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "apply_changes.py",
                        "--plan",
                        str(plan_path),
                        "--backup-dir",
                        str(bdir),
                        "--serial",
                        "MOCK123",
                        "--allow-unrecoverable-uninstall",
                    ],
                ),
                redirect_stdout(io.StringIO()),
            ):
                rc = apply_changes.main()

            self.assertIsNone(rc)
            self.assertNotIn("com.mipay.wallet.in", dev.packages)

            report_jsons = list(bdir.glob("execution_report_*.json"))
            report_mds = list(bdir.glob("execution_report_*.md"))
            self.assertEqual(len(report_jsons), 1)
            self.assertEqual(len(report_mds), 1)

            report = json.loads(report_jsons[0].read_text(encoding="utf-8"))
            self.assertEqual(len(report["removed_applications"]), 1)
            removed_entry = report["removed_applications"][0]
            self.assertEqual(removed_entry["package_name"], "com.mipay.wallet.in")
            self.assertEqual(removed_entry["recovery_source"], "unrecoverable_no_apk_backup")
            self.assertNotEqual(removed_entry["recovery_source"], "local_apk_backup")
            self.assertFalse(removed_entry["recoverable"])
            self.assertIn("without a verified local APK backup", removed_entry.get("recovery_warning", ""))

            rr = report["restore_readiness"]
            self.assertEqual(rr["status"], "unrecoverable_removals_present")
            self.assertEqual(rr["unrecoverable_packages"], ["com.mipay.wallet.in"])
            self.assertTrue(rr["allow_unrecoverable_uninstall_used"])
            self.assertIn("CANNOT be restored locally from this backup", rr.get("note", ""))

            md_text = report_mds[0].read_text(encoding="utf-8")
            self.assertIn("unrecoverable_no_apk_backup", md_text)
            self.assertIn("unrecoverable_removals_present", md_text)
            self.assertIn("- Unrecoverable Removed Packages (No Local APK Backup): com.mipay.wallet.in", md_text)


class TestRestoreReliabilityRegressions(unittest.TestCase):
    def test_filter_applied_modifications_ignores_failed_and_keeps_earliest_old_value(self) -> None:
        mods = [
            {"type": "package", "package_name": "com.example.failed", "action": "disable_user", "success": False},
            {"type": "package", "package_name": "com.example.ok", "action": "disable_user", "success": True},
            {"type": "setting", "namespace": "global", "key": "k", "old_value": "orig", "new_value": "v1", "success": True},
            {"type": "setting", "namespace": "global", "key": "k", "old_value": "v1", "new_value": "v2", "success": True},
            {"type": "setting", "namespace": "global", "key": "k", "old_value": "v2", "new_value": "v3", "success": False},
        ]
        filtered = restore.filter_applied_modifications(mods)
        self.assertEqual(len(filtered["ignored_unsuccessful"]), 2)
        self.assertEqual(
            [p.get("package_name") or p.get("package") for p in filtered["packages"]],
            ["com.example.ok"],
            msg=f"filtered={filtered}",
        )
        self.assertEqual(len(filtered["settings"]), 1)
        self.assertEqual(filtered["settings"][0]["old_value"], "orig")

    def test_classify_restoration_mechanism(self) -> None:
        self.assertEqual(
            restore.classify_restoration_mechanism({"is_system": True, "installed_user0": False, "enabled": True}),
            "system_install_existing",
        )
        self.assertEqual(
            restore.classify_restoration_mechanism({"is_system": False, "installed_user0": False, "enabled": True}),
            "local_apk_install",
        )

    def test_partial_restore_failure_and_corrupt_apk_return_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp)
            dev = _FakeAndroidDevice()
            dev.packages["com.miui.msa.global"]["enabled"] = False
            dev.fail_packages.add("com.miui.msa.global")

            apk_path = bdir / "apks" / "com.example.app" / "base.apk"
            good_hash = _make_valid_apk(apk_path)

            (bdir / "device_info.json").write_text(
                json.dumps({"brand": "Xiaomi", "manufacturer": "Xiaomi", "model": "M2101K7BI", "serial": "MOCK123"}),
                encoding="utf-8",
            )
            (bdir / "packages_inventory.json").write_text(
                json.dumps([{"package_name": "com.miui.msa.global", "is_system": True, "installed_user0": True, "enabled": True, "enabled_code_user0": 0}]),
                encoding="utf-8",
            )
            manifest_data = {
                "backed_up_packages": {
                    "com.example.app": {
                        "files": [{"local_rel_path": "apks/com.example.app/base.apk", "sha256": good_hash}]
                    }
                }
            }
            (bdir / "apk_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")
            (bdir / "changelog.json").write_text(
                json.dumps(
                    {
                        "modifications": [
                            {"type": "package", "package_name": "com.miui.msa.global", "action": "disable_user", "success": True},
                            {"type": "package", "package_name": "com.example.never_changed", "action": "disable_user", "success": False},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            # Live restore fails on com.miui.msa.global -> must exit 1
            with (
                mock.patch.object(restore, "find_adb", return_value="adb"),
                mock.patch.object(restore, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(restore, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["restore.py", "--backup-dir", str(bdir), "--yes"]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    restore.main()
                self.assertEqual(ctx.exception.code, 1)

            # Tamper APK and ensure --dry-run exits 1
            apk_path.write_bytes(b"corrupted")
            with (
                mock.patch.object(restore, "find_adb", return_value="adb"),
                mock.patch.object(restore, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(restore, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["restore.py", "--backup-dir", str(bdir), "--dry-run"]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    restore.main()
                self.assertEqual(ctx.exception.code, 1)


class TestMockedEndToEndPipeline(unittest.TestCase):
    def test_apply_verify_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backup_m2101k7bi"
            bdir.mkdir(parents=True)
            dev = _FakeAndroidDevice()

            apk_path = bdir / "apks" / "com.mipay.wallet.in" / "base.apk"
            mipay_hash = _make_valid_apk(apk_path)

            baseline_device_info = {
                "brand": "Xiaomi",
                "manufacturer": "Xiaomi",
                "model": "M2101K7BI",
                "device": "rosemary",
                "android_version": "13",
                "miui_ui_version": "V14",
                "rom_incremental": "V14.0.11.0.TKLINXM",
                "serial": "MOCK123",
            }
            baseline_inventory = [
                {
                    "package_name": pkg,
                    "category": "system_app" if info["system"] else "third_party",
                    "is_system": info["system"],
                    "installed_user0": True,
                    "enabled": info["enabled"],
                    "enabled_code_user0": 0 if info["enabled"] else 3,
                }
                for pkg, info in dev.packages.items()
            ]
            baseline_settings = {"global": {"personalized_ad_enabled": "1"}, "system": {}, "secure": {}}
            baseline_manifest = {
                "backed_up_packages": {
                    "com.mipay.wallet.in": {
                        "files": [{"local_rel_path": "apks/com.mipay.wallet.in/base.apk", "sha256": mipay_hash}]
                    }
                },
                "skipped_or_failed": [],
                "excluded_from_apk_pull": [],
                "completeness_summary": {"backed_up_packages": 1, "skipped_or_failed": 0, "excluded_from_apk_pull": 10},
            }

            (bdir / "device_info.json").write_text(json.dumps(baseline_device_info), encoding="utf-8")
            (bdir / "packages_inventory.json").write_text(json.dumps(baseline_inventory), encoding="utf-8")
            (bdir / "settings_snapshot.json").write_text(json.dumps(baseline_settings), encoding="utf-8")
            (bdir / "apk_manifest.json").write_text(json.dumps(baseline_manifest), encoding="utf-8")
            (bdir / "changelog.json").write_text(json.dumps({"modifications": []}), encoding="utf-8")
            (bdir / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")

            plan_path = Path(tmp) / "approved_plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "approved": True,
                        "packages": [
                            {"package_name": "com.miui.msa.global", "action": "disable_user"},
                            {"package_name": "com.mipay.wallet.in", "action": "disable_user"},
                            {"package_name": "com.nonexistent.absent", "action": "disable_user"},
                        ],
                        "settings": [
                            {"namespace": "global", "key": "personalized_ad_enabled", "new_value": "0"},
                        ],
                        "appops": [
                            {"package_name": "com.miui.msa.global", "op": "POST_NOTIFICATION", "new_mode": "ignore", "old_mode": "allow"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            # 1) Apply approved plan
            with (
                mock.patch.object(apply_changes, "find_adb", return_value="adb"),
                mock.patch.object(apply_changes, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(apply_changes, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["apply_changes.py", "--plan", str(plan_path), "--backup-dir", str(bdir)]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                apply_changes.main()

            self.assertFalse(dev.packages["com.miui.msa.global"]["enabled"])
            self.assertFalse(dev.packages["com.mipay.wallet.in"]["enabled"])
            self.assertEqual(dev.settings[("global", "personalized_ad_enabled")], "0")
            self.assertEqual(dev.appops[("com.miui.msa.global", "POST_NOTIFICATION")], "ignore")
            self.assertTrue((bdir / "changelog.json").exists())
            self.assertTrue(list(bdir.glob("execution_report_*.json")))

            # 2) Run verify_device (mocking restore.py --dry-run subprocess to invoke our in-process restore --dry-run)
            def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> mock.MagicMock:
                if any("restore.py" in str(part) for part in cmd):
                    out_buf = io.StringIO()
                    code = 0
                    with (
                        mock.patch.object(restore, "find_adb", return_value="adb"),
                        mock.patch.object(restore, "resolve_device_serial", return_value="MOCK123"),
                        mock.patch.object(restore, "run_adb", side_effect=dev.run_adb),
                        mock.patch.object(sys, "argv", ["restore.py", "--dry-run", "--backup-dir", str(bdir), "--serial", "MOCK123"]),
                        redirect_stdout(out_buf),
                        redirect_stderr(io.StringIO()),
                    ):
                        try:
                            restore.main()
                        except SystemExit as exc:
                            code = int(exc.code or 0)
                    return mock.MagicMock(returncode=code, stdout=out_buf.getvalue(), stderr="")
                return mock.MagicMock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(verify_device, "find_adb", return_value="adb"),
                mock.patch.object(verify_device, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(verify_device, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(verify_device.subprocess, "run", side_effect=fake_subprocess_run),
                mock.patch.object(sys, "argv", ["verify_device.py", "--backup-dir", str(bdir)]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    verify_device.main()
                self.assertEqual(ctx.exception.code, 0)

            ver_reports = sorted(bdir.glob("verification_report_*.json"))
            self.assertTrue(ver_reports)
            vr_data = json.loads(ver_reports[-1].read_text(encoding="utf-8"))
            for key in (
                "detected_applications",
                "removed_applications",
                "disabled_applications",
                "skipped_applications",
                "modified_advertisement_settings",
                "backup_completeness",
                "restore_readiness",
                "failed_operations",
                "verification_results",
            ):
                self.assertIn(key, vr_data)
            self.assertEqual(vr_data["verification_results"]["overall_status"], "PASS")
            self.assertIn("com.miui.msa.global", vr_data["disabled_applications"])
            self.assertTrue(vr_data["skipped_applications"])

            # 3) Snapshot original backup hash so we prove restore does not alter baseline files
            orig_dev_info_bytes = (bdir / "device_info.json").read_bytes()
            orig_manifest_bytes = (bdir / "apk_manifest.json").read_bytes()

            # 4) Execute live restore with live post-restore verification enabled
            restore_out = io.StringIO()
            restore_err = io.StringIO()
            with (
                mock.patch.object(restore, "find_adb", return_value="adb"),
                mock.patch.object(restore, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(restore, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["restore.py", "--backup-dir", str(bdir), "--yes", "--scope", "applied"]),
                redirect_stdout(restore_out),
                redirect_stderr(restore_err),
            ):
                try:
                    restore.main()
                except SystemExit as exc:
                    reps = sorted(r for r in bdir.glob("restoration_report_*.json") if not r.name.endswith("_dryrun.json"))
                    rep_txt = reps[-1].read_text(encoding="utf-8") if reps else ""
                    self.fail(f"restore.main() exited {exc.code}: {restore_out.getvalue()} {restore_err.getvalue()} | {rep_txt}")

            self.assertTrue(dev.packages["com.miui.msa.global"]["enabled"])
            self.assertTrue(dev.packages["com.mipay.wallet.in"]["enabled"])
            self.assertEqual(dev.settings[("global", "personalized_ad_enabled")], "1")
            self.assertEqual(dev.appops[("com.miui.msa.global", "POST_NOTIFICATION")], "allow")
            self.assertEqual((bdir / "device_info.json").read_bytes(), orig_dev_info_bytes)
            self.assertEqual((bdir / "apk_manifest.json").read_bytes(), orig_manifest_bytes)

            live_reports = sorted(r for r in bdir.glob("restoration_report_*.json") if not r.name.endswith("_dryrun.json"))
            self.assertTrue(live_reports)
            live_data = json.loads(live_reports[-1].read_text(encoding="utf-8"))
            self.assertEqual(live_data["overall_status"], "success")
            self.assertFalse(live_data["dry_run"])
            self.assertIn(
                "com.miui.msa.global",
                [item.get("package_name") if isinstance(item, dict) else item for item in live_data["restored_states_ok"]],
            )

            # 5) Simulate silent ROM ignore on setting write -> post-restore verification MUST catch mismatch and exit 1
            dev.settings[("global", "personalized_ad_enabled")] = "0"
            dev.silent_ignore_settings = True
            with (
                mock.patch.object(restore, "find_adb", return_value="adb"),
                mock.patch.object(restore, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(restore, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["restore.py", "--backup-dir", str(bdir), "--yes", "--scope", "applied"]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    restore.main()
                self.assertEqual(ctx.exception.code, 1)

    def test_apk_backed_non_system_and_system_uninstall_restore_e2e(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bdir = Path(tmp) / "backup_uninstall"
            bdir.mkdir(parents=True)
            dev = _FakeAndroidDevice()

            apk_path = bdir / "apks" / "com.mipay.wallet.in" / "base.apk"
            mipay_hash = _make_valid_apk(apk_path)

            baseline_device_info = {
                "brand": "Xiaomi",
                "manufacturer": "Xiaomi",
                "model": "M2101K7BI",
                "device": "rosemary",
                "android_version": "13",
                "miui_ui_version": "V14",
                "rom_incremental": "V14.0.11.0.TKLINXM",
                "serial": "MOCK123",
            }
            baseline_inventory = [
                {
                    "package_name": pkg,
                    "category": "system_app" if info["system"] else "third_party",
                    "is_system": info["system"],
                    "installed_user0": True,
                    "enabled": info["enabled"],
                    "enabled_code_user0": 0,
                }
                for pkg, info in dev.packages.items()
            ]
            baseline_manifest = {
                "backed_up_packages": {
                    "com.mipay.wallet.in": {
                        "files": [{"local_rel_path": "apks/com.mipay.wallet.in/base.apk", "sha256": mipay_hash}]
                    }
                }
            }
            (bdir / "device_info.json").write_text(json.dumps(baseline_device_info), encoding="utf-8")
            (bdir / "packages_inventory.json").write_text(json.dumps(baseline_inventory), encoding="utf-8")
            (bdir / "settings_snapshot.json").write_text(json.dumps({"global": {}, "system": {}, "secure": {}}), encoding="utf-8")
            (bdir / "apk_manifest.json").write_text(json.dumps(baseline_manifest), encoding="utf-8")
            (bdir / "changelog.json").write_text(json.dumps({"modifications": []}), encoding="utf-8")
            (bdir / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")

            plan_path = Path(tmp) / "uninstall_plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "approved": True,
                        "packages": [
                            {"package_name": "com.miui.msa.global", "action": "uninstall_user0"},
                            {"package_name": "com.mipay.wallet.in", "action": "uninstall_user0"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.object(apply_changes, "find_adb", return_value="adb"),
                mock.patch.object(apply_changes, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(apply_changes, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["apply_changes.py", "--plan", str(plan_path), "--backup-dir", str(bdir)]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                apply_changes.main()

            self.assertNotIn("com.miui.msa.global", dev.packages)
            self.assertNotIn("com.mipay.wallet.in", dev.packages)

            # Restore both: system via install-existing, non-system via local APK install
            with (
                mock.patch.object(restore, "find_adb", return_value="adb"),
                mock.patch.object(restore, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(restore, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["restore.py", "--backup-dir", str(bdir), "--yes", "--scope", "applied"]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                restore.main()

            self.assertIn("com.miui.msa.global", dev.packages)
            self.assertIn("com.mipay.wallet.in", dev.packages)
            reps = sorted(r for r in bdir.glob("restoration_report_*.json") if not r.name.endswith("_dryrun.json"))
            rep = json.loads(reps[-1].read_text(encoding="utf-8"))
            mechs = {r["package_name"]: r["restoration_mechanism"] for r in rep["restored_packages_ok"]}
            self.assertEqual(mechs["com.miui.msa.global"], "system_install_existing")
            self.assertEqual(mechs["com.mipay.wallet.in"], "local_apk_install")

            # Verify INSTALL_FAILED_USER_RESTRICTED surfaces cleanly as non-zero partial_failure with Install via USB hint
            dev.packages.pop("com.mipay.wallet.in", None)
            dev.install_restricted = True
            with (
                mock.patch.object(restore, "find_adb", return_value="adb"),
                mock.patch.object(restore, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(restore, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["restore.py", "--backup-dir", str(bdir), "--yes", "--scope", "applied"]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    restore.main()
                self.assertEqual(ctx.exception.code, 1)
            reps2 = sorted(r for r in bdir.glob("restoration_report_*.json") if not r.name.endswith("_dryrun.json"))
            rep2 = json.loads(reps2[-1].read_text(encoding="utf-8"))
            self.assertEqual(rep2["restored_packages_failed"][0]["package_name"], "com.mipay.wallet.in")
            self.assertIn("Install via USB", rep2["restored_packages_failed"][0]["message"])

    def test_interactive_restore_corrupt_apks_and_project_dir_and_report_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            bdir = project_dir / "backups" / "20261111_000001_M2101K7BI"
            bdir.mkdir(parents=True)
            (project_dir / "backups" / "LATEST_BACKUP.txt").write_text("backups/20261111_000001_M2101K7BI\n", encoding="utf-8")

            dev = _FakeAndroidDevice()
            apk_path = bdir / "apks" / "com.mipay.wallet.in" / "base.apk"
            good_hash = _make_valid_apk(apk_path)

            baseline_device_info = {
                "brand": "Xiaomi",
                "manufacturer": "Xiaomi",
                "model": "M2101K7BI",
                "device": "rosemary",
                "android_version": "13",
                "serial": "MOCK123",
            }
            baseline_inventory = [
                {
                    "package_name": pkg,
                    "category": "system_app" if info["system"] else "third_party",
                    "is_system": info["system"],
                    "installed_user0": True,
                    "enabled": info["enabled"],
                    "enabled_code_user0": 0,
                }
                for pkg, info in dev.packages.items()
            ]
            baseline_manifest = {
                "backed_up_packages": {
                    "com.mipay.wallet.in": {
                        "files": [{"local_rel_path": "apks/com.mipay.wallet.in/base.apk", "sha256": good_hash}]
                    }
                },
                "completeness_summary": {"apk_backups_verified": 1, "apk_backups_skipped_or_failed": 0},
            }
            (bdir / "device_info.json").write_text(json.dumps(baseline_device_info), encoding="utf-8")
            (bdir / "packages_inventory.json").write_text(json.dumps(baseline_inventory), encoding="utf-8")
            (bdir / "settings_snapshot.json").write_text(json.dumps({"global": {}, "system": {}, "secure": {}}), encoding="utf-8")
            (bdir / "apk_manifest.json").write_text(json.dumps(baseline_manifest), encoding="utf-8")
            (bdir / "changelog.json").write_text(json.dumps({"modifications": []}), encoding="utf-8")
            (bdir / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")

            # Apply a plan including wm_density so execution_report and verification_report align on all 9 sections & wm_density
            plan_path = project_dir / "wm_plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "approved": True,
                        "packages": [{"package_name": "com.miui.msa.global", "action": "disable_user"}],
                        "settings": [{"namespace": "global", "key": "personalized_ad_enabled", "new_value": "0"}],
                        "wm_density": "400",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(apply_changes, "find_adb", return_value="adb"),
                mock.patch.object(apply_changes, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(apply_changes, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["apply_changes.py", "--plan", str(plan_path), "--backup-dir", str(bdir)]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                apply_changes.main()

            exec_json = sorted(bdir.glob("execution_report_*.json"))[-1]
            exec_md = sorted(bdir.glob("execution_report_*.md"))[-1].read_text(encoding="utf-8")
            exec_data = json.loads(exec_json.read_text(encoding="utf-8"))
            nine_sections = (
                "detected_applications",
                "removed_applications",
                "disabled_applications",
                "skipped_applications",
                "modified_advertisement_settings",
                "backup_completeness",
                "restore_readiness",
                "failed_operations",
                "verification_results",
            )
            for key in nine_sections:
                self.assertIn(key, exec_data)
            for idx in range(1, 10):
                self.assertIn(f"## {idx}. ", exec_md)

            def fake_sub(cmd: list[str], **kwargs: Any) -> mock.MagicMock:
                return mock.MagicMock(returncode=0, stdout="", stderr="")

            with (
                mock.patch.object(verify_device, "find_adb", return_value="adb"),
                mock.patch.object(verify_device, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(verify_device, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(verify_device.subprocess, "run", side_effect=fake_sub),
                mock.patch.object(sys, "argv", ["verify_device.py", "--backup-dir", str(bdir)]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as ctx_v:
                    verify_device.main()
                self.assertEqual(ctx_v.exception.code, 0)

            ver_json = sorted(bdir.glob("verification_report_*.json"))[-1]
            ver_md = sorted(bdir.glob("verification_report_*.md"))[-1].read_text(encoding="utf-8")
            ver_data = json.loads(ver_json.read_text(encoding="utf-8"))
            for key in nine_sections:
                self.assertIn(key, ver_data)
            for idx in range(1, 10):
                self.assertIn(f"## {idx}. ", ver_md)
            self.assertEqual(
                len(exec_data["modified_advertisement_settings"]),
                len(ver_data["modified_advertisement_settings"]),
            )
            self.assertTrue(any(m.get("type") == "wm_density" for m in ver_data["modified_advertisement_settings"]))

            # Verify --project-dir resolves custom backup root on restore.py --dry-run
            with (
                mock.patch.object(restore, "find_adb", return_value="adb"),
                mock.patch.object(restore, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(restore, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["restore.py", "--project-dir", str(project_dir), "--dry-run"]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                restore.main()

            # Reset changelog so zero restore targets remain, tamper APK, and verify interactive restore reports integrity failure instead of 'Nothing to restore.'
            (bdir / "changelog.json").write_text(json.dumps({"modifications": []}), encoding="utf-8")
            dev.packages["com.miui.msa.global"]["enabled"] = True
            dev.settings[("global", "personalized_ad_enabled")] = "1"
            apk_path.write_bytes(b"TAMPERED")
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            with (
                mock.patch.object(restore, "find_adb", return_value="adb"),
                mock.patch.object(restore, "resolve_device_serial", return_value="MOCK123"),
                mock.patch.object(restore, "run_adb", side_effect=dev.run_adb),
                mock.patch.object(sys, "argv", ["restore.py", "--project-dir", str(project_dir)]),
                redirect_stdout(out_buf),
                redirect_stderr(err_buf),
            ):
                with self.assertRaises(SystemExit) as ctx_r:
                    restore.main()
                self.assertEqual(ctx_r.exception.code, 1)
            combined_interactive = out_buf.getvalue() + err_buf.getvalue()
            self.assertNotIn("Nothing to restore.", combined_interactive)
            self.assertIn("INTEGRITY FAILURE", combined_interactive.upper())


class TestWrapperScripts(unittest.TestCase):
    def test_wrappers_forward_project_dir_to_real_scripts_and_fix_help_messaging(self) -> None:
        import os
        import shutil
        import subprocess
        import textwrap

        fake_adb_py = textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import io
            import re
            import sys
            import zipfile
            from pathlib import Path

            PROPS = {
                "ro.product.model": "M2101K7BI",
                "ro.product.brand": "Xiaomi",
                "ro.product.manufacturer": "Xiaomi",
                "ro.product.device": "rosemary",
                "ro.build.version.release": "13",
                "ro.build.version.sdk": "33",
                "ro.miui.ui.version.name": "V14",
                "ro.build.version.incremental": "V14.0.11.0.TKLINXM",
                "ro.mi.os.version.name": "",
                "ro.build.fingerprint": "Redmi/rosemary_in/rosemary:13/TP1A.220624.014/V14.0.11.0.TKLINXM:user/release-keys",
            }
            PKGS = {
                "com.miui.home": {"path": "/system/priv-app/MiuiHome/MiuiHome.apk", "system": True},
                "com.google.android.inputmethod.latin": {"path": "/data/app/gboard/base.apk", "system": False},
                "com.example.userapp": {"path": "/data/app/userapp/base.apk", "system": False},
            }

            args = sys.argv[1:]
            if len(args) >= 2 and args[0] == "-s":
                args = args[2:]

            if args[:1] == ["devices"]:
                print("List of devices attached")
                print("SER_WRAPPER\\tdevice product:rosemary_in model:M2101K7BI device:rosemary")
                sys.exit(0)
            if args[:1] == ["get-state"]:
                print("device")
                sys.exit(0)
            if args[:1] == ["pull"] and len(args) >= 3:
                dest = Path(args[2])
                dest.parent.mkdir(parents=True, exist_ok=True)
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as zf:
                    zf.writestr("AndroidManifest.xml", b"manifest")
                    zf.writestr("classes.dex", b"dex\\n035\\x00")
                dest.write_bytes(buf.getvalue())
                print("1 file pulled.")
                sys.exit(0)
            if args[:1] == ["shell"]:
                sh_joined = " ".join(args[1:])
                if "$(getprop " in sh_joined:
                    for m in re.finditer(r"echo '([^=]+)='\\$\\(getprop ([^)]+)\\)", sh_joined):
                        print(f"{m.group(1)}={PROPS.get(m.group(2), '')}")
                    sys.exit(0)
                if sh_joined.startswith("getprop"):
                    parts = sh_joined.split()
                    if len(parts) == 2:
                        print(PROPS.get(parts[1], ""))
                    else:
                        for k, v in PROPS.items():
                            print(f"[{k}]: [{v}]")
                    sys.exit(0)
                if "resolve-activity" in sh_joined:
                    print("com.miui.home/.launcher.Launcher")
                elif "pm list packages" in sh_joined:
                    show_s = " -s" in f" {sh_joined} "
                    show_3 = " -3" in f" {sh_joined} "
                    show_d = " -d" in f" {sh_joined} "
                    show_f = " -f" in f" {sh_joined} "
                    if not show_d:
                        for p, info in sorted(PKGS.items()):
                            if show_s and not info["system"]:
                                continue
                            if show_3 and info["system"]:
                                continue
                            print(f"package:{info['path']}={p}" if show_f else f"package:{p}")
                elif sh_joined.startswith("pm path "):
                    pkg = sh_joined.split()[2]
                    if pkg in PKGS:
                        print(f"package:{PKGS[pkg]['path']}")
                    else:
                        sys.exit(1)
                elif sh_joined.startswith("pm list users"):
                    print("Users:\\n\\tUserInfo{0:Owner:4c13} running")
                elif sh_joined.startswith("stat -c"):
                    print("1024")
                elif sh_joined == "dumpsys package packages":
                    print("Packages:")
                    for p, info in sorted(PKGS.items()):
                        flags = "SYSTEM" if info["system"] else ""
                        code_dir = Path(info["path"]).parent.as_posix()
                        print(f"  Package [{p}] (deadbeef):")
                        print(f"    codePath={code_dir}")
                        print("    versionCode=100 minSdk=29 targetSdk=33")
                        print("    versionName=1.0")
                        print(f"    pkgFlags=[ {flags} ]")
                        print("    User 0: ceDataInode=1 installed=true hidden=false enabled=0")
                elif sh_joined.startswith("dumpsys package "):
                    print("    User 0: ceDataInode=1 installed=true hidden=false enabled=0")
                elif sh_joined == "settings list global":
                    print("personalized_ad_enabled=1")
                elif sh_joined == "settings list secure":
                    print("default_input_method=com.google.android.inputmethod.latin/.LatinIME")
                elif sh_joined == "settings list system":
                    print("font_scale=1.0")
                elif sh_joined.startswith("wm size"):
                    print("Physical size: 1080x2400")
                elif sh_joined.startswith("wm density"):
                    print("Physical density: 440")
                elif sh_joined.startswith("df "):
                    print("Filesystem Size Used Avail Use% Mounted on\\n/dev/block/dm-5 100G 40G 60G 40% /data")
                elif "appops get" in sh_joined:
                    print("POST_NOTIFICATION: allow")
            sys.exit(0)
            """
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / "repo"
            caller_dir = tmp_path / "caller workdir"
            fake_bin = repo_dir / "fake_bin"
            repo_dir.mkdir()
            caller_dir.mkdir()
            fake_bin.mkdir()

            shutil.copytree(REPO_ROOT / "scripts", repo_dir / "scripts")
            shutil.copytree(REPO_ROOT / "profiles", repo_dir / "profiles")
            for fname in ("OneClick_Backup.bat", "OneClick_Restore.bat", "oneclick_backup.sh", "oneclick_restore.sh"):
                shutil.copy2(REPO_ROOT / fname, repo_dir / fname)

            adb_py = fake_bin / "adb.py"
            adb_py.write_text(fake_adb_py, encoding="utf-8", newline="\n")
            adb_posix = fake_bin / "adb"
            adb_posix.write_text(fake_adb_py, encoding="utf-8", newline="\n")
            adb_bat = fake_bin / "adb.bat"
            adb_bat.write_text(f'@echo off\r\n"{sys.executable}" "%~dp0adb.py" %*\r\n', encoding="utf-8")

            if sys.platform == "win32":
                env = os.environ.copy()
                env["CLEAN_MIUI_NO_PAUSE"] = "1"
                env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

                bat_path = repo_dir / "OneClick_Backup.bat"
                res_b = subprocess.run(
                    ["cmd", "/c", str(bat_path), "--serial", "SER_WRAPPER", "--project-dir", "proj with spaces"],
                    cwd=caller_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(res_b.returncode, 0, msg=res_b.stdout + res_b.stderr)
                spaced_root = caller_dir / "proj with spaces"
                ptr_file = spaced_root / "backups" / "LATEST_BACKUP.txt"
                self.assertTrue(ptr_file.is_file(), msg=res_b.stdout + res_b.stderr)
                bdir = spaced_root / ptr_file.read_text(encoding="utf-8").strip()
                self.assertTrue((bdir / "summary.json").is_file())
                self.assertTrue((bdir / "apks" / "com.example.userapp" / "base.apk").is_file())
                self.assertTrue(any(bdir.glob("restoration_report_*_dryrun.md")))
                self.assertIn("[OK] Backup created and dry-run restore verified", res_b.stdout)
                self.assertIn("Restoration status: dry_run_ready", res_b.stdout)
                self.assertFalse((repo_dir / "backups").exists())

                res_eq = subprocess.run(
                    ["cmd", "/c", str(bat_path), "--project-dir=eq dir with spaces", "--skip-apk-pull"],
                    cwd=caller_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(res_eq.returncode, 0, msg=res_eq.stdout + res_eq.stderr)
                self.assertTrue((caller_dir / "eq dir with spaces" / "backups" / "LATEST_BACKUP.txt").is_file())

                res_empty = subprocess.run(
                    ["cmd", "/c", str(bat_path), "--project-dir", ""],
                    cwd=caller_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res_empty.returncode, 0)

                res_ws = subprocess.run(
                    ["cmd", "/c", str(bat_path), "--project-dir", "   "],
                    cwd=caller_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res_ws.returncode, 0)

                res_missing = subprocess.run(
                    ["cmd", "/c", str(bat_path), "--project-dir"],
                    cwd=caller_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res_missing.returncode, 0)

                res_b_help = subprocess.run(
                    ["cmd", "/c", str(bat_path), "--help"],
                    cwd=caller_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(res_b_help.returncode, 0, msg=res_b_help.stdout + res_b_help.stderr)
                self.assertIn("--project-dir", res_b_help.stdout)
                self.assertNotIn("One-Click Backup & Restore Verification", res_b_help.stdout)

                res_r_help = subprocess.run(
                    ["cmd", "/c", str(repo_dir / "OneClick_Restore.bat"), "--help"],
                    cwd=caller_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(res_r_help.returncode, 0, msg=res_r_help.stdout + res_r_help.stderr)
                self.assertIn("--project-dir", res_r_help.stdout)
                self.assertNotIn("[DONE] Restore script completed", res_r_help.stdout)

                res_bad_flag = subprocess.run(
                    ["cmd", "/c", str(bat_path), "--unknown-flag-xyz"],
                    cwd=caller_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res_bad_flag.returncode, 0)

            bash_exe = shutil.which("bash")
            if bash_exe:
                sh_res = subprocess.run(
                    [
                        bash_exe,
                        "-c",
                        'chmod +x ../repo/oneclick_backup.sh ../repo/oneclick_restore.sh ../repo/fake_bin/adb && '
                        'PATH="$PWD/../repo/fake_bin:$PATH" ../repo/oneclick_backup.sh '
                        '--serial=SER_WRAPPER --project-dir="sh spaced proj"',
                    ],
                    cwd=caller_dir,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(sh_res.returncode, 0, msg=sh_res.stdout + sh_res.stderr)
                sh_proj = caller_dir / "sh spaced proj"
                sh_ptr = sh_proj / "backups" / "LATEST_BACKUP.txt"
                self.assertTrue(sh_ptr.is_file(), msg=sh_res.stdout + sh_res.stderr)
                sh_bdir = sh_proj / sh_ptr.read_text(encoding="utf-8").strip()
                self.assertTrue((sh_bdir / "summary.json").is_file())
                self.assertTrue((sh_bdir / "apks" / "com.example.userapp" / "base.apk").is_file())
                self.assertTrue(any(sh_bdir.glob("restoration_report_*_dryrun.md")))
                self.assertIn("[OK] Backup created and dry-run restore verified", sh_res.stdout)
                self.assertIn("Restoration status: dry_run_ready", sh_res.stdout)
                self.assertFalse((repo_dir / "backups").exists())

                sh_sep = subprocess.run(
                    [
                        bash_exe,
                        "-c",
                        'PATH="$PWD/../repo/fake_bin:$PATH" ../repo/oneclick_backup.sh '
                        '--serial SER_WRAPPER --project-dir "sh spaced sep" --skip-apk-pull',
                    ],
                    cwd=caller_dir,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(sh_sep.returncode, 0, msg=sh_sep.stdout + sh_sep.stderr)
                self.assertTrue((caller_dir / "sh spaced sep" / "backups" / "LATEST_BACKUP.txt").is_file())
                self.assertFalse((repo_dir / "backups").exists())

                sh_empty = subprocess.run(
                    [bash_exe, "-c", '../repo/oneclick_backup.sh --project-dir ""'],
                    cwd=caller_dir,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(sh_empty.returncode, 0)

                sh_ws = subprocess.run(
                    [bash_exe, "-c", '../repo/oneclick_backup.sh --project-dir "   "'],
                    cwd=caller_dir,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(sh_ws.returncode, 0)

                sh_help = subprocess.run(
                    [bash_exe, "-c", "../repo/oneclick_backup.sh --help"],
                    cwd=caller_dir,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(sh_help.returncode, 0, msg=sh_help.stdout + sh_help.stderr)
                self.assertIn("--project-dir", sh_help.stdout)
                self.assertNotIn("One-Click Backup & Restore Verification", sh_help.stdout)


if __name__ == "__main__":
    unittest.main()
