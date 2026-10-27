import Foundation
import UIKit

// MARK: - URLOpener (testable abstraction)

protocol URLOpener {
    func canOpenURL(_ url: URL) -> Bool
    func open(_ url: URL, completion: ((Bool) -> Void)?)
}

extension UIApplication: URLOpener {
    func open(_ url: URL, completion: ((Bool) -> Void)?) {
        self.open(url, options: [:], completionHandler: completion)
    }
}

// MARK: - JioSaavnService

/// Handles JioSaavn-related URL/deep-link/media functionality.
/// Uses ONLY public iOS APIs. No private APIs, no UI automation.
/// For POC, OPEN_LINK opens HTTPS JioSaavn URLs via UIApplication.shared.open.
final class JioSaavnService {

    private let urlOpener: URLOpener

    init(urlOpener: URLOpener = UIApplication.shared) {
        self.urlOpener = urlOpener
    }

    // MARK: - OPEN_LINK

    /// Validates and opens a JioSaavn HTTPS URL.
    /// Returns true if open was attempted, false if validation/canOpen failed.
    /// Whitelist enforced again here (defense in depth) even though validator already checks.
    @discardableResult
    func openLink(urlString: String) -> Bool {
        guard let url = URL(string: urlString),
              let components = URLComponents(string: urlString),
              let scheme = components.scheme?.lowercased(), scheme == "https",
              let host = components.host?.lowercased() else {
            print("[JioSaavnService] Rejected OPEN_LINK: invalid URL '\(urlString)'")
            return false
        }

        let allowed = CommandValidator.allowedHosts.contains(host) || host.hasSuffix(".jiosaavn.com")
        guard allowed else {
            print("[JioSaavnService] Rejected OPEN_LINK: host not whitelisted '\(host)'")
            return false
        }

        // canOpenURL check (https always true if LSApplicationQueriesSchemes not restricting, but keep)
        // Note: canOpenURL for https returns true by default; no Info.plist entry needed.
        if !urlOpener.canOpenURL(url) {
            print("[JioSaavnService] canOpenURL returned false for \(url.absoluteString) — attempting open anyway for https")
        }

        // Must be called on main thread for UIApplication
        if Thread.isMainThread {
            urlOpener.open(url) { success in
                print("[JioSaavnService] openURL result: \(success) for \(url.absoluteString)")
            }
        } else {
            DispatchQueue.main.async {
                self.urlOpener.open(url) { success in
                    print("[JioSaavnService] openURL result: \(success) for \(url.absoluteString)")
                }
            }
        }
        print("[JioSaavnService] Opening JioSaavn URL: \(url.absoluteString)")
        return true
    }

    // This controller intentionally does not inspect or mirror JioSaavn's internal
    // playback state. It only opens JioSaavn URLs for validated search requests.

    func handlePlay() {
        // POC: log only. Real implementation would require app's own AVPlayer or JioSaavn scheme.
        print("[JioSaavnService] Executing PLAY command — POC log only (iOS media control requires app's own session or JioSaavn URL scheme)")
    }

    func handlePause() {
        print("[JioSaavnService] Executing PAUSE command — POC log only")
    }

    func handleNext() {
        print("[JioSaavnService] Executing NEXT command — POC log only")
    }

    func handlePrevious() {
        print("[JioSaavnService] Executing PREVIOUS command — POC log only")
    }

    func handleSeek(seconds: Double? = nil, position: Double? = nil, direction: String? = nil) {
        // Current protocol has no seek payload; keep extensible.
        if let s = seconds ?? position {
            print("[JioSaavnService] Executing SEEK command to \(s)s — POC log only (protocol extensible for seconds/position/direction)")
        } else if let dir = direction {
            print("[JioSaavnService] Executing SEEK command direction=\(dir) — POC log only")
        } else {
            print("[JioSaavnService] Executing SEEK command — POC log only (no position in current protocol; extend with seconds/position/direction later)")
        }
    }

