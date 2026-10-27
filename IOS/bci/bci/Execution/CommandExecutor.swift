import Foundation

// MARK: - ExecutionResult

enum ExecutionResult: Equatable {
    case executed(CommandType, detail: String)
    case rejected(ValidationResult)
    case failed(String)

    var displayString: String {
        switch self {
        case .executed(let cmd, let detail):
            if detail.isEmpty {
                return "Executed: \(cmd.rawValue)"
            }
            return "Executed: \(cmd.rawValue) — \(detail)"
        case .rejected(let validation):
            return "Rejected: \(validation.displayString)"
        case .failed(let reason):
            return "Failed: \(reason)"
        }
    }

    var isExecuted: Bool {
        if case .executed = self { return true }
        return false
    }
}

// MARK: - CommandExecutor
//
// Finalized architecture routing:
//   Normal commands (Play/Pause, Next, Previous, Volume Up/Down) ->
//     ShortcutService -> Apple Shortcut -> JioSaavn -> synaptimesh://return -> iOS app.
//   Open JioSaavn -> ShortcutService with NO callback (one-way, stays in JioSaavn).
//   SEARCH -> direct JioSaavn Search API (no Shortcut), first valid result auto-opened.

final class CommandExecutor {

    private let jioSaavnService: JioSaavnService
    private let shortcutService: ShortcutService
    private weak var mqttManager: MQTTManager?

    init(jioSaavnService: JioSaavnService = JioSaavnService(), shortcutService: ShortcutService = ShortcutService(), mqttManager: MQTTManager? = nil) {
        self.jioSaavnService = jioSaavnService
        self.shortcutService = shortcutService
        self.mqttManager = mqttManager
    }

    /// Main entry point. Caller should have already validated; if validation is not .valid we reject.
    func execute(_ message: CommandMessage, validation: ValidationResult) -> ExecutionResult {
        guard validation.isValid else {
            print("[Executor] Rejected \(message.command.rawValue) — \(validation.displayString)")
            return .rejected(validation)
        }

        switch message.command {
        case .PLAY, .PAUSE:
            return runShortcut("JioSaavn Play Pause", for: message.command)

        case .NEXT:
            return runShortcut("JioSaavn Next", for: .NEXT)

        case .PREVIOUS:
            return runShortcut("JioSaavn Previous", for: .PREVIOUS)

        case .VOLUME_UP:
            return runShortcut("JioSaavn Volume Up", for: .VOLUME_UP)

        case .VOLUME_DOWN:
            return runShortcut("JioSaavn Volume Down", for: .VOLUME_DOWN)

        case .OPEN_LINK:
            // One-way: no x-callback, app stays in JioSaavn.
            return runShortcut("Open JioSaavn", for: .OPEN_LINK, callback: false)

        case .SEARCH:
            return handleSearch(message)

        case .unknown:
            let raw = message.rawCommand ?? "UNKNOWN"
            print("[Executor] Unknown command: \(raw)")
            return .rejected(.unsupportedCommand(raw))
        }
    }

    private func runShortcut(_ shortcut: String, for command: CommandType, callback: Bool = true) -> ExecutionResult {
        print("[COMMAND] Mapped command: \(command.rawValue)")
        print("[SHORTCUT] Launching: \(shortcut) callback=\(callback)")
        Task { @MainActor in
            _ = shortcutService.runShortcut(named: shortcut, callback: callback)
        }
        let detail = callback
            ? "Shortcut launch requested: \(shortcut)"
            : "Shortcut launch requested: \(shortcut) (no synaptimesh://return)"
        return .executed(command, detail: detail)
    }

    private func handleSearch(_ message: CommandMessage) -> ExecutionResult {
        let rawQuery = message.query?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let trimmedQuery = rawQuery.isEmpty ? CommandType.defaultSearchQuery : rawQuery

        // SEARCH is intentionally separated: direct JioSaavn API, no Shortcut.
        // One-way flow: search, validate URL, open first valid result in JioSaavn. DONE.
        print("[SEARCH] Query: \(trimmedQuery)")
        print("[SEARCH] Direct JioSaavnService.search(query:) — no Shortcut delegation")
        DispatchQueue.main.async { [weak self] in
            self?.mqttManager?.handleSearchStarted(query: trimmedQuery)
        }
        Task {
            do {
                let songs = try await self.jioSaavnService.search(query: trimmedQuery)
                let firstValid = songs.first(where: { !(($0.url ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) })
                await MainActor.run { [weak self] in
                    guard let self else { return }
                    self.mqttManager?.handleSearchSuccess(query: trimmedQuery, openedSong: firstValid, hasResults: !songs.isEmpty)
                    if let song = firstValid {
                        print("[SEARCH] Auto-opening first valid result: \(song.title ?? "nil")")
                        let opened = self.jioSaavnService.openSong(song)
                        print("[SEARCH] Auto-open attempted, opened=\(opened)")
                    } else {
                        print("[SEARCH] No valid result (with URL) for '\(trimmedQuery)' — nothing opened")
                    }
                }
            } catch {
                await MainActor.run { [weak self] in
                    self?.mqttManager?.handleSearchFailure(query: trimmedQuery)
                }
            }
        }
        return .executed(.SEARCH, detail: "Direct search started for query: \(trimmedQuery) — first valid result will auto-open in JioSaavn")
    }
}
