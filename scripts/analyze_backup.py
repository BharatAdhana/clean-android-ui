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

from safety import is_protected_package, resolve_backup_dir


def evaluate_profile_candidates(
    inventory: list[dict[str, Any]],
    profile: dict[str, Any],
    settings_snapshot: dict[str, dict[str, str]],
    ad_audit: dict[str, Any],
    include_tiers: tuple[str, ...],
) -> dict[str, Any]:
    """Match profile rules against installed packages and evaluate prerequisites safely."""
    by_pkg = {row["package_name"]: row for row in inventory}
    installed_u0 = {pkg for pkg, row in by_pkg.items() if row.get("installed_user0")}

    default_ime_raw = settings_snapshot.get("secure", {}).get("default_input_method", "")
    active_ime_pkg = default_ime_raw.split("/", 1)[0] if "/" in default_ime_raw else ""

    proposed_packages: list[dict[str, Any]] = []
    skipped_prerequisites: list[dict[str, Any]] = []
    blocked_guardrails: list[dict[str, Any]] = []

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

            proposed_packages.append(
                {
                    "package_name": pkg,
                    "app_name": rule.get("app_name") or row.get("app_label") or pkg,
                    "purpose": rule.get("purpose", ""),
                    "why_unnecessary": rule.get("why_unnecessary", ""),
                    "action": rule["action"],
                    "tier": tier_name,
                    "risk": rule.get("risk", "review"),
                    "recovery_available": True,
                    "recovery_method": "cmd package install-existing --user 0"
                    if row.get("is_system")
                    else "pm enable / verified local APK",
                }
            )

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
    print(f"TOTAL_PACKAGES: {len(inventory)} | APK_BACKUPS_VERIFIED: {len(apk_manifest.get('backed_up_packages', {}))}")
    for cat, rows in sorted(by_cat.items()):
        inst = sum(1 for r in rows if r.get("installed_user0"))
        dis = sum(1 for r in rows if r.get("enabled_code_user0") in (2, 3))
        print(f"  - {cat}: total={len(rows)}, installed_u0={inst}, disabled_u0={dis}")

    if args.profile:
        profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
        tiers = ("tier_a_safe_bloat_and_ads",) if args.tier == "tier_a_only" else ("tier_a_safe_bloat_and_ads", "tier_b_google_stock_replacement")
        evaluated = evaluate_profile_candidates(inventory, profile, settings_snapshot, ad_audit, include_tiers=tiers)
        print(f"\n=== PROFILE MATCH ({profile.get('profile_name')}, {args.tier}) ===")
        print(f"Proposed package actions: {len(evaluated['proposed_packages'])}")
        for p in evaluated["proposed_packages"]:
            print(f"  - {p['package_name']} ({p['app_name']}) | {p['action']} | risk={p['risk']} | {p['purpose']}")
        print(f"Proposed AppOps restrictions: {len(evaluated['proposed_appops'])}")
        for o in evaluated["proposed_appops"]:
            print(f"  - {o['package_name']} {o['op']}: {o['old_mode']} -> {o['new_mode']}")
        if evaluated["skipped_prerequisites"]:
            print(f"Skipped due to missing replacement app ({len(evaluated['skipped_prerequisites'])}):")
            for s in evaluated["skipped_prerequisites"]:
                print(f"  - {s['package_name']} ({s['app_name']}): {s['note']} (needs one of {s['missing_alternative_from']})")

        if args.emit_draft_plan:
            draft = {
                "approved": False,
                "approved_by": None,
                "profile_used": profile.get("profile_name"),
                "tier": args.tier,
                "packages": [{"package_name": p["package_name"], "action": p["action"]} for p in evaluated["proposed_packages"]],
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
            }
            out_path = Path(args.emit_draft_plan)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(draft, indent=2), encoding="utf-8")
            print(f"\nDraft (unapproved) plan saved to: {out_path}")


if __name__ == "__main__":
    main()