    func handleVolumeUp() {
        // Public iOS restriction: MPVolumeView is UI-only; no public programmatic system volume API.
        print("[JioSaavnService] Executing VOLUME_UP command — POC log only (programmatic system volume restricted; MPVolumeView is UI-only)")
    }

    func handleVolumeDown() {
        print("[JioSaavnService] Executing VOLUME_DOWN command — POC log only (programmatic system volume restricted)")
    }

    func openSong(_ song: JioSaavnSong) -> Bool {
        guard let url = song.url else {
            print("[JioSaavnService] openSong failed: missing URL for \(song.title ?? "nil")")
            return false
        }
        return openLink(urlString: url)
    }

    // MARK: - Direct SEARCH (search.getResults, no Shortcut)
    //
    // NOTE (diagnosed): autocomplete.get currently returns HTTP 200 with body `[]`
    // for unauthenticated requests, so it yields zero songs. search.getResults on the
    // same api.php host returns {total,start,results:[...]} with the song URL under
    // `perma_url`. Same architecture, same signature — only the __call + mapping changed.

    /// Direct dynamic search against JioSaavn autocomplete/catalog endpoint.
    /// Query must come from MQTT (CommandMessage.query). Never hardcoded.
    /// - Parameter query: dynamic search string from MQTT CommandMessage.query.
    /// - Returns: song results with title/artist/url for display + open.
    func search(query: String) async throws -> [JioSaavnSong] {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            print("[JioSaavnService][SEARCH] ERROR: empty query")
            throw SearchError.emptyQuery
        }

        var comps = URLComponents(string: "https://www.jiosaavn.com/api.php")
        comps?.queryItems = [
            URLQueryItem(name: "__call", value: "search.getResults"),
            URLQueryItem(name: "_format", value: "json"),
            URLQueryItem(name: "_marker", value: "0"),
            URLQueryItem(name: "cc", value: "in"),
            URLQueryItem(name: "ctx", value: "web6dot0"),
            URLQueryItem(name: "api_version", value: "4"),
            URLQueryItem(name: "q", value: trimmed)
        ]
        guard let url = comps?.url else {
            print("[JioSaavnService][SEARCH] ERROR: failed to build URL for query '\(trimmed)'")
            throw SearchError.invalidURL(trimmed)
        }
        // DEBUG: search query + request
        print("[JioSaavnService][SEARCH][DEBUG] query='\(trimmed)'")
        print("[JioSaavnService][SEARCH] GET \(url.absoluteString)")

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(from: url)
        } catch {
            print("[JioSaavnService][SEARCH] ERROR: network failure: \(error.localizedDescription)")
            throw SearchError.networkError(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            print("[JioSaavnService][SEARCH] ERROR: non-HTTP response")
            throw SearchError.invalidResponse("Non-HTTP response")
        }
        // DEBUG: HTTP status + representative response structure (keys/counts only)
        print("[JioSaavnService][SEARCH][DEBUG] HTTP status=\(http.statusCode) bytes=\(data.count)")
        if let peek = try? JSONSerialization.jsonObject(with: data) {
            if let dict = peek as? [String: Any] {
                let resultCount = (dict["results"] as? [Any])?.count ?? -1
                print("[JioSaavnService][SEARCH][DEBUG] top-level keys=\(dict.keys.sorted()) results.count=\(resultCount)")
            } else if let arr = peek as? [Any] {
                print("[JioSaavnService][SEARCH][DEBUG] top-level is JSON array, count=\(arr.count)")
            } else {
                print("[JioSaavnService][SEARCH][DEBUG] top-level is scalar JSON value")
            }
        } else {
            print("[JioSaavnService][SEARCH][DEBUG] body is not JSON (prefix=\(String(data: data.prefix(120), encoding: .utf8) ?? "<?>"))")
        }
        guard (200...299).contains(http.statusCode) else {
            print("[JioSaavnService][SEARCH] ERROR: HTTP \(http.statusCode)")
            throw SearchError.invalidResponse("HTTP \(http.statusCode)")
        }

        let decoded: JioSaavnSearchResultsResponse
        do {
            decoded = try JSONDecoder().decode(JioSaavnSearchResultsResponse.self, from: data)
        } catch {
            print("[JioSaavnService][SEARCH] ERROR: JSON decode failed: \(error)")
            throw SearchError.decodingError(error.localizedDescription)
        }

        let songs = (decoded.results ?? []).map { $0.toJioSaavnSong() }
        print("[JioSaavnService][SEARCH] decoded \(songs.count) song(s) for '\(trimmed)'")
        // DEBUG: first decoded song (title + URL passed to openSong)
        if let first = songs.first {
            print("[JioSaavnService][SEARCH][DEBUG] first.title='\(first.title ?? "nil")' first.url='\(first.url ?? "nil")'")
        } else {
            print("[JioSaavnService][SEARCH][DEBUG] no songs decoded — nothing to open")
        }
        return songs
    }

    /// Synchronous URL builder (testable URL-encoding check, no network).
    static func searchURL(for query: String) -> URL? {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        var comps = URLComponents(string: "https://www.jiosaavn.com/api.php")
        comps?.queryItems = [
            URLQueryItem(name: "__call", value: "search.getResults"),
            URLQueryItem(name: "_format", value: "json"),
            URLQueryItem(name: "_marker", value: "0"),
            URLQueryItem(name: "cc", value: "in"),
            URLQueryItem(name: "ctx", value: "web6dot0"),
            URLQueryItem(name: "api_version", value: "4"),
            URLQueryItem(name: "q", value: trimmed)
        ]
        return comps?.url
    }
}

