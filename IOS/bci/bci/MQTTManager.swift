import Foundation
import CocoaMQTT
import Combine

enum ConnectionState: Equatable {
    case offline
    case connecting
    case online
}

class MQTTManager: ObservableObject {
    @Published var isConnected = false
    @Published var connectionState: ConnectionState = .offline
    @Published var lastCommand: CommandMessage?
    @Published var statusMessage = "Disconnected"
    @Published var isRegistrationApproved = false
    @Published var registrationConflictMessage: String?

    // SEARCH state (direct in-app search, no Shortcut).
    // One-way flow: SEARCH auto-opens the first valid result; no user selection.
    @Published var searchState: SearchState = .idle
    @Published var searchQuery: String?
    @Published var searchOpenedSong: JioSaavnSong?

    private var mqtt: CocoaMQTT?
    private let validator = CommandValidator()
    private lazy var executor: CommandExecutor = CommandExecutor(mqttManager: self)
    private let jsonDecoder = JSONDecoder()
    private let identity = DeviceIdentity.shared

    // Broker settings (host/port fixed; topics are per-user dynamic).
    private let host = "52.21.249.6"
    private let port: UInt16 = 1883

    /// Active per-device command topic for this session (nil until connected).
    private var activeCommandTopic: String?
    private var activeUsername: String = ""

    // Legacy topic — subscribed only as fallback so transitional servers
    // still reach this client; accepted only when no username is set.
    private let legacyTopic = "aiml/commands"

    var deviceID: String { identity.deviceID }
    var username: String { identity.normalizedUsername }

    // MARK: - Sub-Master server presence (aiml/status)
    //
    // The server publishes retained {"status":"ONLINE"} on aiml/status at
    // connect, a non-retained ONLINE heartbeat every 30s, and its MQTT
    // last-will is {"status":"OFFLINE"}. No other client publishes here.
    @Published var isServerOnline = false
    private let serverStatusTopic = "aiml/status"
    private var lastServerHeartbeat: Date?
    private var serverWatchdog: Timer?
    private static let serverOfflineAfter: TimeInterval = 45

    // MARK: - Connect / Disconnect (per-device, mirrors Android)

    /// Connect with the typed Device ID. Publishes aiml/devices/register
    /// {device_id, username, device_type: ios} then subscribes to
    /// aiml/<username>/commands (+ status/ack/media).
    func connect(username rawName: String? = nil) {
        if let rawName {
            identity.saveUsername(rawName)
        }
        guard identity.isUsernameValid else {
            statusMessage = "Enter Device ID first"
            return
        }
        if let mqtt = mqtt, mqtt.connState == .connected || mqtt.connState == .connecting {
            print("[MQTT] Already connected or connecting")
            return
        }

        mqtt?.disconnect()
        mqtt = nil

        let user = identity.normalizedUsername
        activeCommandTopic = identity.commandTopic ?? "aiml/\(user)/commands"
        activeUsername = user

        // Stable client ID per phone (not random each launch).
        let short = String(identity.deviceID.prefix(6))
        let client = CocoaMQTT(clientID: "iOS-\(short)", host: host, port: port)
        client.keepAlive = 60
        client.autoReconnect = true
        client.autoReconnectTimeInterval = 5
        client.delegate = self
        client.logLevel = .debug

        self.mqtt = client
        isRegistrationApproved = false
        registrationConflictMessage = nil
        connectionState = .connecting
        statusMessage = "Connecting..."
        isConnected = false
        isServerOnline = false
        lastServerHeartbeat = nil

        _ = client.connect()
    }

    func disconnect(publishOfflineStatus: Bool = true) {
        guard mqtt != nil else {
            connectionState = .offline
            if registrationConflictMessage == nil {
                statusMessage = "Disconnected"
            }
            return
        }
        if publishOfflineStatus && registrationConflictMessage == nil {
            publishOffline()
        }
        // Unsubscribe per-user topics before dropping the client.
        if let cmd = activeCommandTopic { mqtt?.unsubscribe(cmd) }
        mqtt?.unsubscribe(serverStatusTopic)
        mqtt?.unsubscribe(identity.registerResponseTopic)
        if !activeUsername.isEmpty {
            mqtt?.unsubscribe("aiml/\(activeUsername)/status")
            mqtt?.unsubscribe("aiml/\(activeUsername)/ack")
            mqtt?.unsubscribe("aiml/\(activeUsername)/media")
        }
        mqtt?.autoReconnect = false
        mqtt?.disconnect()
        mqtt = nil
        activeCommandTopic = nil
        stopServerWatchdog()
        lastServerHeartbeat = nil
        isServerOnline = false
        isConnected = false
        connectionState = .offline
        if registrationConflictMessage == nil {
            statusMessage = "Disconnected"
        }
    }

