# One-Time Phone Connection Guide (USB or Same-Wi-Fi Wireless)

You only need to enable Developer Options **once**. After connecting via USB or pairing on the **same Wi-Fi network**, your AI handles backup, verification, cleanup, and rollback automatically.

---

## Step 1: Prerequisites on Your Computer

1. **Python 3.9+** (`python --version` or `python3 --version`)
2. **Android Platform Tools (`adb`)** — install once via:
   - **Windows (`winget`):** `winget install Google.PlatformTools`
   - **macOS (`Homebrew`):** `brew install android-platform-tools`
   - **Linux (`apt`):** `sudo apt install android-tools-adb`
   - *Or unzip Google's official SDK Platform Tools anywhere on PATH (or Android Studio SDK — `scripts/adb_helper.py` detects standard SDK paths automatically).*

Check that ADB is visible:

```bash
python scripts/adb_helper.py doctor
```

---

## Step 2: Enable Developer Options on Your Phone (Once)

### Xiaomi / Redmi / POCO (MIUI or HyperOS)
1. Open **Settings -> About phone**.
2. Tap **MIUI version** (or **OS version** on HyperOS) **7 times** until `"You are now a developer!"` appears.
3. Go to **Settings -> Additional settings -> Developer options**.

### Stock / Other Android (Pixel, Samsung, OnePlus, Motorola)
1. Open **Settings -> About phone** (or **Software information** on Samsung).
2. Tap **Build number** **7 times** and confirm your screen PIN.
3. Open **Settings -> System -> Developer options** (or **Settings -> Developer options** on Samsung).

---

## Step 3: Choose Connection Method (USB OR Same-Wi-Fi Wireless)

### Option A: USB Cable (Simplest & Fastest for Backups)
1. In **Developer options**, enable **USB debugging**.
2. Connect the phone to your PC with a data-capable USB cable (if prompted on phone, choose **File Transfer / Android Auto**, not Charge-only).
3. Unlock the phone screen and tap **Allow** on the `"Allow USB debugging?"` popup (check *Always allow from this computer*).
4. Confirm connection:
   ```bash
   python scripts/adb_helper.py doctor
   ```
   Look for `state=device` (if `state=unauthorized`, unlock the phone screen and accept the popup).

### Option B: Wireless Debugging (Same Wi-Fi Network, Android 11+)
> Make sure your phone and computer are on the **exact same Wi-Fi network** (no client isolation / guest Wi-Fi).

1. In **Developer options**, turn on **Wireless debugging**.
2. Tap into **Wireless debugging** -> tap **Pair device with pairing code**.
3. Note the **IP address & Port** (example: `192.168.1.25:37123`) and **6-digit Wi-Fi pairing code**.
4. Run pairing once:
   ```bash
   python scripts/adb_helper.py pair --endpoint <IP:PAIR_PORT> --code <6_DIGIT_CODE>
   ```
5. Look back at the main **Wireless debugging** screen (the main **IP address & Port** changes after pairing — note that new port, example `192.168.1.25:41895`). Connect:
   ```bash
   python scripts/adb_helper.py connect --endpoint <IP:CONNECT_PORT>
   ```
   *(On future sessions on the same Wi-Fi, you can often reconnect automatically without typing the port:)*
   ```bash
   python scripts/adb_helper.py connect --mdns
   ```
6. Verify:
   ```bash
   python scripts/adb_helper.py doctor
   ```

---

## Step 4 (Xiaomi MIUI / HyperOS Optional Toggles)

Standard bloatware disabling/uninstalling works with regular **USB debugging** / **Wireless debugging**. However, Xiaomi restricts two extra ADB capabilities behind separate switches inside **Developer options**:

1. **`USB debugging (Security settings)`** — Required ONLY if you want ADB to modify system `settings` keys, grant/revoke `appops`, or adjust `wm density` display scaling. *(Note: On MIUI, enabling this usually asks for Xiaomi Account sign-in once.)*
2. **`Install via USB`** — Required ONLY if you later need `adb install` to reinstall a **non-system user-installed / third-party preinstalled APK**.
   - *Why our scripts avoid needing this:* Clean MIUI AI defaults to `disable_user` (`pm disable-user --user 0`) on non-system OEM stubs (`Mi Pay`, `Indus Appstore`, `Facemoji`, `Glance`) so `OneClick_Restore` can re-enable them cleanly without hitting `INSTALL_FAILED_USER_RESTRICTED`.

---

## Troubleshooting

- **`unauthorized` device:** Unlock phone -> turn off/on USB Debugging -> replug cable -> tap **Allow**.
- **Wireless pairing fails / times out:** Keep the `"Pair with device"` popup open on the phone screen while running `adb_helper.py pair` (closing that dialog cancels the pairing port).
- **Multiple devices attached:** Check `adb_helper.py doctor` output and pass `--serial <SERIAL>` to backup/analyze/apply/restore commands.
- **No phone showing on Windows over USB:** Install OEM USB drivers or Google USB Driver, or simply switch to **Option B (Wireless debugging)** on the same Wi-Fi!
