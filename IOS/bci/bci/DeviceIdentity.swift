import Foundation
import UIKit
import Combine

/// Persistent device identity mirroring Android BCI_3 UUID flow.
///
/// - `deviceID`: stable per-phone UUID, generated once and stored in
///   UserDefaults (Android uses SharedPreferences). Survives restarts;
///   username renames do NOT create a new identity.
/// - `username`: user-typed ID (same string typed on server active_device).
///   Only the APK whose username == server active_device acts on commands.
final class DeviceIdentity: ObservableObject {
    static let shared = DeviceIdentity()

    private static let uuidKey = "bci.device.uuid"
    private static let usernameKey = "bci.device.username"

    @Published var username: String = ""
    let deviceID: String

    init() {
        let defaults = UserDefaults.standard
        if let stored = defaults.string(forKey: Self.uuidKey), !stored.isEmpty {
            deviceID = stored
        } else {
            // Prefer vendor ID so reinstall on same phone is stable-ish,
            // fallback to random UUID persisted from now on.
            let fresh = UIDevice.current.identifierForVendor?.uuidString
                ?? UUID().uuidString
            defaults.set(fresh, forKey: Self.uuidKey)
            deviceID = fresh
        }
        username = defaults.string(forKey: Self.usernameKey) ?? ""
    }

    /// Normalized channel name: aiml/<username>/...
    var normalizedUsername: String {
        username.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    var isUsernameValid: Bool { !normalizedUsername.isEmpty }

    func saveUsername(_ name: String) {
        let norm = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        username = norm
        UserDefaults.standard.set(norm, forKey: Self.usernameKey)
    }

    // MARK: - Topics (per-user, UUID-gated)

    var commandTopic: String? {
        guard isUsernameValid else { return nil }
        return "aiml/\(normalizedUsername)/commands"
    }
    var statusTopic: String? {
        guard isUsernameValid else { return nil }
        return "aiml/\(normalizedUsername)/status"
    }
    var ackTopic: String? {
        guard isUsernameValid else { return nil }
        return "aiml/\(normalizedUsername)/ack"
    }
    var mediaTopic: String? {
        guard isUsernameValid else { return nil }
        return "aiml/\(normalizedUsername)/media"
    }

    /// Direct registration validation response topic from server: aiml/<device_id>/register_response
    var registerResponseTopic: String {
        "aiml/\(deviceID)/register_response"
    }

    static let discoveryTopic = "aiml/devices/register"

    /// Payload matching Android: {"device_id","username","device_type":"ios"}
    func registrationPayload() -> [String: String] {
        ["device_id": deviceID,
         "username": normalizedUsername,
         "device_type": "ios"]
    }
}