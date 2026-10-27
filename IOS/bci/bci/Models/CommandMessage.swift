import Foundation

// MARK: - CommandType

/// Supported BCI commands. Unknown values decode to `.unknown` instead of throwing.
enum CommandType: String, Codable, CaseIterable, Equatable {
    case PLAY
    case PAUSE
    case NEXT
    case PREVIOUS
    case SEARCH
    case VOLUME_UP
    case VOLUME_DOWN
    case OPEN_LINK
    case unknown = "UNKNOWN"

    static let defaultSearchQuery = "Inaam"

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = try container.decode(String.self)
        // Case-sensitive match; unknown strings become .unknown
        // Also try alias resolution for BCI device variants
        self = CommandType.resolveAlias(raw) ?? CommandType(rawValue: raw.uppercased()) ?? .unknown
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(self.rawValue)
    }

    /// Human readable flag for unsupported commands.
    var isUnknown: Bool { self == .unknown }

    /// Alias resolver for BCI device payloads that use verbose names.
    /// Maps normalized aliases to canonical CommandType.
    static func resolveAlias(_ raw: String) -> CommandType? {
        let upper = raw.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        // Direct canonical
        if let direct = CommandType(rawValue: upper), direct != .unknown {
            return direct
        }
        switch upper {
        case "RIGHT", "RIGHT_SEARCH", "SEARCH_AND_PLAY", "RIGHT_SEARCH_AND_PLAY":
            return .SEARCH
        case "RIGHT+PUSH":
            return .VOLUME_UP
        case "RIGHT+PULL":
            return .VOLUME_DOWN
        case "LEFT":
            return .PLAY
        case "PUSH":
            return .NEXT
        case "PULL":
            return .PREVIOUS
        case "RIGHT_PREVIOUS_SONG", "LEFT_PREVIOUS_SONG", "PREVIOUS_SONG", "PREV_SONG", "PREVIOUS_TRACK", "RIGHT_PREVIOUS", "LEFT_PREVIOUS":
            return .PREVIOUS
        case "RIGHT_NEXT_SONG", "LEFT_NEXT_SONG", "NEXT_SONG", "NEXT_TRACK", "RIGHT_NEXT", "LEFT_NEXT":
            return .NEXT
        case "RIGHT_PLAY_PAUSE", "LEFT_PLAY_PAUSE", "PLAY_PAUSE", "PLAYPAUSE", "TOGGLE_PLAY", "PLAY / PAUSE", "PLAY/PAUSE":
            return .PLAY
        case "RIGHT_VOLUME_UP", "LEFT_VOLUME_UP", "VOLUME_UP", "VOL_UP", "VOLUME +", "VOLUME+":
            return .VOLUME_UP
        case "RIGHT_VOLUME_DOWN", "LEFT_VOLUME_DOWN", "VOLUME_DOWN", "VOL_DOWN", "VOLUME -", "VOLUME-":
            return .VOLUME_DOWN
        case "SEARCH":
            return .SEARCH
        case "RIGHT_JIOSAAVN", "LEFT_JIOSAAVN", "JIOSAAVN", "OPEN_JIOSAAVN", "RIGHT_JIO_SAAVN", "JIO_SAAVN":
            return .OPEN_LINK
        default:
            break
        }
        // Keyword fallback (contains)
        if upper.contains("JIOSAAVN") || upper.contains("JIO_SAAVN") { return .OPEN_LINK }
        if upper.contains("PREVIOUS") { return .PREVIOUS }
        if upper.contains("NEXT") { return .NEXT }
        if upper.contains("PLAY") && upper.contains("PAUSE") { return .PLAY }
        if upper == "PLAY" { return .PLAY }
        if upper == "PAUSE" { return .PAUSE }
        if upper.contains("VOLUME") && upper.contains("UP") { return .VOLUME_UP }
        if upper.contains("VOLUME") && upper.contains("DOWN") { return .VOLUME_DOWN }
        if upper.contains("SEARCH") { return .SEARCH }
        return nil
    }

    /// Resolve with preference for normalized_command over raw command.
    static func resolve(raw: String, normalized: String?) -> CommandType {
        if let norm = normalized, let m = resolveAlias(norm) {
            return m
        }
        if let m = resolveAlias(raw) {
            return m
        }
        // Try uppercased raw directly
        if let direct = CommandType(rawValue: raw.uppercased()), direct != .unknown {
            return direct
        }
        return .unknown
    }
}

