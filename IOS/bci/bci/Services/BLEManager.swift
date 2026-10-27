import Foundation
import CoreBluetooth
import Combine
import UIKit

// MARK: - BLE UUIDs
// Spec provided 7B43F001-7B43-4B43-9B43-BCI000000001 which contains non-hex 'I' -> invalid.
// Replaced with valid 128-bit UUIDs, documented consistently for iOS + PC.
enum BLEUUID {
    static let service = CBUUID(string: "7B43F001-7B43-4B43-9B43-000000000001")
    static let commandCharacteristic = CBUUID(string: "7B43F002-7B43-4B43-9B43-000000000002")
    // Original spec invalid: BCI000000001 -> replaced 000000000001
}

/// BLE experimental transport — iPhone as CENTRAL, PC/Mac as PERIPHERAL (GATT server)
/// Only handles discovery/connection/notification. Validation/execution delegated to existing pipeline.
/// Background: uses bluetooth-central, CoreBluetooth will wake app for characteristic updates (best-effort).
final class BLEManager: NSObject, ObservableObject {
    @Published var status: String = "Disconnected"
    @Published var peripheralName: String?
    @Published var lastBLECommand: String?
    @Published var isScanning = false
    @Published var isConnected = false
    @Published var discoveredList: [(name: String, id: UUID, rssi: Int)] = []
    @Published var lastScanCount: Int = 0

    private var centralManager: CBCentralManager!
    private var discoveredPeripheral: CBPeripheral?
    private var commandCharacteristic: CBCharacteristic?
    private var allDiscovered: [UUID: CBPeripheral] = [:]

    private let validator = CommandValidator()
    private lazy var executor: CommandExecutor = CommandExecutor(mqttManager: nil)
    // For UI history parity with MQTTManager — but BLEManager owns its own simple history via MQTTManager? We will forward to a shared store via NotificationCenter
    // Instead we publish via objectWillChange and let ContentView also show MQTTManager history; BLE updates will be logged and also mirrored via delegate.

    override init() {
        super.init()

        // Background: bluetooth-central is configured via Info.plist (see project.pbxproj INFOPLIST_KEY_UIBackgroundModes).
        // State restoration requires that background mode to be present, otherwise CBCentralManager throws
        // NSInternalInconsistencyException: "State restoration of CBCentralManager is only allowed for applications that have specified the \"bluetooth-central\" background mode".
        // To avoid crash on fresh installs / before Xcode correctly generates UIBackgroundModes, we start WITHOUT restoration
        // and only enable it when the Info.plist actually contains bluetooth-central (checked at runtime).
        let hasBGMode = Bundle.main.object(forInfoDictionaryKey: "UIBackgroundModes") as? [String] ?? []
        let usesRestore = hasBGMode.contains("bluetooth-central")
        if !usesRestore {
            print("[BLE] Warning: UIBackgroundModes missing bluetooth-central — starting without restoration (enable in Xcode: Signing & Capabilities → Background Modes → Uses Bluetooth LE accessories)")
        } else {
            print("[BLE] UIBackgroundModes contains bluetooth-central — enabling restoration")
        }
        var opts: [String: Any] = [CBCentralManagerOptionShowPowerAlertKey: true]
        if usesRestore {
            opts[CBCentralManagerOptionRestoreIdentifierKey] = "BCICentralManager"
        }
        centralManager = CBCentralManager(delegate: self, queue: nil, options: opts)
        // Observe background/foreground for logging
        NotificationCenter.default.addObserver(self, selector: #selector(didEnterBackground), name: UIApplication.didEnterBackgroundNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(didEnterForeground), name: UIApplication.willEnterForegroundNotification, object: nil)
    }

    @objc private func didEnterBackground() {
        print("[APP] Entered background — BLE isConnected=\(isConnected) status=\(status)")
        if isConnected {
            print("[BLE] Background: remains connected (bluetooth-central), awaiting notifications")
        }
    }
    @objc private func didEnterForeground() {
        print("[APP] Entered foreground — BLE status=\(status)")
    }

    // MARK: - Public

    func startScan(filtered: Bool = true) {
        guard centralManager.state == .poweredOn else {
            print("[BLE] Cannot scan — Bluetooth state: \(centralStateString(centralManager.state))")
            status = "Bluetooth off"
            return
        }
        // Reset list for new scan — also clear stale peripheral name unless already connected
        discoveredList = []
        allDiscovered = [:]
        lastScanCount = 0
        if !isConnected {
            peripheralName = nil
            discoveredPeripheral = nil
        }
        if filtered {
            print("[BLE] Scanning started (filtered service \(BLEUUID.service.uuidString))")
        } else {
            print("[BLE] Scanning started (UNFILTERED — all peripherals, debug)")
        }
        isScanning = true
        status = filtered ? "Scanning..." : "Scanning all..."
        let services: [CBUUID]? = filtered ? [BLEUUID.service] : nil
        centralManager.scanForPeripherals(withServices: services, options: [CBCentralManagerScanOptionAllowDuplicatesKey: false])
        // Timeout after 15s — if filtered and nothing found, suggest unfiltered
        DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in
            guard let self = self, self.isScanning, !self.isConnected else { return }
            print("[BLE] Scan timeout — found \(self.discoveredList.count) device(s)")
            self.stopScan()
            if self.discoveredList.isEmpty {
                self.status = "Not found — try Scan All"
                print("[BLE] Hint: Start ble_peripheral.py on Mac and keep terminal foreground + allow Bluetooth permission. If still not found, tap 'Scan All' to see if Mac is advertising under different name.")
            } else {
                self.status = "Found \(self.discoveredList.count) — tap Connect"
            }
        }
    }