    // MARK: - Presence / ACK (per-user topics)

    private func publishOnline() {
        guard let mqtt, !activeUsername.isEmpty else { return }
        // 1. Device registration (stable UUID + user-entered Device ID).
        if let data = try? JSONSerialization.data(
            withJSONObject: identity.registrationPayload()),
            let json = String(data: data, encoding: .utf8) {
            mqtt.publish(DeviceIdentity.discoveryTopic, withString: json,
                         qos: .qos1, retained: true)
            print("[MQTT] Published register: \(json)")
        }
        // 2. ONLINE presence on per-user status.
        mqtt.publish("aiml/\(activeUsername)/status",
                     withString: "{\"status\":\"ONLINE\"}", qos: .qos1, retained: true)
    }

    private func publishOffline() {
        guard let mqtt, !activeUsername.isEmpty else { return }
        mqtt.publish("aiml/\(activeUsername)/status",
                     withString: "{\"status\":\"OFFLINE\"}", qos: .qos1, retained: true)
    }

    /// ACK back to server on aiml/<username>/ack so dashboard shows EXECUTED.
    func publishAck(command: String) {
        guard let mqtt, !activeUsername.isEmpty else { return }
        let payload = "{\"status\":\"EXECUTED\",\"command\":\"\(command)\"}"
        mqtt.publish("aiml/\(activeUsername)/ack", withString: payload, qos: .qos1)
    }

    // MARK: - Server presence (aiml/status)

    /// Update Sub-Master online state from a aiml/status payload.
    /// Accepts {"status":"ONLINE"} JSON (what the server sends) or a raw token.
    private func handleServerPresence(_ payload: String?) {
        guard let raw = payload?.trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty else { return }
        var status: String?
        if let data = raw.data(using: .utf8),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            status = json["status"] as? String
        }
        let upper = (status ?? raw).uppercased()
        let online = ["ONLINE", "CONNECTED", "READY", "1", "TRUE"].contains(upper)
        let offline = ["OFFLINE", "DISCONNECTED", "0", "FALSE"].contains(upper)
        guard online || offline else { return }
        DispatchQueue.main.async {
            if online { self.lastServerHeartbeat = Date() }
            if self.isServerOnline != online {
                self.isServerOnline = online
                print("[SERVER] Sub-Master \(online ? "ONLINE" : "OFFLINE")")
            }
        }
    }

    private func startServerWatchdog() {
        stopServerWatchdog()
        // Timer must be created on the main runloop (delegate callbacks are background).
        DispatchQueue.main.async {
            self.serverWatchdog = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
                self?.checkServerLiveness()
            }
        }
    }

    private func stopServerWatchdog() {
        serverWatchdog?.invalidate()
        serverWatchdog = nil
    }

    /// Mark the server offline if its 30s heartbeat has gone quiet.
    /// Covers graceful server stops (no last-will fires then); the retained
    /// ONLINE message can otherwise look stale-fresh on (re)subscribe.
    private func checkServerLiveness() {
        guard isConnected else { return }  // broker down: connection card covers it
        if let last = lastServerHeartbeat,
           Date().timeIntervalSince(last) < Self.serverOfflineAfter { return }
        if isServerOnline {
            isServerOnline = false
            print("[SERVER] heartbeat lost -> OFFLINE")
        }
    }

    // MARK: - SEARCH helpers (in-app, no Shortcut)

    func handleSearchStarted(query: String) {
        searchQuery = query
        searchOpenedSong = nil
        searchState = .searching
        statusMessage = "Searching JioSaavn..."
    }

    /// Starts the same search path used by MQTT, BLE, and APNs commands.
    /// An empty field intentionally searches the static default song.
    func searchAndPlay(query: String) {
        let message = CommandMessage(
            command: .SEARCH,
            rawCommand: "SEARCH_AND_PLAY",
            confidence: 1.0,
            timestamp: Date().timeIntervalSince1970,
            query: query
        )
        lastCommand = message
        let validation = validator.validate(message)
        _ = executor.execute(message, validation: validation)
    }

    func handleSearchSuccess(query: String, openedSong: JioSaavnSong?, hasResults: Bool) {
        searchQuery = query
        searchOpenedSong = openedSong
        if openedSong != nil {
            searchState = .success
            statusMessage = "Opening JioSaavn..."
        } else if hasResults {
            searchState = .success
            statusMessage = "No playable result found"
        } else {
            searchState = .noResults
            statusMessage = "No results found"
        }
        print("[SEARCH] Success for '\(query)' opened=\(openedSong?.title ?? "none")")
    }

    func handleSearchFailure(query: String) {
        searchQuery = query
        searchOpenedSong = nil
        searchState = .failure
        statusMessage = "Search failed"
        print("[SEARCH] Failure for '\(query)'")
    }

    func openSongInJioSaavn(_ song: JioSaavnSong) {
        let opened = JioSaavnService().openSong(song)
        print("[SEARCH] openSong title=\(song.title ?? "nil") opened=\(opened)")
    }
}