// MARK: - CommandMessage

/// Codable model matching MQTT JSON payload.
/// Handles graceful decoding for unknown commands and future-extensible fields.
struct CommandMessage: Codable, Equatable {
    let command: CommandType
    /// Raw string as received, useful for diagnostics when command is .unknown
    let rawCommand: String?
    let confidence: Double?
    let timestamp: TimeInterval?
    let url: String?

    // SEARCH query / OPEN_LINK url live here; other keys are tolerated for forward-compatibility.
    let seconds: Double?
    let position: Double?
    let direction: String?

    // SEARCH command field for dynamic JioSaavn search
    let query: String?

    // Legacy / tolerance: `target` may appear in some payloads, ignore via decodeIfPresent
    let target: String?
    // BCI device extra fields
    let normalizedCommand: String?
    let label: String?

    enum CodingKeys: String, CodingKey {
        case command
        case confidence
        case timestamp
        case url
        case seconds
        case position
        case direction
        case query
        case target
        case normalizedCommand = "normalized_command"
        case label
    }

    init(
        command: CommandType,
        rawCommand: String? = nil,
        confidence: Double? = nil,
        timestamp: TimeInterval? = nil,
        url: String? = nil,
        seconds: Double? = nil,
        position: Double? = nil,
        direction: String? = nil,
        query: String? = nil,
        target: String? = nil,
        normalizedCommand: String? = nil,
        label: String? = nil
    ) {
        self.command = command
        self.rawCommand = rawCommand
        self.confidence = confidence
        self.timestamp = timestamp
        self.url = url
        self.seconds = seconds
        self.position = position
        self.direction = direction
        if command == .SEARCH && (query == nil || query?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == true) {
            self.query = CommandType.defaultSearchQuery
        } else {
            self.query = query
        }
        self.target = target
        self.normalizedCommand = normalizedCommand
        self.label = label
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)

        // Command is required; missing key throws -> caught at call site as invalid JSON
        let raw = try container.decode(String.self, forKey: .command)
        let normalized = try container.decodeIfPresent(String.self, forKey: .normalizedCommand)
        let typed = CommandType.resolve(raw: raw, normalized: normalized)
        self.command = typed
        self.rawCommand = raw
        self.normalizedCommand = normalized
        self.label = try container.decodeIfPresent(String.self, forKey: .label)

        self.confidence = try container.decodeIfPresent(Double.self, forKey: .confidence)

        // Timestamp may be Int, Double, or ISO8601 string; handle all without throwing on type mismatch
        var ts: TimeInterval? = nil
        // Attempt Int (epoch seconds)
        do {
            if let v = try container.decodeIfPresent(Int.self, forKey: .timestamp) {
                ts = TimeInterval(v)
            }
        } catch { /* type mismatch -> try next */ }
        // Attempt Double (epoch with fractional)
        if ts == nil {
            do {
                if let v = try container.decodeIfPresent(Double.self, forKey: .timestamp) {
                    ts = v
                }
            } catch { }
        }
        // Attempt String (ISO8601 or epoch-as-string)
        if ts == nil {
            do {
                if let s = try container.decodeIfPresent(String.self, forKey: .timestamp) {
                    ts = Self.parseTimestampString(s)
                }
            } catch { }
        }
        self.timestamp = ts