// MARK: - Search state / errors (direct in-app search, no Shortcut)

/// UI-facing search state. Kept separate from per-request errors.
enum SearchState: Equatable {
    case idle
    case searching
    case success
    case noResults
    case failure
}

enum SearchError: LocalizedError, Equatable {
    case emptyQuery
    case invalidURL(String)
    case networkError(String)
    case invalidResponse(String)
    case decodingError(String)

    var errorDescription: String? {
        switch self {
        case .emptyQuery: return "Empty search query"
        case .invalidURL(let q): return "Invalid search URL for query: \(q)"
        case .networkError(let r): return "Search network failure: \(r)"
        case .invalidResponse(let r): return "Search failed: \(r)"
        case .decodingError(let r): return "Search decode failed: \(r)"
        }
    }
}

// MARK: - JioSaavn API Models (search.getResults response)

// Live response shape: {total, start, results:[{id,title,subtitle,image,
// perma_url,year,type,more_info:{album,music,duration,artistMap:{primary_artists:[{name}]}}}]}.
// The playable song URL is under `perma_url` (NOT `url`).
struct JioSaavnSearchResultsResponse: Decodable {
    let total: Int?
    let start: Int?
    let results: [JioSaavnSearchResultItem]?
}

struct JioSaavnSearchResultItem: Decodable {
    let id: String?
    let title: String?
    let subtitle: String?
    let image: String?
    let permaURL: String?
    let year: String?
    let type: String?
    let moreInfo: JioSaavnSearchMoreInfo?

    enum CodingKeys: String, CodingKey {
        case id, title, subtitle, image, year, type
        case permaURL = "perma_url"
        case moreInfo = "more_info"
    }

    /// Map to the app's canonical song model. perma_url becomes song.url so the
    /// existing openSong()/openLink() validation + open path works unchanged.
    func toJioSaavnSong() -> JioSaavnSong {
        let names = moreInfo?.artistMap?.primaryArtists?.compactMap { $0.name }.filter { !$0.isEmpty }
        let artist: [String]?
        if let names, !names.isEmpty {
            artist = [names.joined(separator: ", ").decodingSaavnEntities()]
        } else if let sub = subtitle, !sub.isEmpty {
            // subtitle format is "Artist - Album"; use the artist part.
            let first = sub.components(separatedBy: " - ").first ?? sub
            artist = [first.decodingSaavnEntities()]
        } else {
            artist = nil
        }
        return JioSaavnSong(
            id: id,
            title: title?.decodingSaavnEntities(),
            artist: artist,
            url: permaURL,
            vlink: nil,
            image: image,
            album: moreInfo?.album?.decodingSaavnEntities(),
            year: year,
            duration: moreInfo?.duration.flatMap { Int($0) }
        )
    }
}

