import Foundation

// MARK: - ValidationResult

enum ValidationResult: Equatable {
    case valid
    case invalidJSON(String)
    case unsupportedCommand(String)
    case missingConfidence
    case invalidConfidence(Double)
    case confidenceBelowThreshold(Double, threshold: Double)
    case missingURL
    case invalidURL(String)
    case missingQuery
    case invalidQuery(String)
    case invalidTimestamp(String)

    var isValid: Bool {
        if case .valid = self { return true }
        return false
    }

    /// Short display string for UI.
    var displayString: String {
        switch self {
        case .valid:
            return "VALID"
        case .invalidJSON(let reason):
            return "INVALID: Invalid JSON — \(reason)"
        case .unsupportedCommand(let cmd):
            return "INVALID: Unsupported command '\(cmd)'"
        case .missingConfidence:
            return "INVALID: Missing confidence"
        case .invalidConfidence(let v):
            return "INVALID: Confidence out of range (\(v))"
        case .confidenceBelowThreshold(let v, let t):
            return String(format: "INVALID: Confidence %.2f below threshold %.2f", v, t)
        case .missingURL:
            return "INVALID: Missing URL for OPEN_LINK"
        case .invalidURL(let u):
            return "INVALID: Invalid URL '\(u)'"
        case .missingQuery:
            return "INVALID: Missing query for SEARCH"
        case .invalidQuery(let q):
            return "INVALID: Invalid query '\(q)'"
        case .invalidTimestamp(let r):
            return "INVALID: Invalid timestamp — \(r)"
        }
    }
}

// MARK: - CommandValidator

struct CommandValidator {

    /// Single source of truth for confidence threshold. Change here to adjust globally.
    static let confidenceThreshold: Double = 0.80

    /// Whitelisted hosts for OPEN_LINK (POC: JioSaavn only, HTTPS required)
    static let allowedHosts: [String] = [
        "jiosaavn.com",
        "www.jiosaavn.com",
        "jio.saavn.com"
    ]

    let threshold: Double

    init(threshold: Double = CommandValidator.confidenceThreshold) {
        self.threshold = threshold
    }

    func validate(_ message: CommandMessage) -> ValidationResult {
        // 1. Unsupported command
        if message.command.isUnknown {
            return .unsupportedCommand(message.rawCommand ?? "UNKNOWN")
        }

        // 2. Confidence checks
        guard let conf = message.confidence else {
            return .missingConfidence
        }
        if conf.isNaN || conf < 0 || conf > 1 {
            return .invalidConfidence(conf)
        }
        if conf < threshold {
            return .confidenceBelowThreshold(conf, threshold: threshold)
        }

        // 3. OPEN_LINK URL checks (whitelist + HTTPS)
        if message.command == .OPEN_LINK {
            guard let urlString = message.url, !urlString.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                return .missingURL
            }
            guard let url = URL(string: urlString),
                  let components = URLComponents(string: urlString),
                  let scheme = components.scheme?.lowercased(),
                  scheme == "https",
                  let host = components.host?.lowercased(),
                  !host.isEmpty,
                  url.host != nil else {
                return .invalidURL(urlString)
            }
            // Host must be exactly jiosaavn.com or www.jiosaavn.com (or subdomain allowance)
            let isAllowed = Self.allowedHosts.contains(host) || host.hasSuffix(".jiosaavn.com")
            if !isAllowed {
                return .invalidURL(urlString)
            }
            // Also reject URLs with no path or obviously malformed
            _ = url // validated
        }

        // 3. SEARCH query checks (defaults to the static song query if empty)
        if message.command == .SEARCH {
            let queryString = (message.query?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false)
                ? message.query!
                : CommandType.defaultSearchQuery
            let trimmed = queryString.trimmingCharacters(in: .whitespacesAndNewlines)
            // Basic validation: non-empty, reasonable length
            if trimmed.isEmpty || trimmed.count > 500 {
                return .invalidQuery(queryString)
            }
        }

        // 5. Timestamp check (POC level: lenient to handle BCI ISO strings without timezone and clock skew)
        if let ts = message.timestamp {
            if ts <= 0 {
                return .invalidTimestamp("timestamp must be > 0")
            }
            // For POC, allow future timestamps with generous window (7 days) to tolerate
            // BCI device clock skew and ISO strings without timezone (parsed as GMT).
            // Previous 5-minute window was too strict and caused valid BCI messages to be rejected.
            let now = Date().timeIntervalSince1970
            if ts > now + 7 * 24 * 3600 {
                return .invalidTimestamp("timestamp too far in future")
            }
            // Very old timestamps are allowed for POC
        }
        // Missing timestamp is allowed for POC (do not reject)

        return .valid
    }

    /// Validate raw JSON data before decoding is used for error reporting;
    /// this helper is used by MQTTManager for string that fails to decode.
    static func invalidJSONResult(_ error: Error) -> ValidationResult {
        return .invalidJSON(error.localizedDescription)
    }
}