        var decodedURL = try container.decodeIfPresent(String.self, forKey: .url)
        // Default URL for OPEN_LINK via JioSaavn alias when no URL provided
        if typed == .OPEN_LINK && (decodedURL == nil || decodedURL?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == true) {
            let upperRaw = raw.uppercased()
            let upperNorm = normalized?.uppercased() ?? ""
            if upperRaw.contains("JIOSAAVN") || upperRaw.contains("JIO_SAAVN") || upperNorm.contains("JIOSAAVN") || upperNorm.contains("JIO_SAAVN") {
                decodedURL = "https://www.jiosaavn.com"
            }
        }
        self.url = decodedURL
        self.seconds = try container.decodeIfPresent(Double.self, forKey: .seconds)
        self.position = try container.decodeIfPresent(Double.self, forKey: .position)
        self.direction = try container.decodeIfPresent(String.self, forKey: .direction)
        var decodedQuery = try container.decodeIfPresent(String.self, forKey: .query)
        if typed == .SEARCH && (decodedQuery == nil || decodedQuery?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == true) {
            decodedQuery = CommandType.defaultSearchQuery
        }
        self.query = decodedQuery
        self.target = try container.decodeIfPresent(String.self, forKey: .target)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        // Encode the raw string if unknown, else the typed value
        if command == .unknown, let raw = rawCommand {
            try container.encode(raw, forKey: .command)
        } else {
            try container.encode(command.rawValue, forKey: .command)
        }
        try container.encodeIfPresent(confidence, forKey: .confidence)
        try container.encodeIfPresent(timestamp, forKey: .timestamp)
        try container.encodeIfPresent(url, forKey: .url)
        try container.encodeIfPresent(seconds, forKey: .seconds)
        try container.encodeIfPresent(position, forKey: .position)
        try container.encodeIfPresent(direction, forKey: .direction)
        try container.encodeIfPresent(query, forKey: .query)
        try container.encodeIfPresent(target, forKey: .target)
        try container.encodeIfPresent(normalizedCommand, forKey: .normalizedCommand)
        try container.encodeIfPresent(label, forKey: .label)
    }

    // MARK: - Timestamp parsing

    /// Parse ISO8601 / common BCI timestamp strings to epoch seconds.
    /// Supports: 1757400000 (int already handled), "2026-09-09T15:51:35.550996", with/without Z or offset.
    static func parseTimestampString(_ s: String) -> TimeInterval? {
        let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return nil }
        // Try epoch as string
        if let doubleVal = Double(trimmed) {
            // Heuristic: if large > 1e9 it's epoch seconds, else nil
            if doubleVal > 1_000_000_000 { return doubleVal }
        }
        // Try ISO8601 with fractional seconds and timezone
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = iso.date(from: trimmed) {
            return date.timeIntervalSince1970
        }
        // Try ISO8601 without fractional
        iso.formatOptions = [.withInternetDateTime]
        if let date = iso.date(from: trimmed) {
            return date.timeIntervalSince1970
        }
        // Try without timezone (BCI sends "2026-09-09T15:51:35.550996" no Z)
        // Use DateFormatter with local/UTC assumption
        let df1 = DateFormatter()
        df1.locale = Locale(identifier: "en_US_POSIX")
        df1.timeZone = TimeZone(secondsFromGMT: 0)
        df1.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
        if let date = df1.date(from: trimmed) {
            return date.timeIntervalSince1970
        }
        df1.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSS"
        if let date = df1.date(from: trimmed) {
            return date.timeIntervalSince1970
        }
        df1.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        if let date = df1.date(from: trimmed) {
            return date.timeIntervalSince1970
        }
        // Try appending Z if missing timezone
        let withZ = trimmed + "Z"
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = iso.date(from: withZ) {
            return date.timeIntervalSince1970
        }
        iso.formatOptions = [.withInternetDateTime]
        if let date = iso.date(from: withZ) {
            return date.timeIntervalSince1970
        }
        return nil
    }
}

// MARK: - Helpers

extension CommandMessage {
    /// Formatted timestamp for UI display.
    var formattedTimestamp: String {
        guard let ts = timestamp else { return "—" }
        let date = Date(timeIntervalSince1970: ts)
        let formatter = DateFormatter()
        formatter.dateStyle = .short
        formatter.timeStyle = .medium
        return formatter.string(from: date)
    }

    /// Short time string for history table.
    var historyTimeString: String {
        guard let ts = timestamp else {
            let f = DateFormatter()
            f.dateFormat = "HH:mm:ss"
            return f.string(from: Date())
        }
        let date = Date(timeIntervalSince1970: ts)
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f.string(from: date)
    }
}

// MARK: - CommandHistoryEntry

/// Lightweight history entry for UI table. Kept here to avoid extra file for model.
struct CommandHistoryEntry: Identifiable, Equatable {
    let id = UUID()
    let date: Date
    let command: String
    let confidence: Double?
    let validationDisplay: String
    let executionDisplay: String
    let isValid: Bool

    static func == (lhs: CommandHistoryEntry, rhs: CommandHistoryEntry) -> Bool {
        lhs.id == rhs.id
    }
}