struct JioSaavnSearchMoreInfo: Decodable {
    let album: String?
    let music: String?
    let duration: String?
    let artistMap: JioSaavnArtistMap?

    enum CodingKeys: String, CodingKey {
        case album, music, duration
        case artistMap = "artistMap"
    }
}

struct JioSaavnArtistMap: Decodable {
    let primaryArtists: [JioSaavnArtistRef]?

    enum CodingKeys: String, CodingKey {
        case primaryArtists = "primary_artists"
    }
}

struct JioSaavnArtistRef: Decodable {
    let name: String?
}

// MARK: - Legacy autocomplete.get models (endpoint currently returns [])
//
// Kept compiling but superseded: autocomplete.get returns HTTP 200 `[]` for
// unauthenticated requests, so search() uses search.getResults above.

// Top-level autocomplete response. Only `songs` is required for results;
// album/playlist/artist/topquery sections are decoded leniently so unknown
// shapes never break song decoding.
struct JioSaavnAutocompleteResponse: Decodable {
    let songs: JioSaavnSongsSection?
    let albums: JioSaavnAlbumsSection?
    let playlists: JioSaavnPlaylistsSection?
    let artists: JioSaavnArtistsSection?
    let topquery: JioSaavnTopQuerySection?

    enum CodingKeys: String, CodingKey {
        case songs, albums, playlists, artists
        case topquery
        case topQueryAlt = "topQuery"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        songs = try c.decodeIfPresent(JioSaavnSongsSection.self, forKey: .songs)
        albums = try c.decodeIfPresent(JioSaavnAlbumsSection.self, forKey: .albums)
        playlists = try c.decodeIfPresent(JioSaavnPlaylistsSection.self, forKey: .playlists)
        artists = try c.decodeIfPresent(JioSaavnArtistsSection.self, forKey: .artists)
        // API uses "topquery"; tolerate "topQuery" variant.
        if let t = try? c.decodeIfPresent(JioSaavnTopQuerySection.self, forKey: .topquery), t != nil {
            topquery = t
        } else {
            topquery = try c.decodeIfPresent(JioSaavnTopQuerySection.self, forKey: .topQueryAlt)
        }
    }
}

struct JioSaavnSongsSection: Codable, Equatable {
    let data: [JioSaavnAutocompleteSong]?
    let status: Bool?
}

struct JioSaavnAlbumsSection: Codable, Equatable {
    let data: [JioSaavnAlbum]?
    let status: Bool?
}

struct JioSaavnPlaylist: Codable, Equatable {
    let id: String?
    let title: String?
    let image: String?
    let url: String?
}

struct JioSaavnPlaylistsSection: Codable, Equatable {
    let data: [JioSaavnPlaylist]?
    let status: Bool?
}

struct JioSaavnAlbum: Codable, Equatable {
    let id: String?
    let title: String?
    let image: String?
    let url: String?
    let music: String?
}

struct JioSaavnArtistsSection: Codable, Equatable {
    let data: [JioSaavnArtist]?
    let status: Bool?
}

struct JioSaavnArtist: Codable, Equatable {
    let id: String?
    let title: String?
    let name: String?
    let image: String?
    let url: String?

    var displayName: String? { title ?? name }
}

struct JioSaavnTopQuerySection: Codable, Equatable {
    let data: [JioSaavnTopQuery]?
    let status: Bool?
}

struct JioSaavnTopQuery: Codable, Equatable {
    let id: String?
    let title: String?
    let image: String?
    let url: String?
    let type: String?
}

