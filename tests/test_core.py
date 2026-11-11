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


if __name__ == "__main__":
    unittest.main()
