# Clean MIUI AI — Workspace Agent Rules

When a user opens this repository and asks an AI assistant to inspect, declutter, de-bloat, restyle, or restore an Android / Xiaomi (MIUI / HyperOS) phone:

1. **Follow the Phased Workflow:** Read [`skills/clean-miui-ai/SKILL.md`](skills/clean-miui-ai/SKILL.md) first and execute phases strictly in order:
   - **Pre-flight & Connect (`scripts/adb_helper.py doctor` / `pair` / `connect`)**
   - **Mandatory Backup + Dry-Run Verify (`scripts/inspect_and_backup.py` + `scripts/restore.py --dry-run`)**
   - **Device Audit & Draft Plan (`scripts/analyze_backup.py`)**
   - **Plain-Language User Review & Explicit Approval Gate**
   - **Apply via `scripts/apply_changes.py` (never ad-hoc `pm`/`settings` loops)**
   - **Verify (`scripts/verify_device.py`) + UI/Control Center Guide (`docs/UI_AND_CONTROL_CENTER_GUIDE.md`)**
2. **Existing OS Only:** Only modify stock OS state via non-root ADB and phone Settings toggles. Never flash ROMs, unlock bootloaders, or root devices.
3. **Protect Critical Packages & Prerequisites:** All writes must go through `scripts/apply_changes.py` so `scripts/safety.py` and live active-Home / active-IME protections execute. Never remove an OEM keyboard, browser, file manager, calculator, or calendar unless its replacement is installed.
4. **Honest Reporting:** Only claim commands succeeded if you ran them and observed zero errors. If MIUI raises `SecurityException` (missing `USB debugging (Security settings)`) or `INSTALL_FAILED_USER_RESTRICTED` (missing `Install via USB`), explain the exact Developer Options toggle cleanly rather than hiding it.
5. **Privacy Hygiene:** Never place device backups (`backups/`), local execution plans (`plans/`), device serial numbers, local IPs, or personal screenshots into git-tracked files.