    func startScanAll() { startScan(filtered: false) }

    func stopScan() {
        centralManager.stopScan()
        isScanning = false
        print("[BLE] Scanning stopped — discovered \(discoveredList.count) in this session")
    }

    func connect() {
        guard let p = discoveredPeripheral else {
            print("[BLE] No target peripheral — tap a device in list or Scan BLE again")
            if !discoveredList.isEmpty {
                status = "Select a device from list"
            } else {
                startScan()
            }
            return
        }
        print("[BLE] Connecting to \(p.name ?? "unknown") …")
        status = "Connecting"
        centralManager.connect(p, options: nil)
    }

    func connect(to id: UUID) {
        guard let p = allDiscovered[id] else { return }
        let name = p.name ?? "Unknown"
        print("[BLE] Manual connect to \(name) \(id)")
        discoveredPeripheral = p
        peripheralName = name
        connect()
    }

    func disconnect() {
        if let p = discoveredPeripheral {
            print("[BLE] Disconnecting")
            centralManager.cancelPeripheralConnection(p)
        }
        commandCharacteristic = nil
        status = "Disconnected"
        isConnected = false
    }

    // Test helper: inject fake command without BLE (for UI testing)
    func injectTestCommand(_ cmd: String) {
        print("[BLE] Test inject: \(cmd)")
        handleBLECommandString(cmd)
    }

    // MARK: - Pipeline (reuse existing validation/executor)

    private func handleBLECommandString(_ raw: String) {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        print("[BLE] Received command: \(trimmed)")
        DispatchQueue.main.async { self.lastBLECommand = trimmed }

        // Convert simple string to CommandMessage via same alias logic
        // SEARCH is now a distinct command with query field
        let typed = CommandType.resolveAlias(trimmed) ?? CommandType(rawValue: trimmed.uppercased()) ?? .unknown
        // For BLE first test, use confidence 1.0 and now timestamp
        let msg = CommandMessage(
            command: CommandType.resolve(raw: trimmed, normalized: nil),
            rawCommand: trimmed,
            confidence: 1.0,
            timestamp: Date().timeIntervalSince1970,
            url: nil
        )
        // Validate via existing validator
        let validation = validator.validate(msg)
        print("[COMMAND] Validated: \(typed.rawValue) — \(validation.displayString)")
        let execution = executor.execute(msg, validation: validation)
        print("[BLE] Execution: \(execution.displayString)")
        if validation.isValid {
            print("[BLE] → Shortcut mapping via CommandExecutor logged above")
        }
        // Post to MQTTManager history via NotificationCenter so both transports share history
        NotificationCenter.default.post(name: .bleCommandProcessed, object: nil, userInfo: [
            "raw": trimmed,
            "validation": validation,
            "execution": execution,
            "timestamp": Date()
        ])
    }

    private func centralStateString(_ state: CBManagerState) -> String {
        switch state {
        case .unknown: return "unknown"
        case .resetting: return "resetting"
        case .unsupported: return "unsupported"
        case .unauthorized: return "unauthorized"
        case .poweredOff: return "poweredOff"
        case .poweredOn: return "poweredOn"
        @unknown default: return "unknown(\(state.rawValue))"
        }
    }
}

// MARK: - CBCentralManagerDelegate, CBPeripheralDelegate

