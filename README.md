<div align="center">
  <img src="assets/logo.svg" alt="Clean Android UI Logo" width="84" height="84" />
  <h1>Clean Android UI</h1>
  <p><strong>No bloatware apps, no ads — clean, organized, reversible Android &amp; Xiaomi (MIUI / HyperOS) UI strictly on your existing OS.</strong></p>
</div>

> [!IMPORTANT]
> **NO OS change. NO custom ROM. NO bootloader unlock. NO root required.**
> Clean MIUI AI only configures your **existing stock Android / MIUI / HyperOS system** using standard ADB (`pm`, `settings`, `appops`, `wm`) and built-in phone Settings — so warranty stays intact, banking/UPI apps keep working normally, and every change stays safely reversible.

---

## What It Does

1. **One-Click Backup & Restore Check FIRST:** Captures your device inventory, package states, settings (`system`/`secure`/`global`), and pullable APKs (with SHA-256 + ZIP checks), then runs `restore.py --dry-run` **before** changing a single app.
2. **Removes Ads & Bloatware Safely:** Targets ad daemons, trackers, carrier/partner stubs, and unwanted OEM duplicates without touching Android core, SystemUI, Play Services, Telephony, Settings, Mi Cloud / Find Device, active Launcher, or active Keyboard.
3. **Optional "Stock Google" Look + Control Center Polish:** Lets you remove duplicate OEM tools (Browser, File Manager, Facemoji Keyboard, Calculator, Calendar) *only if* clean replacements (`Chrome`, `Files by Google`, `Gboard`, `Google Calculator`, `Google Calendar`) are already installed — plus guides you through Control Center layout, crisp `440 dpi` / `392dp` scaling, clean App Drawer categories, and optional Private DNS ad blocking.
4. **Universal AI Bundle:** Includes an Antigravity plugin (`plugin.json` + `.agents/skills.json`), a full phased workflow skill ([`skills/clean-miui-ai/SKILL.md`](skills/clean-miui-ai/SKILL.md)), safety rules ([`AGENTS.md`](AGENTS.md), [`rules/AGENTS.md`](rules/AGENTS.md), [`GEMINI.md`](GEMINI.md), [`CLAUDE.md`](CLAUDE.md)), and cross-platform One-Click `.bat` / `.sh` scripts.

---

## Supported Devices & Tested Baseline

| Scope | Status | Notes |
| :--- | :--- | :--- |
| **Xiaomi / Redmi / POCO (MIUI 12–14 & HyperOS)** | Full Preset (`profiles/xiaomi_miui_hyperos_clean.json`) | Tier A (ads/bloat) + Tier B (Google Stock replacements with prerequisite checks) + AppOps presets |
| **Verified Hardware Baseline** | Real-Device Tested | **Redmi Note 10S (`M2101K7BI`)**, Android 13, MIUI Global `14.0.11.0.TKLINXM` |
| **Other Android (Pixel, Samsung, OnePlus, Motorola, etc.)** | Safe Audit Fallback (`profiles/generic_android_audit.json`) | Only flags cross-OEM Meta stubs automatically; AI inspects remaining inventory conservatively |

---

## 3-Step Quickstart

### 1. Install or Clone the Bundle

**Option A — Clone & open in any AI coding assistant (Antigravity, Claude Code, Cursor, Codex, Gemini CLI):**
```bash
git clone https://github.com/BharatAdhana/clean-android-ui.git
cd clean-android-ui
```

**Option B — Use as an Antigravity Plugin:**
Open the Plugins manager in Antigravity and install/load this repository folder (powered by [`plugin.json`](plugin.json), [`rules/AGENTS.md`](rules/AGENTS.md), and [`skills/clean-miui-ai/SKILL.md`](skills/clean-miui-ai/SKILL.md)).

**Option C — Double-Click Backups Without an AI:**
- **Windows:** Double-click `OneClick_Backup.bat` (creates backup + runs restore `--dry-run`) or `OneClick_Restore.bat`
- **macOS / Linux:** Run `bash oneclick_backup.sh` or `bash oneclick_restore.sh` (or run `chmod +x oneclick_*.sh` once and execute `./oneclick_backup.sh`)

---

### 2. One-Time Connection Setup (USB or Same-Wi-Fi Wireless)

Follow [`docs/CONNECTION_GUIDE.md`](docs/CONNECTION_GUIDE.md) once:
1. Enable **Developer options** on your phone.
2. Turn on **USB debugging** (plug in cable) **OR** **Wireless debugging** (same Wi-Fi network as your PC).
3. *(Optional on Xiaomi MIUI/HyperOS)* Turn on **USB debugging (Security settings)** if you also want ADB-assisted `settings` / `appops` / `wm density` tweaks.
4. Check connection on your computer:
   ```bash
   python scripts/adb_helper.py doctor
   ```

---

### 3. Paste This Prompt to Your AI

More variations live in [`QUICKSTART_PROMPTS.md`](QUICKSTART_PROMPTS.md). Here is the standard prompt:

```text
Use this repository (AGENTS.md and skills/clean-miui-ai/SKILL.md) to clean and organize my phone on its existing OS (no root, no ROM flash).

My details:
- Phone Brand & Model: <e.g., Redmi Note 10S / POCO X5 / Samsung>
- OS / Version: <e.g., MIUI 14 / HyperOS / Android 14>
- Connection: <USB cable OR Wireless ADB on same Wi-Fi — IP:Port & pairing code if needed>
- Goal: <Clean Google Stock style + remove bloat/ads OR Remove ads & bloat only>

First run `adb_helper.py doctor` and help me connect if needed. Next, take a full One-Click Backup and verify `restore.py --dry-run` before touching anything. Then inspect the backup, show me an itemized proposal table with safety notes, and wait for my approval before applying changes. Finally verify the phone and walk me through the UI & Control Center polish steps in docs/UI_AND_CONTROL_CENTER_GUIDE.md.
```

