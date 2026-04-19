import Foundation

struct AppConfig: Codable {
    var outputBookmark: Data?
    var lastTranscript: String?
    var launchAtLogin: Bool = false

    private static var storeURL: URL {
        let support = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return support.appendingPathComponent("MeetingTranscriber/config.json")
    }

    static func load() -> AppConfig {
        guard let data = try? Data(contentsOf: storeURL),
              let cfg = try? JSONDecoder().decode(AppConfig.self, from: data)
        else { return AppConfig() }
        return cfg
    }

    func save() {
        let dir = AppConfig.storeURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        guard let data = try? JSONEncoder().encode(self) else { return }
        try? data.write(to: AppConfig.storeURL, options: .atomic)
    }

    // Caller must balance startAccessingSecurityScopedResource() /
    // stopAccessingSecurityScopedResource() around any file I/O on this URL.
    var outputDirectory: URL? {
        guard let bookmark = outputBookmark else { return nil }
        var isStale = false
        return try? URL(resolvingBookmarkData: bookmark,
                        options: .withSecurityScope,
                        relativeTo: nil,
                        bookmarkDataIsStale: &isStale)
    }
}