extension BLEManager: CBCentralManagerDelegate {
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        print("[BLE] Bluetooth state: \(centralStateString(central.state))")
        if central.state == .poweredOn {
            print("[BLE] Ready to scan (bluetooth-central background mode enabled)")
            if central.state == .poweredOn && status == "Disconnected" {
                // Optional auto-scan hint
            }
        } else {
            status = centralStateString(central.state)
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral, advertisementData: [String : Any], rssi RSSI: NSNumber) {
        let name = peripheral.name ?? (advertisementData[CBAdvertisementDataLocalNameKey] as? String) ?? "Unknown"
        let isOurService = (advertisementData[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID])?.contains(BLEUUID.service) ?? false
        print("[BLE] Discovered: \(name) — \(peripheral.identifier.uuidString) RSSI \(RSSI) services:\(advertisementData[CBAdvertisementDataServiceUUIDsKey] ?? "none") isOur=\(isOurService)")
        // Keep list for dashboard
        if allDiscovered[peripheral.identifier] == nil {
            allDiscovered[peripheral.identifier] = peripheral
            let entry = (name: name, id: peripheral.identifier, rssi: RSSI.intValue)
            DispatchQueue.main.async {
                self.discoveredList.append(entry)
                self.lastScanCount = self.discoveredList.count
            }
        }
        // If this peripheral advertises our service, auto-pick it
        if isOurService || name == "BCI-Mac-Gateway" {
            print("[BLE] ★ Target peripheral found — auto-connecting")
            discoveredPeripheral = peripheral
            peripheralName = name
            stopScan()
            status = "Discovered: \(name)"
            connect()
            return
        }
        // Otherwise keep scanning — DO NOT auto-pick non-target
        // Keep the first non-target only for debug list, not for auto-connect
        // Do not set discoveredPeripheral/peripheralName for non-target to avoid "Peripheral: Unknown" confusion
        DispatchQueue.main.async {
            self.status = "Scanning... (\(self.discoveredList.count) found, no target yet)"
        }
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        print("[BLE] Connected to \(peripheral.name ?? "peripheral")")
        isConnected = true
        status = "Connected"
        peripheral.delegate = self
        peripheral.discoverServices([BLEUUID.service])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        print("[BLE] Failed to connect: \(error?.localizedDescription ?? "unknown")")
        status = "Failed"
        isConnected = false
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        print("[BLE] Disconnected (\(error?.localizedDescription ?? "no error"))")
        status = "Disconnected"
        isConnected = false
        commandCharacteristic = nil
        // Optionally auto-reconnect in background? For now leave disconnected.
    }

    func centralManager(_ central: CBCentralManager, willRestoreState dict: [String : Any]) {
        print("[BLE] willRestoreState (background wake): \(dict.keys)")
        if let peripherals = dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral] {
            for p in peripherals {
                discoveredPeripheral = p
                p.delegate = self
                print("[BLE] Restored peripheral: \(p.name ?? "?")")
                // Re-subscribe will happen after state restoration
            }
        }
    }
}

extension BLEManager: CBPeripheralDelegate {
    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        if let e = error { print("[BLE] Service discovery error: \(e)"); return }
        guard let services = peripheral.services else { print("[BLE] No services"); return }
        for s in services {
            print("[BLE] Service discovered: \(s.uuid)")
            if s.uuid == BLEUUID.service {
                peripheral.discoverCharacteristics([BLEUUID.commandCharacteristic], for: s)
            }
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        if let e = error { print("[BLE] Characteristic discovery error: \(e)"); return }
        guard let chars = service.characteristics else { return }
        for c in chars {
            print("[BLE] Characteristic discovered: \(c.uuid) properties: \(c.properties)")
            if c.uuid == BLEUUID.commandCharacteristic {
                commandCharacteristic = c
                print("[BLE] Notifications enabled for \(c.uuid)")
                peripheral.setNotifyValue(true, for: c)
                status = "Connected — Ready"
                // Also read current value if any
                peripheral.readValue(for: c)
            }
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        if let e = error { print("[BLE] Update error: \(e)"); return }
        guard characteristic.uuid == BLEUUID.commandCharacteristic else { return }
        guard let data = characteristic.value else { print("[BLE] Empty data"); return }
        if let str = String(data: data, encoding: .utf8) {
            handleBLECommandString(str)
        } else {
            print("[BLE] Non-UTF8 data: \(data.hexString())")
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        if let e = error { print("[BLE] Notify state error: \(e)"); return }
        print("[BLE] Notify \(characteristic.uuid) isNotifying=\(characteristic.isNotifying)")
    }
}

private extension Data {
    func hexString() -> String { map { String(format:"%02x",$0)}.joined() }
}

extension Notification.Name {
    static let bleCommandProcessed = Notification.Name("bleCommandProcessed")
}