/// Single song entry inside autocomplete `songs.data`.
/// Fields are all optional — the endpoint varies across app versions.
struct JioSaavnAutocompleteSong: Codable, Equatable {
    let id: String?
    let title: String?
    let image: String?
    let url: String?
    let album: String?
    let primaryArtists: String?
    let singers: String?
    let music: String?
    let year: String?
    let duration: String?

    enum CodingKeys: String, CodingKey {
        case id, title, image, url, album, singers, music, year, duration
        case primaryArtists = "primary_artists"
    }

    /// Map to the app's canonical song model used by display + openSong().
    func toJioSaavnSong() -> JioSaavnSong {
        let artistString = primaryArtists ?? singers ?? music
        let artists: [String]? = artistString.map { [$0.decodingSaavnEntities()] }
        return JioSaavnSong(
            id: id,
            title: title?.decodingSaavnEntities(),
            artist: artists,
            url: url,
            vlink: nil,
            image: image,
            album: album?.decodingSaavnEntities(),
            year: year,
            duration: duration.flatMap { Int($0) }
        )
    }
}

private extension String {
    /// JioSaavn titles are HTML-entity escaped (e.g. " & &#039;).
    func decodingSaavnEntities() -> String {
        var s = self
        let entities = [
            ("&quot;", "\""), ("&#039;", "'"),
            ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " ")
        ]
        for (entity, char) in entities { s = s.replacingOccurrences(of: entity, with: char) }
        // Numeric entities like &#123;
        while let range = s.range(of: "&#(\\d+);", options: .regularExpression) {
            let entity = String(s[range])
            let digits = entity.dropFirst(2).dropLast()
            if let code = Int(digits), let scalar = UnicodeScalar(code) {
                s.replaceSubrange(range, with: String(scalar))
            } else { break }
        }
        return s
    }
}


struct JioSaavnSong: Codable, Equatable {
    let id: String?
    let title: String?
    let artist: [String]?
    let url: String?
    let vlink: String?
    let image: String?
    let album: String?
    let year: String?
    let duration: Int?
    // Extended for queue context (optional, not always present in autocomplete)
    let albumid: String?
    let albumId: String?
    let moreInfo: JioSaavnMoreInfo?

    enum CodingKeys: String, CodingKey {
        case id, title, url, vlink, image, album, year, duration
        case artist
        case albumid
        case albumId = "albumId"
        case moreInfo = "more_info"
    }

    init(id: String?, title: String?, artist: [String]?, url: String?, vlink: String?, image: String?, album: String?, year: String?, duration: Int?, albumid: String? = nil, albumId: String? = nil, moreInfo: JioSaavnMoreInfo? = nil) {
        self.id = id; self.title = title; self.artist = artist; self.url = url; self.vlink = vlink; self.image = image; self.album = album; self.year = year; self.duration = duration; self.albumid = albumid; self.albumId = albumId; self.moreInfo = moreInfo
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        // artist may be String or [String] in different endpoints; handle gracefully
        if let arr = try? c.decodeIfPresent([String].self, forKey: .artist) {
            artist = arr
        } else if let str = try? c.decodeIfPresent(String.self, forKey: .artist) {
            artist = [str]
        } else {
            artist = nil
        }
        url = try c.decodeIfPresent(String.self, forKey: .url)
        vlink = try c.decodeIfPresent(String.self, forKey: .vlink)
        image = try c.decodeIfPresent(String.self, forKey: .image)
        album = try c.decodeIfPresent(String.self, forKey: .album)
        year = try c.decodeIfPresent(String.self, forKey: .year)
        duration = try c.decodeIfPresent(Int.self, forKey: .duration)
        albumid = try c.decodeIfPresent(String.self, forKey: .albumid)
        albumId = try c.decodeIfPresent(String.self, forKey: .albumId)
        moreInfo = try c.decodeIfPresent(JioSaavnMoreInfo.self, forKey: .moreInfo)
    }
}

struct JioSaavnMoreInfo: Codable, Equatable {
    let album: String?
    let albumId: String?
    let albumid: String?
    enum CodingKeys: String, CodingKey { case album; case albumId = "album_id"; case albumid }
}