extension MQTTManager: CocoaMQTTDelegate {

    func mqtt(_ mqtt: CocoaMQTT, didConnectAck ack: CocoaMQTTConnAck) {
        if ack == .accept {
            DispatchQueue.main.async {
                self.isConnected = true
                self.connectionState = .online
                self.statusMessage = "Ready"
            }
            // Direct registration response channel (UUID-addressed).
            mqtt.subscribe(identity.registerResponseTopic, qos: .qos1)
            print("[MQTT] Subscribed to \(identity.registerResponseTopic)")

            // Per-user command channel + legacy fallback.
            if let cmd = activeCommandTopic {
                mqtt.subscribe(cmd, qos: .qos1)
                print("[MQTT] Connected, subscribed to \(cmd)")
            }
            mqtt.subscribe(legacyTopic, qos: .qos1)
            // Sub-Master server presence (retained ONLINE arrives immediately if up).
            mqtt.subscribe(serverStatusTopic, qos: .qos1)
            if !activeUsername.isEmpty {
                mqtt.subscribe("aiml/\(activeUsername)/status", qos: .qos1)
                mqtt.subscribe("aiml/\(activeUsername)/ack", qos: .qos1)
                mqtt.subscribe("aiml/\(activeUsername)/media", qos: .qos1)
            }
            startServerWatchdog()
            publishOnline()
        } else {
            DispatchQueue.main.async {
                self.connectionState = .offline
                self.statusMessage = "Connection failed"
            }
        }
    }