---

## How the Safety Architecture Works

Every cleanup run strictly enforces multiple independent layers of protection:

1. **Mandatory Backup + Dry-Run Verification First:**
   - [`scripts/inspect_and_backup.py`](scripts/inspect_and_backup.py) snapshots `packages_inventory.json`, `settings_{system,secure,global}.txt`, `ad_tracking_audit.json`, `device_info.json`, and local pullable APKs into `backups/<YYYYMMDD_HHMMSS>_<MODEL>/`, updating `backups/LATEST_BACKUP.txt`.
   - [`scripts/restore.py`](scripts/restore.py) `--dry-run` verifies every APK's SHA-256 hash and ZIP integrity before any change starts.
2. **Hardcoded + Live Dynamic Protection (`scripts/safety.py` + `scripts/apply_changes.py`):**
   - `apply_changes.py` refuses any plan without `"approved": true`.
   - Blocks core packages (`SystemUI`, Play Services, Settings, Telephony, Package Installer, System Updater, Mi Account / Cloud / Find Device), **plus live active Home launcher and live active Keyboard IME detected on your phone**.
3. **Replacement Prerequisite Checks (`requires_any_of`):**
   - [`scripts/analyze_backup.py`](scripts/analyze_backup.py) refuses to recommend removing Xiaomi Browser, File Manager, Facemoji Keyboard, Calculator, or Calendar unless `Chrome`, `Files by Google`, `Gboard`, `Google Calculator`, or `Google Calendar` is confirmed present.
4. **Non-System App Safe Handling:**
   - Non-system preinstalled apps default to `disable_user` instead of `uninstall_user0` because Xiaomi MIUI blocks ADB APK reinstalls (`INSTALL_FAILED_USER_RESTRICTED`) unless `Install via USB` is toggled on.
5. **Instant Full Rollback (`scripts/restore.py`):**
   - Re-enables disabled apps, runs `pm install-existing --user 0` for user-0 uninstalled system packages, reinstalls missing backed-up APKs (`adb install` / `adb install-multiple`), restores logged `settings`, `appops`, and `wm density`, and saves a detailed restoration report.

---

## Honest Scope & Known Real-World Caveats

- **Non-root ADB backup scope:** Backups capture APKs, package install/enabled state, and recorded `settings`, `appops`, and `wm density` snapshots. Without root, ADB cannot copy private `/data/data` internal app databases; package actions (`disable-user` / `uninstall -k --user 0`) preserve app state cleanly for rollback.
- **"System apps updater" entry in MIUI Global Settings:** Removing/disabling `com.xiaomi.discover` stops the background updater app, though MIUI Global Settings still shows the static `"System apps updater"` menu row.
- **Control Center sizing (`325dp` quirk):** On MIUI 14 (`Redmi Note 10S`), shrinking display DPI / changing Smallest Width aggressively compresses the New Control Center toggle card width. Keep Smallest Width near **`392dp` (`440 dpi`)** + **Text size: Small** — see [`docs/UI_AND_CONTROL_CENTER_GUIDE.md`](docs/UI_AND_CONTROL_CENTER_GUIDE.md).
- **Manual in-app ad switches:** Certain MIUI apps ignore `settings put` over ADB or require `USB debugging (Security settings)` enabled once. See the short manual checklist in [`docs/UI_AND_CONTROL_CENTER_GUIDE.md`](docs/UI_AND_CONTROL_CENTER_GUIDE.md#5-manual-in-app-ad-toggles-on-miui-always-double-check-once).

---

## Repository Layout

```text
clean-android-ui/
├── plugin.json                              # Antigravity plugin manifest
├── .agents/skills.json                      # Workspace skill auto-discovery
├── AGENTS.md / GEMINI.md / CLAUDE.md        # Universal AI instructions & pointers
├── OneClick_Backup.bat / oneclick_backup.sh # Cross-platform Backup + Dry-Run Verify
├── OneClick_Restore.bat / oneclick_restore.sh
├── QUICKSTART_PROMPTS.md                    # Ready copy-paste prompts
├── skills/clean-miui-ai/SKILL.md            # Complete AI workflow playbook
├── rules/AGENTS.md                          # Plugin-scoped guardrails
├── docs/
│   ├── CONNECTION_GUIDE.md                  # USB & same-Wi-Fi Wireless ADB setup
│   └── UI_AND_CONTROL_CENTER_GUIDE.md       # Clean UI, Control Center DPI & ad toggles
├── profiles/
│   ├── xiaomi_miui_hyperos_clean.json       # Xiaomi MIUI / HyperOS Tier A, B & AppOps
│   ├── generic_android_audit.json           # Safe cross-OEM fallback profile
│   └── schema_notes.md                      # Profile format & prerequisite rules
├── scripts/
│   ├── adb_helper.py                        # doctor / pair / connect / serial resolution
│   ├── safety.py                            # Protected core rules, SHA-256 & path helpers
│   ├── inspect_and_backup.py                # Phase 1-3 snapshot & APK backup
│   ├── analyze_backup.py                    # Profile evaluation + unapproved draft plan
│   ├── apply_changes.py                     # Guarded execution + CHANGELOG generation
│   ├── restore.py                           # Dry-run verify & full rollback engine
│   └── verify_device.py                     # Post-cleanup health & readiness check
└── tests/
    └── test_core.py                         # Unit tests (parsers, safety guards, dry-run logic)
```

---

## Running Automated Tests

Verify parsers, protection guards, prerequisite checks, and restore drift logic locally (no phone required):

```bash
python -m unittest discover -s tests -v
```
