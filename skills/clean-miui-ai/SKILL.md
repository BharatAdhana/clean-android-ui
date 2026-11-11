---
name: clean-miui-ai
description: Safely declutter, de-bloat, reduce system ads, and apply clean stock-style UI tweaks to Android, MIUI, and HyperOS phones on the existing OS (no root, no ROM flash) via ADB with mandatory One-Click Backup and dry-run restore verification first.
---

# Clean MIUI AI — Phased Phone Cleanup Workflow

## Overview & Honest Scope

This skill modifies **only the existing stock OS** over standard Android Debug Bridge (ADB) (`pm`, `settings`, `appops`, `wm`).

- **No OS change / No custom ROM / No bootloader unlock / No root required.**
- **Mandatory Backup & Restore Check First:** Capture a full pre-change snapshot and verify its restore plan with `--dry-run` before touching any package or setting.
- **Reversible Backup Boundary:** Backups capture package inventory, disabled/uninstalled states, pullable APKs (with SHA-256 + ZIP checks), and recorded `settings`, `appops`, and `wm density` snapshots. Without root, ADB cannot back up private `/data/data` app files; package operations keep or safely disable user-0 state so apps can be re-enabled cleanly.

---

## Utility Scripts

Run all commands from the repository root with Python 3.9+ and `adb` on `PATH` (or bundled Platform-Tools detected by `scripts/adb_helper.py`):

| Script | Purpose |
| :--- | :--- |
| `scripts/adb_helper.py` | Check ADB tools, pair Wireless Debugging, connect over Wi-Fi, resolve device serial |
| `scripts/inspect_and_backup.py` | Create timestamped backup snapshot, pull APKs safely, verify SHA-256 & ZIP integrity |
| `scripts/restore.py` | Dry-run or execute rollback of packages, settings, appops, and display density |
| `scripts/analyze_backup.py` | Classify installed apps against OEM profile + safety checks, emit draft unapproved plan |
| `scripts/apply_changes.py` | Verify `"approved": true` + runtime protection guards, apply changes, write changelog |
| `scripts/verify_device.py` | Verify core Android services, active launcher, active IME, crash tail, and restore readiness |

---

## Workflow

### Step 0: Pre-Flight & One-Time Connection

1. Run environment and device check:
   ```bash
   python scripts/adb_helper.py doctor
   ```
2. If no authorized device appears, guide the user through `docs/CONNECTION_GUIDE.md`:
   - **USB:** Enable Developer Options -> USB Debugging, plug in cable, accept RSA prompt on phone.
   - **Wireless (same Wi-Fi):** Enable Developer Options -> Wireless debugging -> Pair device with pairing code:
     ```bash
     python scripts/adb_helper.py pair --endpoint <IP:PAIR_PORT> --code <6_DIGIT_CODE>
     python scripts/adb_helper.py connect --endpoint <IP:CONNECT_PORT>
     ```
     Or try mDNS discovery if already paired on the same Wi-Fi:
     ```bash
     python scripts/adb_helper.py connect --mdns
     ```
3. Re-run `python scripts/adb_helper.py doctor` until `device` (not `unauthorized` or `offline`) is confirmed.
4. For Xiaomi MIUI / HyperOS users who also want ADB permission fixes or APK reinstalls later, mention the optional toggles in **Developer options**:
   - `USB debugging (Security settings)` — required on MIUI if changing `settings put` / `appops` / `wm density` over ADB.
   - `Install via USB` — required only if reinstalling split/non-system APKs later via `adb install`.

### Step 1: Mandatory One-Click Backup + Restore Verification

**Never modify any package or setting before finishing this step.**

1. Create a fresh backup:
   ```bash
   python scripts/inspect_and_backup.py --serial <SERIAL>
   ```
   *(Or ask the user to run `OneClick_Backup.bat` on Windows / `./oneclick_backup.sh` on macOS/Linux.)*
2. Immediately verify the backup is restorable:
   ```bash
   python scripts/restore.py --backup-dir backups/<BACKUP_FOLDER> --serial <SERIAL> --dry-run
   ```
   Confirm zero corrupted APK checksums/ZIPs and note the exact backup folder path for rollback.

### Step 2: Device Detection & Profile Analysis

Inspect the device summary generated inside `backups/<BACKUP_FOLDER>/summary.json` (model, Android version, MIUI/HyperOS version, density, active launcher, active IME).

- **Xiaomi / Redmi / POCO (MIUI or HyperOS):** Use `profiles/xiaomi_miui_hyperos_clean.json`:
  - User wants **Ads & Bloatware removal only** -> `--tier tier_a_only`
  - User wants **Clean Stock Google look + Bloatware removal** -> `--tier tier_a_and_b`
- **Other Android OEMs (Pixel, Samsung, OnePlus, Motorola, Nothing, etc.):** Use `profiles/generic_android_audit.json` (`--tier tier_a_only`) and inspect `packages_inventory.json` carefully before suggesting any OEM-specific entry. Never apply Xiaomi package lists blindly to non-Xiaomi phones.

Run the analyzer to emit an **unapproved** draft plan:

```bash
python scripts/analyze_backup.py ^
  --backup-dir backups/<BACKUP_FOLDER> ^
  --profile profiles/xiaomi_miui_hyperos_clean.json ^
  --tier tier_a_and_b ^
  --emit-draft-plan plans/draft_plan.json
```

Pay close attention to `requires_any_of` prerequisites reported by `analyze_backup.py` (for example: never propose disabling Xiaomi Calculator, Calendar, File Manager, Browser, or Facemoji Keyboard unless Google Calculator, Google Calendar, Files by Google, Chrome, or Gboard is installed).

### Step 3: Present Plain-Language Review & Wait for Explicit Approval

Show the user a structured Markdown table grouped into:
1. **Tier A — Safe Bloatware & Ad Services** (MSA, Analytics, GetApps, App Vault, Mi Pay/Indus stubs, Meta preinstalls, etc.)
2. **Tier B — OEM App Cleanup / Stock Google Style** (duplicate dialer, messages, browser, keyboard, file manager, calendar, calculator — strictly listing which working replacement app was detected)
3. **AppOps / Settings / Display Density Polish** (e.g., revoking `POST_NOTIFICATION` / `SYSTEM_ALERT_WINDOW` / background clutter or keeping Smallest Width / `440 dpi` balanced for Control Center)
4. **Excluded / Protected System Core** (SystemUI, Play Services, active Home launcher, active Keyboard IME, Settings, Phone, System Updater, Mi Cloud / Find Device)

Note known MIUI limitations upfront:
- Non-system preinstalled apps (e.g., `com.mipay.wallet.in`, `com.indus.appstore`, `com.facemoji.lite.xiaomi`, `com.miui.android.fashiongallery`) default to `disable_user` instead of `uninstall_user0` because MIUI blocks `adb install` on restore unless `Install via USB` is toggled on.
- `com.xiaomi.discover` disables background System Apps Updater activity, though MIUI Global Settings still renders the static `"System apps updater"` menu row.
- MIUI Global Ad/System toggles (like personalized ads in MSA, Themes, Security, File Manager, Downloads) sometimes reset or resist `settings put` over ADB; list manual in-app switches cleanly in Step 5 rather than claiming ADB toggled them if `SecurityException` occurred.

**Wait for explicit user approval** (confirming exact packages/settings to keep or skip) before creating `plans/approved_plan.json` with `"approved": true`.

### Step 4: Apply Approved Changes

Write the approved subset into `plans/approved_plan.json` (`"approved": true`), ensuring `scripts/safety.py` protections remain intact, and run:

```bash
python scripts/apply_changes.py --plan plans/approved_plan.json --backup-dir backups/<BACKUP_FOLDER> --serial <SERIAL>
```

Review `backups/<BACKUP_FOLDER>/CHANGELOG.md` and report any skipped or restricted items honestly (e.g., if `USB debugging (Security settings)` is needed for an optional `settings` / `appops` change).

### Step 5: Verify Health & Guide UI / Control Center Polish

1. Run post-change verification:
   ```bash
   python scripts/verify_device.py --backup-dir backups/<BACKUP_FOLDER> --serial <SERIAL>
   ```
2. Walk the user through the manual polish steps in `docs/UI_AND_CONTROL_CENTER_GUIDE.md` matching their preferences:
   - **Control Center style & full-width layout:** MIUI Control Center `New version` + **Display size / Smallest width `392dp` (`440 dpi`)** + **Text size: Small** so tiles fill cleanly without wrapping or shrinking into a small floating box.
   - **Clean App Drawer / Home Layout:** Enable App Drawer mode (optional categories & Google Discover toggle), hide unwanted clutter on Home, keep only daily stock Google + core apps visible.
   - **Remaining In-App Ad Switches & Optional Private DNS:** Show the exact MIUI toggles (`msa`, Themes recommendations, Security app recommendations, Optional `dns.adguard-dns.com`).

### Step 6: Instant Rollback (Whenever Needed)

If anything misbehaves or the user wants to revert:

```bash
python scripts/restore.py --backup-dir backups/<BACKUP_FOLDER> --serial <SERIAL>
```
*(Or run `OneClick_Restore.bat` / `./oneclick_restore.sh`.)*

---

## Common Mistakes to Avoid

- Applying a plan with `"approved": false` or approving items without the user's explicit go-ahead.
- Removing an OEM app (`com.facemoji.lite.xiaomi`, `com.android.browser`, `com.mi.android.globalFileexplorer`, `com.miui.calculator`, `com.xiaomi.calendar`) when no replacement keyboard/browser/file manager/calculator/calendar is installed yet.
- Guessing or modifying packages manually instead of using `scripts/apply_changes.py` (which enforces `scripts/safety.py` + live IME/Home checks and logs every state transition to `CHANGELOG.md`).
- Over-lowering display DPI/increasing Smallest Width carelessly on MIUI 14 (`325dp` Control Center quirk), which shrinks quick toggles into a compressed box.
- Treating a `Generic Android` device like Xiaomi MIUI — always check `summary.json` first and use `profiles/generic_android_audit.json` on non-Xiaomi devices.
