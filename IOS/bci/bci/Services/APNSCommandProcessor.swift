import Foundation
import UIKit

/// Reuses existing validation/execution pipeline for APNs payloads.
final class APNSCommandProcessor {
    static let shared = APNSCommandProcessor()

    private let validator = CommandValidator()
    private let executor = CommandExecutor()

    private init() {}

    /// Process APNs userInfo dictionary. Expected keys:
    ///  bci_command, confidence, timestamp, url
    ///  plus aps.content-available
    func handle(userInfo: [AnyHashable: Any], completion: @escaping (UIBackgroundFetchResult) -> Void) {
        print("[APNS] Background notification received")
        print("[APNS] userInfo: \(userInfo)")

        // Extract BCI command
        guard let rawCmd = userInfo["bci_command"] as? String else {
            print("[APNS] No bci_command in payload")
            completion(.noData)
            return
        }
        let confidence = userInfo["confidence"] as? Double ?? (userInfo["confidence"] as? NSNumber)?.doubleValue
        let timestampRaw = userInfo["timestamp"]
        let url = userInfo["url"] as? String

        print("[APNS] BCI command: \(rawCmd)")
        if let c = confidence { print("[APNS] Confidence: \(c)") }
        print("[APNS] Timestamp raw: \(String(describing: timestampRaw))")

        // Build CommandMessage payload for validation
        // Try to preserve original timestamp handling: if timestamp is string, parse similarly
        var timestamp: TimeInterval?
        if let tStr = timestampRaw as? String {
            timestamp = CommandMessage.parseTimestampString(tStr)
            if timestamp == nil, let d = Double(tStr) { timestamp = d }
        } else if let tDouble = timestampRaw as? Double {
            timestamp = tDouble
        } else if let tInt = timestampRaw as? Int {
            timestamp = TimeInterval(tInt)
        } else if let tNum = timestampRaw as? NSNumber {
            timestamp = tNum.doubleValue
        }

        // Map to canonical via same alias logic
        let normalized = rawCmd // for APNs we use bci_command as raw, no separate normalized
        let typed = CommandType.resolve(raw: rawCmd, normalized: nil)

        // Special default URL for Open JioSaavn alias
        var finalURL = url
        if typed == .OPEN_LINK && (finalURL == nil || finalURL?.isEmpty == true) {
            if rawCmd.uppercased().contains("JIOSAAVN") {
                finalURL = "https://www.jiosaavn.com"
            }
        }

        let msg = CommandMessage(
            command: typed,
            rawCommand: rawCmd,
            confidence: confidence,
            timestamp: timestamp,
            url: finalURL,
            normalizedCommand: nil,
            label: nil
        )

        let validation = validator.validate(msg)
        print("[APNS] Validation: \(validation.displayString)")
        print("[COMMAND] Validated: \(typed.rawValue) — \(validation.displayString)")

        let execution = executor.execute(msg, validation: validation)
        print("[APNS] Execution: \(execution.displayString)")

        // For background, we need to log shortcut attempt result
        // CommandExecutor already triggers ShortcutService via Task, which will log [SHORTCUT]...
        // Give a short delay to allow that log before completion?
        // But per Apple, we must call completion promptly (30 sec limit).
        // Just complete with newData if valid, noData if invalid.
        if validation.isValid {
            // If shortcut was launched in background, it may be throttled
            print("[SHORTCUT] Attempting background shortcut: \(ShortcutService.shortcutName(for: typed) ?? "unknown")")
            completion(.newData)
        } else {
            completion(.noData)
        }
    }
}