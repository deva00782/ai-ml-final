#!/usr/bin/env python3
"""
Mac/PC BLE GATT Peripheral for BCI → iPhone experiment
iPhone is CENTRAL (bluetooth-central), Mac is PERIPHERAL.

Service UUID: 7B43F001-7B43-4B43-9B43-000000000001
Char UUID:    7B43F002-7B43-4B43-9B43-000000000002  (notify/read/write, UTF-8 strings NEXT etc.)

Note: Spec's original 7B43F001...BCI000000001 contains 'I' (non-hex) -> replaced with 000000000001, valid 128-bit.

Requires: pip install -r requirements.txt
macOS: Needs Bluetooth permission, and Python must use CoreBluetooth via bless/pyobjc. BLE peripheral on macOS is supported via bless which wraps CBPeripheralManager.
If bless fails on macOS, use Swift alternative: ios_ble_gateway/BLEPeripheralSwift/README.
Linux (BlueZ) also works with bless, but this prototype targets macOS 26.6 arm64.

Usage:
  python3 ble_peripheral.py
  > Enter command: NEXT
"""

import asyncio
import sys

SERVICE_UUID = "7B43F001-7B43-4B43-9B43-000000000001"
CHAR_UUID    = "7B43F002-7B43-4B43-9B43-000000000002"
DEVICE_NAME  = "BCI-Mac-Gateway"

VALID_CMDS = ["NEXT","PREVIOUS","PLAY","PAUSE","VOLUME_UP","VOLUME_DOWN","SEARCH","OPEN_LINK"]

try:
    from bless import BlessServer, BlessGATTCharacteristic, GATTCharacteristicProperties, GATTAttributePermissions
    HAS_BLESS = True
except ImportError:
    HAS_BLESS = False
    print("[BLE] Missing bless — install: pip install bless bleak")
    sys.exit(1)

async def run():
    print("[BLE] Initializing GATT server...")
    print(f"[BLE] Service UUID: {SERVICE_UUID}")
    print(f"[BLE] Characteristic UUID: {CHAR_UUID} (valid replacement for BCI000000001/2)")
    server = BlessServer(name=DEVICE_NAME)
    # bless requires async setup
    await server.add_new_service(SERVICE_UUID)
    await server.add_new_characteristic(
        SERVICE_UUID,
        CHAR_UUID,
        properties= GATTCharacteristicProperties.notify | GATTCharacteristicProperties.read | GATTCharacteristicProperties.write,
        permissions= GATTAttributePermissions.readable | GATTAttributePermissions.writeable,
        value=None
    )
    await server.start(prioritize_local_name=False)
    print(f"[BLE] Advertising started as '{DEVICE_NAME}'")
    print("[BLE] Waiting for iPhone Central to connect... (use Scan BLE on iPhone)")
    # Bless handles advertising automatically on start()

    # Track connections
    def on_write(*args, **kwargs):
        print(f"[BLE] Write request: {args} {kwargs}")

    # Keep alive and allow interactive commands
    while True:
        try:
            cmd = await asyncio.to_thread(input, "Enter command (NEXT/PREVIOUS/PLAY/PAUSE/VOLUME_UP/VOLUME_DOWN/SEARCH/OPEN_LINK or 'q' to quit): ")
        except (EOFError, KeyboardInterrupt):
            break
        cmd = cmd.strip().upper()
        if cmd in ("Q", "QUIT", "EXIT"):
            break
        if cmd not in VALID_CMDS:
            print(f"[BLE] Invalid. Valid: {', '.join(VALID_CMDS)}")
            continue
        data = cmd.encode('utf-8')
        # Update characteristic value and notify — handle both bless API variants
        try:
            # Bless 0.2.x: update_value takes service_uuid, char_uuid
            # Try to set characteristic value via get_characteristic if available
            try:
                ch = server.get_characteristic(CHAR_UUID)
                if ch is not None:
                    ch.value = data
            except Exception:
                pass
            # Notify — bless expects update_value to trigger notification
            try:
                server.update_value(SERVICE_UUID, CHAR_UUID)
            except TypeError:
                # Some versions expect (char_uuid, value)
                server.update_value(CHAR_UUID, data)
            print(f"[BLE] Sending command: {cmd}")
            # Count subscribers if available
            subs = getattr(server, '_subscribed_clients', None) or getattr(server, '_connected_clients', None)
            count = len(subs) if isinstance(subs, (list, set, dict)) else "?"
            print(f"[BLE] Command sent — notifying {count} central(s). On iPhone check [BLE] Received command: {cmd}")
            if count == "?" or count == 0:
                print("[BLE] No central subscribed yet — on iPhone tap Scan BLE → Connected — Ready, then retry")
        except Exception as e:
            print(f"[BLE] Send failed: {e}")
            import traceback; traceback.print_exc()

    await server.stop()
    print("[BLE] Stopped")

if __name__ == "__main__":
    # Check platform
    import platform
    print(f"[BLE] Platform: {platform.system()} {platform.machine()} Python {sys.version.split()[0]}")
    if platform.system() == "Darwin":
        print("[BLE] macOS detected — bless uses CoreBluetooth CBPeripheralManager. Allow Bluetooth permission when prompted.")
        print("[BLE] If advertising fails, see README Swift peripheral alternative (more reliable on macOS).")
    elif platform.system() == "Linux":
        print("[BLE] Linux detected — ensure bluetoothd + BlueZ permissions.")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[BLE] Interrupted")

