"""Phase 4-5 Inventory Analyzer and Profile-Aware Proposal Generator.

Inspects the latest backup's package inventory and settings, matches an optional
device profile preset, verifies safety prerequisites (like alternative keyboard
or file manager before replacing stock OEM apps), and writes a draft proposal JSON
with `approved: false`. Never modifies the device.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from safety import evaluate_profile_compatibility, is_protected_package, resolve_backup_dir


def evaluate_profile_candidates(
    inventory: list[dict[str, Any]],
    profile: dict[str, Any],
    settings_snapshot: dict[str, dict[str, str]],
    ad_audit: dict[str, Any],
    include_tiers: tuple[str, ...],
    apk_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Match profile rules against installed packages and evaluate prerequisites safely."""
    by_pkg = {row["package_name"]: row for row in inventory}
    installed_u0 = {pkg for pkg, row in by_pkg.items() if row.get("installed_user0")}
    backed_pkgs = set((apk_manifest or {}).get("backed_up_packages", {}).keys())

    default_ime_raw = settings_snapshot.get("secure", {}).get("default_input_method", "")
    active_ime_pkg = default_ime_raw.split("/", 1)[0] if "/" in default_ime_raw else ""

    proposed_packages: list[dict[str, Any]] = []
    skipped_prerequisites: list[dict[str, Any]] = []
    blocked_guardrails: list[dict[str, Any]] = []
    backup_warnings: list[str] = []

    for tier_name in include_tiers:
        for rule in profile.get(tier_name, []):
            pkg = rule["package_name"]
            row = by_pkg.get(pkg)
            if not row or not row.get("installed_user0"):
                continue

            # Skip if already disabled on baseline and rule is disable_user
            if rule["action"] == "disable_user" and row.get("enabled_code_user0") in (2, 3):
                continue

            protected, reason = is_protected_package(pkg, dynamic_protected={active_ime_pkg} if active_ime_pkg else None)
            if protected:
                blocked_guardrails.append({"package_name": pkg, "reason": reason})
                continue

            reqs = rule.get("requires_any_of", [])
            if reqs and not any((req in installed_u0 and by_pkg[req].get("enabled_code_user0") not in (2, 3)) for req in reqs):
                skipped_prerequisites.append(
                    {
                        "package_name": pkg,
                        "app_name": rule.get("app_name") or row.get("app_label") or pkg,
                        "tier": tier_name,
                        "missing_alternative_from": reqs,
                        "note": rule.get("prerequisite_note", "Install an alternative before removing."),
                    }
                )
                continue

            effective_action = rule["action"]
            is_system = bool(row.get("is_system"))
            downgraded_from: str | None = None
            if effective_action == "disable_user":
                recovery_source = "pm_enable_state"
                recovery_method = "pm enable / pm default-state"
            elif is_system:
                recovery_source = "system_partition"
                recovery_method = "cmd package install-existing --user 0 (system partition)"
            elif apk_manifest is None or pkg in backed_pkgs:
                recovery_source = "local_apk_backup"
                recovery_method = "adb install (verified local APK backup; MIUI requires 'Install via USB')"
            else:
                downgraded_from = effective_action
                effective_action = "disable_user"
                recovery_source = "pm_enable_state"
                recovery_method = "pm enable (downgraded from uninstall_user0: local APK not backed up)"
                backup_warnings.append(
                    f"Downgraded '{pkg}' from uninstall_user0 to disable_user because no local APK backup was recorded."
                )

            item: dict[str, Any] = {
                "package_name": pkg,
                "app_name": rule.get("app_name") or row.get("app_label") or pkg,
                "purpose": rule.get("purpose", ""),
                "why_unnecessary": rule.get("why_unnecessary", ""),
                "action": effective_action,
                "tier": tier_name,
                "risk": rule.get("risk", "review"),
                "is_system": is_system,
                "has_verified_apk_backup": pkg in backed_pkgs if apk_manifest is not None else None,
                "recovery_available": True,
                "recovery_source": recovery_source,
                "recovery_method": recovery_method,
            }
            if downgraded_from:
                item["downgraded_from"] = downgraded_from
            proposed_packages.append(item)

    proposed_appops: list[dict[str, Any]] = []
    for op_rule in profile.get("appops_restrictions", []):
        pkg = op_rule["package_name"]
        row = by_pkg.get(pkg)
        if not row or not row.get("installed_user0"):
            continue
        existing_appops = ad_audit.get("key_package_audit", {}).get(pkg, {}).get("appops", "")
        op_name = op_rule["op"]
        if f"{op_name}: {op_rule['new_mode']}" in existing_appops:
            continue
        old_mode = "default"
        for line in existing_appops.splitlines():
            if line.strip().startswith(f"{op_name}:"):
                old_mode = line.split(":", 1)[1].strip().split()[0]
                break
        if old_mode == op_rule["new_mode"]:
            continue
        proposed_appops.append(
            {
                "package_name": pkg,
                "op": op_name,
                "old_mode": old_mode,
                "new_mode": op_rule["new_mode"],
                "purpose": op_rule.get("purpose", ""),
            }
        )

    return {
        "proposed_packages": proposed_packages,
        "skipped_prerequisites": skipped_prerequisites,
        "blocked_guardrails": blocked_guardrails,
        "proposed_appops": proposed_appops,
        "backup_warnings": backup_warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze backup inventory & optionally draft an unapproved proposal plan")
    parser.add_argument("--backup-dir", help="Explicit backup dir")
    parser.add_argument("--profile", help="Path to JSON cleanup profile (e.g., profiles/xiaomi_miui_hyperos_clean.json)")
    parser.add_argument(
        "--tier",
        choices=("tier_a_only", "tier_a_and_b"),
        default="tier_a_and_b",
        help="Include only Tier A (safe ad/bloat removal) or Tier A + Tier B (stock Google replacement style)",
    )
    parser.add_argument(
        "--include-optional",
        action="store_true",
        help="Also include profile optional_user_preferences entries (e.g., non-bloat Google apps user may choose)",
    )
    parser.add_argument(
        "--allow-untested-device",
        action="store_true",
        help="Allow running a profile on an untested hardware/ROM variant or OEM-mismatched device",
    )
    parser.add_argument("--emit-draft-plan", help="Write unapproved draft JSON plan (`approved: false`) to this file")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent.parent
    backup_dir = resolve_backup_dir(project_dir, args.backup_dir)

    inventory: list[dict[str, Any]] = json.loads((backup_dir / "packages_inventory.json").read_text(encoding="utf-8"))
    ad_audit: dict[str, Any] = json.loads((backup_dir / "ad_tracking_audit.json").read_text(encoding="utf-8"))
    apk_manifest: dict[str, Any] = json.loads((backup_dir / "apk_manifest.json").read_text(encoding="utf-8"))
    settings_snapshot: dict[str, dict[str, str]] = json.loads((backup_dir / "settings_snapshot.json").read_text(encoding="utf-8"))
    device_info: dict[str, Any] = json.loads((backup_dir / "device_info.json").read_text(encoding="utf-8"))

    by_cat: dict[str, list[dict[str, Any]]] = {}
    for row in inventory:
        by_cat.setdefault(row["category"], []).append(row)

    print(f"BACKUP_DIR: {backup_dir}")
    print(f"DEVICE: {device_info.get('brand')} {device_info.get('model')} | Android {device_info.get('android_version')} | ROM {device_info.get('rom_incremental')}")
    print(f"UI_METRICS: {device_info.get('wm_size')} | {device_info.get('wm_density')} | ro.sf.lcd_density={device_info.get('ro_sf_lcd_density')} | font_scale={settings_snapshot.get('system', {}).get('font_scale')}")
    completeness = apk_manifest.get("completeness_summary", {})
    print(
        f"TOTAL_PACKAGES: {len(inventory)} | APK_BACKUPS_VERIFIED: {len(apk_manifest.get('backed_up_packages', {}))} "
        f"| APK_SKIPPED_OR_FAILED: {len(apk_manifest.get('skipped_or_failed', {}))} "
        f"| EXCLUDED_BY_POLICY: {completeness.get('apk_excluded_by_policy', '?')}"
    )
    for cat, rows in sorted(by_cat.items()):
        inst = sum(1 for r in rows if r.get("installed_user0"))
        dis = sum(1 for r in rows if r.get("enabled_code_user0") in (2, 3))
        print(f"  - {cat}: total={len(rows)}, installed_u0={inst}, disabled_u0={dis}")

    if args.profile:
        profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
        compat = evaluate_profile_compatibility(device_info, profile)
        print(f"\n=== COMPATIBILITY CHECK ({profile.get('profile_name')}) ===")
        print(f"  compatible={compat['compatible']} | hardware_tested={compat['tested']} | {compat['reason']}")
        for w in compat.get("warnings", []):
            print(f"  [COMPATIBILITY WARNING] {w}")

        if not compat["compatible"] and not args.allow_untested_device:
            print(
                "[COMPATIBILITY BLOCKED] Refusing to apply OEM-mismatched profile without --allow-untested-device. "
                "Use profiles/generic_android_audit.json for non-Xiaomi devices."
            )
            raise SystemExit(2)
        if not compat["tested"] and compat.get("requires_untested_opt_in") and not args.allow_untested_device:
            print(
                "[COMPATIBILITY BLOCKED] Connected device is UNTESTED against this profile's verified hardware "
                "baseline. Review the warnings above and re-run with --allow-untested-device only if intended."
            )
            raise SystemExit(2)

        tier_list = ["tier_a_safe_bloat_and_ads"]
        if args.tier == "tier_a_and_b":
            tier_list.append("tier_b_google_stock_replacement")
        if args.include_optional:
            tier_list.append("optional_user_preferences")

        evaluated = evaluate_profile_candidates(
            inventory,
            profile,
            settings_snapshot,
            ad_audit,
            include_tiers=tuple(tier_list),
            apk_manifest=apk_manifest,
        )
        print(f"\n=== PROFILE MATCH ({profile.get('profile_name')}, {args.tier}) ===")
        print(f"Proposed package actions: {len(evaluated['proposed_packages'])}")
        for p in evaluated["proposed_packages"]:
            extra = f" (downgraded from {p['downgraded_from']})" if p.get("downgraded_from") else ""
            print(
                f"  - {p['package_name']} ({p['app_name']}) | {p['action']}{extra} | "
                f"recovery={p['recovery_source']} | risk={p['risk']} | {p['purpose']}"
            )
        print(f"Proposed AppOps restrictions: {len(evaluated['proposed_appops'])}")
        for o in evaluated["proposed_appops"]:
            print(f"  - {o['package_name']} {o['op']}: {o['old_mode']} -> {o['new_mode']}")
        if evaluated["skipped_prerequisites"]:
            print(f"Skipped due to missing replacement app ({len(evaluated['skipped_prerequisites'])}):")
            for s in evaluated["skipped_prerequisites"]:
                print(f"  - {s['package_name']} ({s['app_name']}): {s['note']} (needs one of {s['missing_alternative_from']})")
        for bw in evaluated.get("backup_warnings", []):
            print(f"  [BACKUP WARNING] {bw}")

        if args.emit_draft_plan:
            draft = {
                "approved": False,
                "approved_by": None,
                "profile_used": profile.get("profile_name"),
                "tier": args.tier,
                "target_device": {
                    "brand": device_info.get("brand"),
                    "manufacturer": device_info.get("manufacturer"),
                    "model": device_info.get("model"),
                    "device": device_info.get("device"),
                    "android_version": device_info.get("android_version"),
                },
                "compatibility": compat,
                "packages": [
                    {
                        "package_name": p["package_name"],
                        "action": p["action"],
                        "recovery_source": p["recovery_source"],
                    }
                    for p in evaluated["proposed_packages"]
                ],
                "settings": [],
                "appops": [
                    {
                        "package_name": o["package_name"],
                        "op": o["op"],
                        "old_mode": o["old_mode"],
                        "new_mode": o["new_mode"],
                    }
                    for o in evaluated["proposed_appops"]
                ],
                "skipped_prerequisites_review": evaluated["skipped_prerequisites"],
                "backup_warnings": evaluated.get("backup_warnings", []),
            }
            out_path = Path(args.emit_draft_plan)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(draft, indent=2), encoding="utf-8")
            print(f"\nDraft (unapproved) plan saved to: {out_path}")


if __name__ == "__main__":
    main()
