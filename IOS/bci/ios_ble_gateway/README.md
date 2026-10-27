# iOS BLE Gateway — Mac/PC → iPhone (bluetooth-central)

Experimental transport to answer: *Can iPhone receive BCI commands via BLE while backgrounded and trigger JioSaavn shortcuts?*

```
Mac (BLE Peripheral GATT Server)
  ↓ notify 7B43F002 characteristic (UTF-8 "NEXT")
iPhone (BLE Central, bluetooth-central)
  ↓ didUpdateValueFor
BLEManager → CommandValidator → CommandExecutor → ShortcutService → JioSaavn
```

MQTT remains as fallback (MODE A). BLE is MODE B experimental alongside.

## UUIDs (valid 128-bit)
Spec's `7B43F001-...-BCI000000001` contains `I` (non-hex) → rejected by `CBUUID`. Replaced:

* Service: `7B43F001-7B43-4B43-9B43-000000000001`
* Characteristic: `7B43F002-7B43-4B43-9B43-000000000002` (notify/read/write)

Documented consistently in `Services/BLEManager.swift:6` and `ble_peripheral.py:6`.

## Mac Environment

macOS 26.6 arm64 Python 3.13 (this Mac). `bluez` (Linux-only) does NOT work on macOS.

**Preferred:** `bless` (0.2.8) which wraps `CBPeripheralManager` via `pyobjc` on macOS and `BlueZ` on Linux. Works on macOS but requires:

* `pip install bless bleak` (bless pulls `bleak`)
* System Settings → Privacy & Security → Bluetooth → Terminal/Python allowed
* The Python process must remain foreground while advertising (macOS suspends peripheral when app hides).

**If `bless` fails** (common on newer macOS due to missing entitlements), use the more reliable **Swift macOS peripheral** (Apple sample):

* Create `ios_ble_gateway/BLEPeripheralSwift/` as a tiny Swift command-line tool using `CBPeripheralManager`:

```swift
import CoreBluetooth
let service = CBMutableService(type: CBUUID(string:"7B43F001-..."), primary:true)
let char = CBMutableCharacteristic(type: CBUUID(string:"7B43F002-..."), properties: [.notify, .read, .write], value: nil, permissions: [.readable,.writeable])
service.characteristics=[char]; peripheralManager.add(service); peripheralManager.startAdvertising([CBAdvertisementDataServiceUUIDsKey:[service.uuid], CBAdvertisementDataLocalNameKey:"BCI-Mac-Gateway"])
```

Compile with `swiftc` and run: `./ble_peripheral_swift`. This is documented as fallback because Apple's BLE peripheral on macOS is best done via native `CoreBluetooth`, not Python.

## How to Run (Python)

```bash
cd Desktop/Coding/ios/bci/ios_ble_gateway
python3 -m venv venv; source venv/bin/activate
pip install -r requirements.txt
python3 ble_peripheral.py
# → [BLE] Advertising started as 'BCI-Mac-Gateway'
# Keep terminal in foreground.
```

On iPhone: Open `JioSaavnBCIController` → **BLE Experimental** → `Scan BLE` → `Connected — Ready` → On Mac type `NEXT` → iPhone logs:

```
[BLE] Received command: NEXT
[COMMAND] Validated: NEXT
[SHORTCUT] Mapping NEXT → JioSaavn Next
```

Then `JioSaavn Next` shortcut should change track (if JioSaavn is Now Playing). **Do not claim success if shortcut was not observed.**

## iOS Configuration

Added in `project.pbxproj:266`:

* `INFOPLIST_KEY_NSBluetoothAlwaysUsageDescription` = "BCI commands via BLE from Mac/PC"
* `INFOPLIST_KEY_NSBluetoothPeripheralUsageDescription` = same
* `INFOPLIST_KEY_UIBackgroundModes` = `bluetooth-central` (only this, no audio/location/voip)

Works with free Personal team (no paid program required). `CBCentralManager` init with `CBCentralManagerOptionRestoreIdentifierKey:"BCICentralManager"` + `willRestoreState` handles wake.

## Background Behavior

* Foreground: full scan/connect/notify.
* Home → JioSaavn foreground, BCI app background: iOS *may* wake app for BLE notify (documented: `bluetooth-central` wakes for characteristic updates). Not guaranteed — throttled by system. Test shows console after delay.
* Force-quit (swipe away): **No wake** — documented: *force-quit breaks BLE background*.
* `UIApplication.open(shortcuts://…)` from background **may return `success=false`** — second limitation separate from BLE. Must test. Log shows `[SHORTCUT] Background UIApplication.open completion: success=…`.

No audio/location/voip hacks, no APNs overlap.

## Integration with BCI/FSM

After Tests 1–4 pass:

```
BCI/Master Hub → command string → call ble_peripheral.send("NEXT") instead of MQTT publish
# Example Python BCI integration:
# from ble_peripheral import set_command
# set_command(detected_command)
```

Keep MQTT fallback; do not duplicate execution (both feed `CommandValidator → CommandExecutor`).

## Limitations (README must state)

1. BLE background ≠ unlimited execution — iOS wakes only for Bluetooth events.
2. Throttled/best-effort, controlled by OS.
3. Force-quit → no delivery.
4. `shortcuts://` launch from background may be denied — second gate.
5. Mac Python peripheral less reliable than Swift `CBPeripheralManager` — prefer Swift if bless flaky.
6. UUIDs above are valid replacements for spec's `BCI...`.

## Test Plan References

See main project README for TEST 1–5 steps.
