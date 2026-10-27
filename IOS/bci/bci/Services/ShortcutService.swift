import Foundation
import UIKit

// MARK: - ShortcutService
// Uses Apple's public Shortcuts x-callback-url scheme.
// No private APIs, no extra dependencies.

enum ShortcutError: LocalizedError, Equatable {
    case emptyName
    case invalidURL(String)
    case cannotOpen(String)
    case openFailed(String)

    var errorDescription: String? {
        switch self {
        case .emptyName: return "Shortcut not found (empty name)"
        case .invalidURL(let s): return "Invalid shortcut URL: \(s)"
        case .cannotOpen(let n): return "Shortcuts app unavailable or shortcut not found: \(n)"
        case .openFailed(let r): return "Shortcut could not be opened: \(r)"
        }
    }
}

final class ShortcutService {
    private let urlOpener: URLOpener

    init(urlOpener: URLOpener = UIApplication.shared) {
        self.urlOpener = urlOpener
    }

    /// Build an x-callback URL for a given shortcut name.
    func url(forShortcutNamed name: String, input: String? = nil, callback: Bool = true) -> URL? {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }

        var comps = URLComponents()
        comps.scheme = "shortcuts"
        comps.host = "x-callback-url"
        comps.path = "/run-shortcut"
        let callbackURL = "synaptimesh://return"
        var queryItems = [
            URLQueryItem(name: "name", value: trimmed)
        ]
        if callback {
            queryItems.append(URLQueryItem(name: "x-success", value: callbackURL))
            queryItems.append(URLQueryItem(name: "x-cancel", value: callbackURL))
            queryItems.append(URLQueryItem(name: "x-error", value: callbackURL))
        }
        if let input, !input.isEmpty {
            queryItems.insert(URLQueryItem(name: "input", value: input), at: 1)
            queryItems.insert(URLQueryItem(name: "input-type", value: "Text"), at: 2)
        }
        comps.queryItems = queryItems
        return comps.url
    }

    /// Run shortcut by name. Calls UIApplication.open on main thread.
    /// Completion reports actual UIApplication.open success (not JioSaavn execution).
    @MainActor
    @discardableResult
    func runShortcut(named name: String, input: String? = nil, callback: Bool = true, completion: ((Result<Void, ShortcutError>) -> Void)? = nil) -> Bool {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            print("[SHORTCUT] Shortcut not found (empty name)")
            completion?(.failure(.emptyName))
            return false
        }
        guard let url = url(forShortcutNamed: trimmed, input: input, callback: callback) else {
            print("[SHORTCUT] Invalid shortcut URL for name: \(trimmed)")
            completion?(.failure(.invalidURL(trimmed)))
            return false
        }

        print("[SHORTCUT] Launching: \(trimmed)")
        print("[SHORTCUT] URL: \(url.absoluteString)")

        // For shortcuts://, canOpenURL generally reflects Shortcuts installed; on simulator it will be false.
        if !urlOpener.canOpenURL(url) {
            print("[SHORTCUT] canOpenURL=false for \(trimmed) — Shortcuts app unavailable or shortcut not found")
        }

        urlOpener.open(url) { success in
            print("[SHORTCUT] UIApplication.open completion: success=\(success) for \(trimmed)")
            if success {
                print("[SHORTCUT] Request accepted for \(trimmed)")
                completion?(.success(()))
            } else {
                print("[SHORTCUT] Shortcut could not be opened: \(trimmed) — LSApplicationWorkspaceErrorDomain Code=115 likely means name mismatch or Shortcuts not installed")
                completion?(.failure(.cannotOpen(trimmed)))
            }
        }
        return true
    }

    /// Temporary diagnostic: test if Shortcuts app can be opened at all (shortcuts://)
    @MainActor
    @discardableResult
    func testShortcutsApp() -> Bool {
        guard let url = URL(string: "shortcuts://") else { return false }
        print("[SHORTCUT TEST] Opening shortcuts://")
        if !urlOpener.canOpenURL(url) {
            print("[SHORTCUT TEST] canOpenURL=false for shortcuts://")
        }
        urlOpener.open(url) { success in
            print("[SHORTCUT TEST] UIApplication.open completion: success=\(success) for shortcuts://")
        }
        return true
    }

    // Exact JioSaavn spelling — do NOT use JioSaavan.
    // Finalized table: only normal commands + Open JioSaavn go through Shortcuts.
    // SEARCH uses the direct JioSaavn API (no Shortcut).
    static func shortcutName(for command: CommandType) -> String? {
        switch command {
        case .NEXT: return "JioSaavn Next"
        case .PREVIOUS: return "JioSaavn Previous"
        case .PLAY, .PAUSE: return "JioSaavn Play Pause"
        case .VOLUME_UP: return "JioSaavn Volume Up"
        case .VOLUME_DOWN: return "JioSaavn Volume Down"
        case .OPEN_LINK: return "Open JioSaavn"
        case .SEARCH: return nil
        case .unknown: return nil
        }
    }
}