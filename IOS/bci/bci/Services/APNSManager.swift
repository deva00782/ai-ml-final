import Foundation
import UIKit
import Combine

/// Manages APNs registration and token delivery to gateway.
final class APNSManager: NSObject, ObservableObject {
    static let shared = APNSManager()

    @Published var deviceTokenHex: String?
    @Published var registrationStatus: String = "Not registered"
    @Published var lastRegistrationError: String?

    // Gateway URL - update to your PC's LAN IP when testing.
    // For prototype: keep as UserDefaults so UI can change it.
    var gatewayBaseURL: String {
        get { UserDefaults.standard.string(forKey: "gatewayBaseURL") ?? "http://localhost:8080" }
        set { UserDefaults.standard.set(newValue, forKey: "gatewayBaseURL") }
    }

    private override init() { super.init() }

    func register() {
        print("[APNS] Registration started")
        DispatchQueue.main.async {
            self.registrationStatus = "Registering..."
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    func didRegister(token: Data) {
        let hex = token.map { String(format: "%02x", $0) }.joined()
        // Log only prefix for safety
        let prefix = String(hex.prefix(8))
        print("[APNS] Device token received: \(prefix)... (\(hex.count) chars)")
        print("[APNS] Full token NOT logged for security")

        DispatchQueue.main.async {
            self.deviceTokenHex = hex
            self.registrationStatus = "Token received"
            self.lastRegistrationError = nil
        }
        // Auto-send to gateway
        sendTokenToGateway(hex: hex)
    }

    func didFail(error: Error) {
        let msg = error.localizedDescription
        print("[APNS] Registration failed: \(msg)")
        // Personal team cannot create Push profile -> aps-environment missing
        if msg.contains("aps-environment") {
            print("[APNS] Personal team detected - Push Notifications requires Apple Developer Program ($99/yr)")
            print("[APNS] Fix: Use Direct MQTT mode for now. To enable APNs, use paid team + re-enable bci.entitlements")
            DispatchQueue.main.async {
                self.registrationStatus = "Unavailable (Personal team)"
                self.lastRegistrationError = "Push requires paid Apple Developer Program. Switch to Direct MQTT mode - MQTT→Shortcut still works. To enable APNs: enroll Paid Program → move bci.entitlements.disabled → bci.entitlements and re-add Push capability in Xcode."
                // Auto-switch to Direct MQTT to avoid confusion
                if UserDefaults.standard.string(forKey: "commandDeliveryMode") == "apns" {
                    UserDefaults.standard.set("direct_mqtt", forKey: "commandDeliveryMode")
                    print("[APNS] Auto-switched deliveryMode to direct_mqtt")
                }
            }
            return
        }
        DispatchQueue.main.async {
            self.registrationStatus = "Failed"
            self.lastRegistrationError = msg
        }
    }

    func sendTokenToGateway(hex: String) {
        let base = gatewayBaseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !base.isEmpty, let url = URL(string: base + "/register-device") else {
            print("[APNS] Invalid gateway URL: \(base)")
            DispatchQueue.main.async { self.registrationStatus = "Invalid gateway URL" }
            return
        }
        print("[APNS] Registering token with gateway: \(url.absoluteString)")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: String] = [
            "device_token": hex,
            "environment": "development"
        ]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req) { data, resp, err in
            if let err = err {
                print("[APNS] Gateway registration error: \(err.localizedDescription)")
                DispatchQueue.main.async { self.registrationStatus = "Gateway error: \(err.localizedDescription)" }
                return
            }
            if let http = resp as? HTTPURLResponse {
                print("[APNS] Gateway response: \(http.statusCode)")
                if http.statusCode == 200 {
                    print("[APNS] Device token registered with gateway")
                    DispatchQueue.main.async { self.registrationStatus = "Registered with gateway" }
                } else {
                    let bodyStr = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                    print("[APNS] Gateway registration failed: \(http.statusCode) \(bodyStr)")
                    DispatchQueue.main.async { self.registrationStatus = "Gateway: \(http.statusCode)" }
                }
            }
        }.resume()
    }
}