# Copy-Paste AI Prompts (Clean MIUI AI)

After opening this repository in **Antigravity**, **Claude Code**, **Cursor**, **Codex**, or **Gemini CLI**, paste one of the prompts below and fill in your phone details.

---

## Prompt 1: Full Guided Clean (Recommended for Most Users)

> Use this repository (`AGENTS.md` and `skills/clean-miui-ai/SKILL.md`) to safely clean and organize my phone **without changing the OS, flashing a ROM, or rooting**.
>
> **My details:**
> - **Phone Brand & Model:** `<e.g., Redmi Note 10S / POCO X5 / OnePlus / Samsung>`
> - **OS / Region (if known):** `<e.g., MIUI 14 Global / HyperOS India / Stock Android 14>`
> - **Connection method:** `<USB cable OR Wireless ADB on same Wi-Fi>` *(Pairing/Connect IP:Port & code if wireless: `<optional>`)*
> - **Goal:** `<Choose: "Clean Stock Google style + remove bloat/ads" OR "Only remove ads & bloatware, keep Xiaomi/OEM apps">`
>
> **Instructions for AI:**
> 1. Run `python scripts/adb_helper.py doctor` (and help me pair/connect if not connected yet using `docs/CONNECTION_GUIDE.md`).
> 2. **First**, create a full backup (`scripts/inspect_and_backup.py`) and immediately verify recovery readiness (`scripts/restore.py --dry-run`) before changing anything.
> 3. Analyze the backup with the matching profile (`scripts/analyze_backup.py --emit-draft-plan plans/draft_plan.json`) and verify replacements exist before removing any OEM app.
> 4. Show me an itemized Markdown approval table and **wait for my explicit approval** before applying anything.
> 5. After I approve, apply only approved items via `scripts/apply_changes.py`, run `scripts/verify_device.py`, and walk me through `docs/UI_AND_CONTROL_CENTER_GUIDE.md`.

---

## Prompt 2: Wireless Same-Wi-Fi Connect + Cleanup

> Help me connect my phone over **Wireless debugging on the same Wi-Fi** using `scripts/adb_helper.py` and clean it using `skills/clean-miui-ai/SKILL.md`.
>
> - **Phone Model:** `<your phone model>`
> - **Pairing IP:Port & 6-digit code:** `<IP:PORT>` / `<CODE>` *(or tell me what screen to open on my phone first)*
> - **Main Wireless Connect IP:Port:** `<IP:PORT>`
> - **Cleanup style:** Clean ads + bloatware + replace duplicate OEM apps with Google stock apps (only if my replacement apps are already installed).
>
> Do One-Click Backup (`inspect_and_backup.py`) + `--dry-run` restore verification first, show me the proposed list, and wait for my approval before applying changes.

---

## Prompt 3: Minimal Safe De-Bloat (Keep Original Keyboard, Dialer, Files, Calculator)

> Inspect my phone using `skills/clean-miui-ai/SKILL.md` (`--tier tier_a_only` on Xiaomi/MIUI/HyperOS, or `profiles/generic_android_audit.json` on non-Xiaomi Android).
>
> **My phone model:** `<your phone model>`
> **Connection:** `<USB / Wireless>`
>
> Only propose **Tier A safe ad services, analytics trackers, and obvious preinstalled junk/stubs** — keep my OEM Dialer, Messages, Camera, Gallery, Calculator, Files, and Keyboard untouched.
> Back up + verify dry-run restore first, then stop for my approval before modifying anything.

---

## Prompt 4: Generic Android Phone Audit (Samsung / Pixel / OnePlus / Motorola)

> My phone is `<Brand & Model>`, NOT Xiaomi. Follow `skills/clean-miui-ai/SKILL.md` using `profiles/generic_android_audit.json`.
>
> First run backup + `restore.py --dry-run`. Then inspect `packages_inventory.json` and suggest only clearly safe preinstalled bloat/ad stubs (preserving all launcher, IME, GMS, Play Store, telephony, camera, and core OEM services) for my review before applying anything.

---

## Prompt 5: Restore / Undo Everything

> Restore my phone back to its pre-cleanup snapshot using `scripts/restore.py` from my latest backup in `backups/LATEST_BACKUP.txt`. First run with `--dry-run` and show me what will be restored before proceeding.
