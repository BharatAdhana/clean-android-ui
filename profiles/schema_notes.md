# Cleanup Profile Schema & Safety Rules

Each profile JSON under `profiles/` acts as a **candidate catalog**, never an automatic bulk wipe list.

## Compatibility Validation (`compatibility`)

Profiles declare machine-readable compatibility rules checked by `scripts/analyze_backup.py`:
- `target_brands`: Allowed device brands/manufacturers (empty list means generic Android fallback).
- `target_os_families`: Expected OS families (`miui`, `hyperos`, or empty for generic).
- `tested_devices`: Hardware/ROM combinations actually tested. For profiles declaring `tested_devices` (such as `xiaomi_miui_hyperos_clean.json`), any device matching OEM/OS without matching `tested_devices` is flagged as **UNTESTED** and **rejected** by `scripts/analyze_backup.py` (`exit 2`) unless `--allow-untested-device` is explicitly passed.
- Cross-OEM mismatches (e.g., applying Xiaomi profile rules to Samsung/Pixel) are also **rejected** by `scripts/analyze_backup.py` unless `--allow-untested-device` is explicitly passed.

## Tiers

1. `tier_a_safe_bloat_and_ads`
   - Dedicated advertising SDKs (`com.miui.msa.global`, `com.miui.analytics`, `com.miui.daemon`), promotional app stores, Meta carrier stubs, and unused Xiaomi promotional services.
   - Intended to avoid breaking core functions even without replacing utility apps.
2. `tier_b_google_stock_replacement`
   - Duplicate Xiaomi utilities (`Mi File Manager`, `Mi Music`, `Mi Video`, `Mi Browser`, `Mi Notes`, `Mi Calculator`, `Mi Calendar`, `Xiaomi Cloud`, `ShareMe`, `Scanner`, etc.) removed to achieve a clean **stock Google Android** app drawer.
   - Entries with `requires_any_of` are automatically **withheld** by `scripts/analyze_backup.py` unless the replacement app (e.g., `Files by Google`, `Gboard`, `Chrome`, `Google Calculator`, `Google Calendar`) is already installed and enabled on the device.
3. `optional_user_preferences`
   - Non-bloat optional utilities (such as Google `Personal Safety` / `com.google.android.apps.safetyhub`) that are **excluded by default** unless `--include-optional` is passed or explicitly chosen by the user.
4. `appops_restrictions`
   - Non-destructive `cmd appops set` rules to mute promo notifications and background wakeups without breaking core APK installation or themes.

## Hard Guardrails (`scripts/safety.py`)

`scripts/apply_changes.py` refuses at runtime to touch any package in `NEVER_REMOVE_EXACT`, `NEVER_REMOVE_PREFIXES`, or the device's active default keyboard/home launcher/dialer/SMS handler — even if manually added to a plan file.

## Caution Note for Gamers (`com.xiaomi.joyose`)

`com.xiaomi.joyose` handles Xiaomi gaming thermal/GPU telemetry alongside some analytics and is intentionally **excluded** from the default auto-proposal so performance-sensitive users stay safe unless they explicitly opt in.
