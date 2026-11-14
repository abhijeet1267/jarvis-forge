import Foundation

/// Path validation for the native file capabilities.
///
/// Mirrors the backend's `jarvis/security/paths.py` rather than inventing a
/// second, weaker rule set: the same protected roots, the same credential and
/// persistence subpaths, and the same explicit safe prefixes. The two must agree
/// because an action that the backend refuses must not become possible just
/// because it was asked from the native screen.
enum MacPathGuard {

    /// Roots the app may never modify or enumerate. A trailing `/` is added when
    /// comparing, so `/System` does not also match `/Systematic`.
    static let protectedRoots: [String] = [
        "/", "/System", "/Library", "/bin", "/sbin", "/usr", "/etc", "/var",
        "/private", "/Applications",
    ]

    /// Subpaths under the user's home that hold credentials, grant persistence,
    /// or influence other programs running as the user.
    static let protectedHomeSubpaths: [String] = [
        ".ssh", ".gnupg", ".aws", ".kube", ".netrc", ".git-credentials", ".docker",
        ".config/gh", ".config/gcloud", ".npmrc", ".pypirc",
        "Library/Keychains", "Library/Cookies",
        ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".bashrc", ".bash_profile",
        ".bash_login", ".profile", ".config/fish", ".gitconfig",
        "Library/LaunchAgents", "Library/LaunchDaemons",
        ".jarvis-forge", ".claude", "Library/Application Support/Vantage",
    ]

    /// Checked before the protected roots so scratch space stays usable.
    static let safePrefixes: [String] = ["/tmp", "/private/tmp", "/private/var/folders"]

    /// Well-known folders a person names in a sentence. Expanded exactly, never
    /// guessed: an unknown name is refused instead of being treated as relative.
    static let namedFolders: [String: String] = [
        "downloads": "Downloads", "documents": "Documents", "desktop": "Desktop",
        "pictures": "Pictures", "music": "Music", "movies": "Movies",
        "applications": "Applications", "public": "Public",
        "home": "", "~": "",
    ]

    enum Refusal: Error, Equatable {
        case empty
        case relativePath(String)
        case protectedPath(String)
        case credentials(String)
        case notFound(String)
        case notADirectory(String)
        case symlinkEscape(String)
        case collision(String)
        case unsafeName(String)

        var message: String {
            switch self {
            case .empty:
                return "No path was given, so nothing was done."
            case .relativePath(let p):
                return "\(p) is a relative path. Mac control only acts on absolute paths or "
                    + "the folders you name (Downloads, Documents, Desktop…)."
            case .protectedPath(let p):
                return "\(p) is a protected location. Vantage will not change it."
            case .credentials(let p):
                return "\(p) holds credentials or changes what runs at login. Vantage will "
                    + "not touch it."
            case .notFound(let p):
                return "\(p) does not exist."
            case .notADirectory(let p):
                return "\(p) is a file, not a folder."
            case .symlinkEscape(let p):
                return "\(p) is a symbolic link that points outside an allowed location. "
                    + "Refused."
            case .collision(let p):
                return "\(p) already exists. Vantage will not overwrite it."
            case .unsafeName(let n):
                return "\(n) is not a usable name: names cannot contain a path separator "
                    + "or start with a dot."
            }
        }
    }

    // MARK: - Resolution

    /// Resolve a person's path or folder name into an absolute, checked URL.
    ///
    /// `~` is expanded, a well-known folder name is mapped, and anything else
    /// must already be absolute. The result is *not* followed through symlinks
    /// yet — the caller decides whether it is creating (where the parent is
    /// checked) or acting on an existing item (where the item is checked).
    static func resolve(_ raw: String) throws -> URL {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw Refusal.empty }

        let expanded = (trimmed as NSString).expandingTildeInPath
        if let folder = namedFolders[trimmed.lowercased()] {
            let base = FileManager.default.homeDirectoryForCurrentUser
            return folder.isEmpty ? base : base.appendingPathComponent(folder)
        }
        guard expanded.hasPrefix("/") else { throw Refusal.relativePath(trimmed) }
        return URL(fileURLWithPath: expanded).standardizedFileURL
    }

    /// The reason this location may not be written to or enumerated, or nil.
    ///
    /// Symlinks are resolved first, so a link in an allowed folder that points
    /// at `~/.ssh` is judged by where it *lands*, not where it sits.
    static func refusal(for url: URL, home: URL? = nil) -> Refusal? {
        let homeURL = home ?? FileManager.default.homeDirectoryForCurrentUser
        let resolved = url.resolvingSymlinksInPath().standardizedFileURL.path
        let raw = url.standardizedFileURL.path

        if resolved != raw && url.standardizedFileURL.path != resolved {
            // The item is a link that moves somewhere else. Judge the target,
            // and if the target is itself protected, that is a symlink escape
            // worth naming distinctly.
            if let inner = refusal(forPath: resolved, home: homeURL) {
                return .symlinkEscape(url.path)
            }
            return nil
        }
        return refusal(forPath: resolved, home: homeURL)
    }

    private static func refusal(forPath path: String, home: URL) -> Refusal? {
        if safePrefixes.contains(where: { path == $0 || path.hasPrefix($0 + "/") }) {
            // Still refuse credential stores that live under a safe prefix.
            return nil
        }
        for root in protectedRoots where path == root || path.hasPrefix(root + "/") {
            return .protectedPath(path)
        }
        let homePath = home.standardizedFileURL.path
        if path == homePath || path.hasPrefix(homePath + "/") {
            let relative = String(path.dropFirst(homePath.count)).trimmingCharacters(
                in: CharacterSet(charactersIn: "/"))
            for sub in protectedHomeSubpaths
            where relative == sub || relative.hasPrefix(sub + "/") {
                return .credentials(path)
            }
        }
        return nil
    }

    /// Validate a name being created inside a folder: no separators, no leading
    /// dot, no traversal.
    static func validateNewName(_ name: String) throws -> String {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty || trimmed.contains("/") || trimmed.contains(":")
            || trimmed.hasPrefix(".") || trimmed == ".." || trimmed == "." {
            throw Refusal.unsafeName(trimmed.isEmpty ? "(empty)" : trimmed)
        }
        return trimmed
    }

    /// A destination that must not already exist. Used by copy/move/create so a
    /// second run cannot silently replace the first result.
    static func requireAbsent(_ url: URL) throws {
        if FileManager.default.fileExists(atPath: url.path) {
            throw Refusal.collision(url.path)
        }
    }
}