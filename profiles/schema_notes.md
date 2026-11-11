# Cleanup Profile Schema & Safety Rules

Each profile JSON under `profiles/` acts as a **candidate catalog**, never an automatic bulk wipe list.

## Tiers

1. `tier_a_safe_bloat_and_ads`
   - Dedicated advertising SDKs (`com.miui.msa.global`, `com.miui.analytics`, `com.miui.daemon`), promotional app stores, Meta carrier stubs, and unused Xiaomi promotional services.
   - Safe on MIUI / HyperOS without needing Google replacement apps.
2. `tier_b_google_stock_replacement`
   - Duplicate Xiaomi utilities (`Mi File Manager`, `Mi Music`, `Mi Video`, `Mi Browser`, `Mi Notes`, `Mi Calculator`, `Mi Calendar`, `Xiaomi Cloud`, `ShareMe`, `Scanner`, etc.) removed to achieve a clean **stock Google Android** app drawer.
   - Entries with `requires_any_of` are automatically **withheld** by `scripts/analyze_backup.py` unless the replacement app (e.g., `Files by Google`, `Gboard`, `Chrome`, `Google Calculator`, `Google Calendar`) is already installed and enabled on the device.
3. `appops_restrictions`
   - Non-destructive `cmd appops set` rules to mute promo notifications and background wakeups without breaking core APK installation or themes.

## Hard Guardrails (`scripts/safety.py`)

`scripts/apply_changes.py` refuses at runtime to touch any package in `NEVER_REMOVE_EXACT`, `NEVER_REMOVE_PREFIXES`, or the device's active default keyboard/home launcher — even if manually added to a plan file.

## Caution Note for Gamers (`com.xiaomi.joyose`)

`com.xiaomi.joyose` handles Xiaomi gaming thermal/GPU telemetry alongside some analytics and is intentionally **excluded** from the default auto-proposal so performance-sensitive users stay safe unless they explicitly opt in.