    func mqtt(_ mqtt: CocoaMQTT, didReceiveMessage message: CocoaMQTTMessage, id: UInt16) {
        // Direct registration response from server (UUID-addressed).
        if message.topic == identity.registerResponseTopic {
            handleRegistrationResponse(message.string)
            return
        }
        // Sub-Master server presence comes next (not a device command topic).
        if message.topic == serverStatusTopic {
            handleServerPresence(message.string)
            return
        }
        // Per-device isolation: only act on our own command topic.
        // Legacy topic accepted only when no Device ID is set (transition).
        let topic = message.topic
        if let expected = activeCommandTopic, !expected.isEmpty {
            guard topic == expected else {
                print("[MQTT] Ignored message on \(topic) (expecting \(expected)) — different ID")
                return
            }
        } else if topic != legacyTopic {
            print("[MQTT] Ignored message on \(topic) — no active device ID")
            return
        }
        guard let payload = message.string else { return }

        print("[MQTT] Received: \(payload)")

        guard let data = payload.data(using: .utf8) else {
            print("[Decode] FAILED: not UTF-8")
            return
        }

        do {
            let decoded = try jsonDecoder.decode(CommandMessage.self, from: data)
            print("[COMMAND] Decoded: \(decoded.command.rawValue) query=\(decoded.query ?? "nil")")
            let validation = validator.validate(decoded)
            print("[Validation] \(validation.displayString)")
            let execution = executor.execute(decoded, validation: validation)
            print("[Execution] \(execution.displayString)")
            if case .executed(let cmd, _) = execution {
                self.publishAck(command: cmd.rawValue)
            }

            DispatchQueue.main.async {
                self.lastCommand = decoded
                if decoded.command != .SEARCH {
                    self.statusMessage = validation.isValid ? "" : "Command not recognized"
                }
                // SEARCH status is driven by handleSearchStarted/Success/Failure.
            }
        } catch {
            // Accept plain command tokens as well as JSON.
            let raw = payload.trimmingCharacters(in: .whitespacesAndNewlines)
            if let command = CommandType.resolveAlias(raw) {
                let decoded = CommandMessage(
                    command: command,
                    rawCommand: raw,
                    confidence: 1.0,
                    timestamp: Date().timeIntervalSince1970
                )
                let validation = validator.validate(decoded)
                let execution = executor.execute(decoded, validation: validation)
                print("[Decode] DIRECT command=\(raw) \(execution.displayString)")
                if case .executed(let cmd, _) = execution {
                    self.publishAck(command: cmd.rawValue)
                }
                DispatchQueue.main.async {
                    self.lastCommand = decoded
                    if decoded.command != .SEARCH {
                        self.statusMessage = validation.isValid ? "" : "Command not recognized"
                    }
                }
                return
            }
            print("[Decode] FAILED: \(error.localizedDescription)")
            DispatchQueue.main.async {
                self.lastCommand = nil
                self.statusMessage = "Could not read command"
            }
        }
    }

    func mqttDidDisconnect(_ mqtt: CocoaMQTT, withError err: Error?) {
        stopServerWatchdog()
        DispatchQueue.main.async {
            self.isConnected = false
            self.isServerOnline = false
            self.lastServerHeartbeat = nil
            self.connectionState = .offline
            if self.registrationConflictMessage == nil {
                self.statusMessage = "Disconnected"
            }
        }
        if let err = err {
            print("[MQTT] Disconnected: \(err.localizedDescription)")
        }
    }

    // MARK: - Registration Response (aiml/<deviceID>/register_response)

    private func handleRegistrationResponse(_ payload: String?) {
        guard let payload = payload,
              let data = payload.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }

        let respDeviceId = json["device_id"] as? String ?? ""
        if !respDeviceId.isEmpty && respDeviceId.lowercased() != identity.deviceID.lowercased() {
            print("[REGISTRATION] Ignored response for other device ID: \(respDeviceId)")
            return
        }

        let status = (json["status"] as? String ?? "").uppercased()
        let error = (json["error"] as? String ?? "").uppercased()
        let message = json["message"] as? String ?? ""
        let username = json["username"] as? String ?? activeUsername

        if status == "ERROR" || error == "USERNAME_TAKEN" {
            let conflictMsg = message.isEmpty
                ? "The username '\(username)' is already in use by another device. Please choose a different username."
                : message
            print("[REGISTRATION REJECTED] \(conflictMsg)")
            DispatchQueue.main.async {
                self.isRegistrationApproved = false
                self.registrationConflictMessage = conflictMsg
                self.statusMessage = "Name conflict"
                self.disconnect(publishOfflineStatus: false)
            }
        } else if status == "SUCCESS" {
            print("[REGISTRATION APPROVED] Device verified as '\(username)'.")
            DispatchQueue.main.async {
                self.isRegistrationApproved = true
                self.registrationConflictMessage = nil
                self.statusMessage = "Device verified as '\(username)'"
            }
        }
    }

    // Required empty methods
    func mqtt(_ mqtt: CocoaMQTT, didSubscribeTopics success: NSDictionary, failed: [String]) {}
    func mqtt(_ mqtt: CocoaMQTT, didUnsubscribeTopics topics: [String]) {}
    func mqtt(_ mqtt: CocoaMQTT, didPublishMessage message: CocoaMQTTMessage, id: UInt16) {}
    func mqtt(_ mqtt: CocoaMQTT, didPublishAck id: UInt16) {}
    func mqttDidPing(_ mqtt: CocoaMQTT) {}
    func mqttDidReceivePong(_ mqtt: CocoaMQTT) {}
}
