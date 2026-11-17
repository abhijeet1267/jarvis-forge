import AppKit
import Foundation

// A compiled launcher, because a shell script cannot hold a TCC identity.
//
// Measured: with /bin/sh as CFBundleExecutable, macOS attributed the native
// executor's Accessibility request to "python3-55554944…" — the Homebrew runtime
// the script exec'd — so a grant to Vantage.app never applied. A Mach-O embedded
// in the signed bundle is the thing macOS can authorize, and everything it spawns
// inherits that attribution.
//
// It locates the bundled python, then execs it in place so there is no extra
// process layer between the app's identity and the backend.
let bundle = Bundle.main.bundleURL
let resources = bundle.appendingPathComponent("Contents/Resources")

func readTrimmed(_ path: URL) -> String? {
    guard let text = try? String(contentsOf: path, encoding: .utf8) else { return nil }
    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? nil : trimmed
}

// Prefer a python bundled inside the app; fall back to the recorded path, then PATH.
var candidates: [String] = []
for bundled in ["python/bin/python3", "python3/bin/python3", "bin/python3"] {
    let candidate = resources.appendingPathComponent(bundled).path
    if FileManager.default.isExecutableFile(atPath: candidate) { candidates.append(candidate) }
}
if let recorded = readTrimmed(resources.appendingPathComponent("python_path.txt")),
   FileManager.default.isExecutableFile(atPath: recorded) {
    candidates.append(recorded)
}
for external in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"] {
    if FileManager.default.isExecutableFile(atPath: external) { candidates.append(external) }
}

guard let python = candidates.first else {
    let alert = NSAlert()
    alert.messageText = "Vantage cannot start"
    alert.informativeText = "No Python 3 interpreter was found inside the app or on this Mac."
    alert.runModal()
    exit(1)
}

let launcher = resources.appendingPathComponent("launcher.py").path
guard FileManager.default.fileExists(atPath: launcher) else {
    let alert = NSAlert()
    alert.messageText = "Vantage cannot start"
    alert.informativeText = "The bundled launcher is missing from the application."
    alert.runModal()
    exit(1)
}

// Packages staged beside the app take precedence over whatever the interpreter
// would otherwise find. Without this the bundle still imported from the
// development venv, so copying it elsewhere changed which code ran.
// Resolved RELATIVE to this bundle, never from the recorded absolute path.
// Measured: the build records the path where it built, so a copied bundle pointed
// PYTHONPATH at the original location -- the copy imported packages from the
// bundle it was cloned from, or failed outright once that was gone. The staged
// directory is always beside this executable; that is what makes it relocatable.
let bundledPackages = resources.appendingPathComponent("site-packages")
if FileManager.default.fileExists(atPath: bundledPackages.path) {
    setenv("PYTHONPATH", bundledPackages.path, 1)
    setenv("PYTHONNOUSERSITE", "1", 1)   // ignore ~/.local, which may shadow them
}

// execv replaces this process, so the backend is a direct child of the same
// signed binary rather than of an intermediary shell.
var args: [UnsafeMutablePointer<CChar>?] = [strdup(python), strdup(launcher), nil]
execv(python, args)

// execv only returns on failure.
let alert = NSAlert()
alert.messageText = "Vantage cannot start"
alert.informativeText = "The Python interpreter at \(python) could not be executed."
alert.runModal()
exit(1)
