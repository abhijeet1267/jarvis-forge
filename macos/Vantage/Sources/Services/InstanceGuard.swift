import Foundation
import AppKit
import Darwin

/// Prevents two Vantage GUI instances from running concurrently.
///
/// The standard macOS approach for single-instance enforcement is an exclusive
/// file lock (`O_EXLOCK`). This beats `NSRunningApplication` bundle-id lookups
/// for three reasons:
///
/// 1. A development binary (run from `.build/`) has no bundle id, so the
///    bundle-id check would not catch the exact scenario that caused the
///    incident (a debug build displacing the installed app's backend).
/// 2. `O_EXLOCK` is released automatically when the process exits, crashes,
///    or is SIGKILLed — no stale lock file to clean up.
/// 3. It is race-free: two processes opening the same file with `O_EXLOCK`
///    are serialised by the kernel.
///
/// When a second instance is detected, we attempt to activate the existing
/// one's windows (via bundle-id lookup, which works for the installed app)
/// and then exit before any backend work begins.
enum InstanceGuard {

    /// The lock file lives in `~/Library/Application Support/Vantage/` — the
    /// same directory the app already uses for its persistent stores, so no new
    /// directory is introduced.
    private static let lockFileName = "vantage-instance.lock"

    /// Held for the process lifetime. Setting this to `nil` or letting the
    /// process exit releases the lock automatically.
    private static var lockFD: Int32 = -1

    /// Call once, early in `applicationWillFinishLaunching` — before the
    /// backend is started or any window is shown.
    ///
    /// Returns `true` if this process acquired the lock (it is the sole
    /// instance). Returns `false` if another instance holds it; the caller
    /// should activate the existing instance and exit.
    @discardableResult
    static func acquire() -> Bool {
        let dir = supportDirectory()
        try? FileManager.default.createDirectory(
            atPath: dir, withIntermediateDirectories: true)

        let path = (dir as NSString).appendingPathComponent(lockFileName)

        // O_EXLOCK: kernel-level exclusive lock, released on close / exit.
        // O_NONBLOCK: fail immediately if another process holds the lock.
        // O_NOFOLLOW: refuse to follow a symlink (defence in depth).
        let fd = path.withCString { cPath in
            open(cPath, O_RDWR | O_CREAT | O_EXLOCK | O_NONBLOCK | O_NOFOLLOW, 0o600)
        }

        if fd < 0 {
            // EWOULDBLOCK means another instance holds the exclusive lock.
            // Any other error (permissions, disk full) is treated the same way:
            // we cannot prove we are the only instance, so we refuse to run a
            // second one and risk displacing the other's backend.
            return false
        }

        // Write our pid for diagnostics (not used for ownership decisions —
        // the lock itself is the authority).
        let text = "\(getpid())\n"
        _ = ftruncate(fd, 0)
        _ = lseek(fd, 0, SEEK_SET)
        text.withCString { _ = write(fd, $0, strlen($0)) }

        lockFD = fd
        return true
    }

    /// Try to bring the existing instance's window to the front, then exit.
    ///
    /// Best-effort: the existing instance may be a development binary without
    /// a bundle identifier, in which case we cannot activate it — but we still
    /// refuse to start a second one.
    static func activateExistingAndExit() -> Never {
        let bundleID = "com.jarvisforge.vantage"
        let others = NSRunningApplication.runningApplications(
            withBundleIdentifier: bundleID)
        if let existing = others.first(where: { $0.processIdentifier != getpid() }) {
            existing.activate()
        }

        // Log to stderr so the message is visible in Console.app / terminal
        // when a second launch is attempted.
        fputs("Vantage: another instance is already running. Activating it.\n", stderr)
        exit(0)
    }

    // MARK: - Private

    private static func supportDirectory() -> String {
        // Mirror the convention CaptureStore and FlowStore already use.
        if let override = ProcessInfo.processInfo.environment["VANTAGE_SUPPORT_DIR"],
           !override.isEmpty {
            return override
        }
        let support = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSSearchPathForDirectoriesInDomains(
                .applicationSupportDirectory, .userDomainMask, true).first
                ?? (NSHomeDirectory() + "/Library/Application Support"))
        return support.appendingPathComponent("Vantage", isDirectory: true).path
    }
}
