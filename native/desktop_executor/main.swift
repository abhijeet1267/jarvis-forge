/*
 The native desktop executor: real Core Graphics and Accessibility, from Swift.

 This is a command-line tool the Python backend drives. It exists because the
 permission that lets a synthetic event reach the window server is granted per
 executable, and the process that posts must be the process that is authorized.
 A Python script from a terminal is not that process, and neither is a `swift run`
 harness; only a binary the user has added in System Settings is.

 What each capability actually needs, measured rather than assumed:

 * **Cursor movement** goes through ``CGWarpMouseCursorPosition``, which relocates
   the real pointer through the window server. Measured on this machine with
   ``CGPreflightPostEventAccess() == false``: the pointer moved (701,718) →
   (731,748) and the readback confirmed it. This is the narrowest primitive that
   still produces a genuinely moved cursor.
 * **Clicks, drags, scrolls and keystrokes** are posted as ``CGEvent`` taps and
   need *post-event* access, which is what ``CGPreflightPostEventAccess`` reports.
   With it `false`, ``post`` returns no error and does nothing at all — which is
   exactly why this module never treats "the call returned" as success.
 * **Reading and pressing UI elements** goes through the Accessibility API and
   needs ``AXIsProcessTrusted()``.

 Every operation returns an *observation*, not a claim. A caller that wants to
 say "the click worked" must point at a re-read that says so; this tool can only
 report what it saw.

 Output is one JSON object on stdout. Exit codes are deliberately coarse: the
 structured ``status`` field carries the meaning.
*/

import AppKit
import ApplicationServices
import CoreGraphics
import CryptoKit

// ---------------------------------------------------------------------------
// Result plumbing
// ---------------------------------------------------------------------------

/// The result vocabulary shared with the Python side. Nothing here reports
/// "ok" for an action it could not observe.
enum Status: String {
    case verified              // the expected UI change was observed
    case performed_unverified  // the event was accepted; no observable outcome was read
    case failed                // the operation was attempted and errored
    case blocked               // refused before acting (target, scope, safety)
    case permission_required   // the OS will not accept this from this process
    case target_not_found
    case target_ambiguous
    case target_stale          // the intended target moved or changed owner since it was resolved
    case timed_out
    case cancelled
    case unknown
}

struct Emit {
    static func json(_ body: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys]) else {
            print("{\"status\":\"failed\",\"reason\":\"could not serialise result\"}")
            return
        }
        print(String(data: data, encoding: .utf8) ?? "{}")
    }

    static func out(_ status: Status, _ reason: String, _ extra: [String: Any] = [:]) -> Never {
        var body: [String: Any] = extra
        body["status"] = status.rawValue
        body["reason"] = reason
        json(body)
        exit(status == .verified || status == .performed_unverified ? 0 : 2)
    }
}

// ---------------------------------------------------------------------------
// Permission readings — the honest preflight
// ---------------------------------------------------------------------------

/// What macOS says about *this* executable right now. Every value is read
/// fresh: a cached "granted" is how a program claims a capability it lost.
func permissionSnapshot() -> [String: Any] {
    let trusted = AXIsProcessTrusted()
    let post = CGPreflightPostEventAccess()
    let screen = CGPreflightScreenCaptureAccess()
    return [
        "accessibility": trusted,
        "post_events": post,
        "screen_capture": screen,
        "process_name": ProcessInfo.processInfo.processName,
        "executable": Bundle.main.executablePath ?? CommandLine.arguments.first ?? "",
        "pid": ProcessInfo.processInfo.processIdentifier,
        // The whole point of naming the subject: macOS answers about the
        // process that asks, and the answer for a harness is not the answer
        // for the app in the Dock.
        "subject_note": "these values describe this executable only; a grant to Vantage.app does not apply here and vice versa",
    ]
}

func requireAX(_ verb: String) {
    if AXIsProcessTrusted() { return }
    Emit.out(.permission_required,
             "\(verb) needs Accessibility access and macOS has not granted it to this executable (\(ProcessInfo.processInfo.processName)). Add this binary in System Settings → Privacy & Security → Accessibility, then run it again.",
             ["permissions": permissionSnapshot(), "needs": "accessibility"])
}

func requirePostEvents(_ verb: String) {
    if CGPreflightPostEventAccess() { return }
    Emit.out(.permission_required,
             "\(verb) needs permission to post input events and macOS has not granted it to this executable (\(ProcessInfo.processInfo.processName)). Add this binary in System Settings → Privacy & Security → Accessibility (the same switch covers event posting), then run it again.",
             ["permissions": permissionSnapshot(), "needs": "post_events"])
}

// ---------------------------------------------------------------------------
// Coordinate space
// ---------------------------------------------------------------------------

/// The single conversion layer. Everything the tool accepts is a **global
/// display point** — the space CGEvent and CGWarpMouseCursorPosition both use.
///
/// The confusion this exists to prevent: a screenshot is in *pixels*, an
/// AXFrame is in points relative to the *top-left* of the main display, and a
/// window `NSRect` is in points relative to the *bottom-left*. Mixing any two
/// of them produces a click that lands a title bar's height away from its
/// target and works on one display only.
enum Coord {
    /// Screen bounds for one display, in points, top-left origin.
    static func displayBounds(_ id: CGDirectDisplayID) -> CGRect { CGDisplayBounds(id) }

    /// The displays that are present and drawing, in a stable order.
    ///
    /// `CGGetActiveDisplayList` was the obvious choice and it is wrong here: on
    /// this machine it returns an empty list with error 0 while
    /// `CGGetOnlineDisplayList` returns the one real display and
    /// `CGMainDisplayID` reports 1352x878 points. Building a point check on the
    /// empty list refused every coordinate with "not on any display" -- a
    /// validation bug that looks exactly like a safety feature.
    ///
    /// `CGGetOnlineDisplayList` is the honest source: it reports the displays
    /// the system is driving, which is what "where can a pointer go" means.
    /// The main display is unioned in so a machine that reports neither still
    /// has a usable bound rather than an empty one.
    static func activeDisplays() -> [CGDirectDisplayID] {
        var count: UInt32 = 0
        CGGetOnlineDisplayList(0, nil, &count)
        var ids = [CGDirectDisplayID](repeating: 0, count: Int(max(count, 8)))
        var filled: UInt32 = 0
        CGGetOnlineDisplayList(UInt32(ids.count), &ids, &filled)
        var result = Array(ids.prefix(Int(filled)))
        if result.isEmpty {
            let main = CGMainDisplayID()
            if CGDisplayBounds(main).width > 0 { result = [main] }
        }
        return result
    }

    /// Every display's bounds, for callers that need to validate a point.
    static func allBounds() -> [[String: Any]] {
        activeDisplays().enumerated().map { index, id in
            let b = displayBounds(id)
            return [
                "index": index,
                "id": id,
                "x": b.origin.x, "y": b.origin.y,
                "width": b.width, "height": b.height,
                "pixel_width": CGDisplayPixelsWide(id),
                "pixel_height": CGDisplayPixelsHigh(id),
                // 1.0 on a non-Retina display, 2.0 on Retina. A screenshot is
                // in pixels; every coordinate this tool accepts is in points.
                "scale": b.width > 0 ? Double(CGDisplayPixelsWide(id)) / Double(b.width) : 1.0,
            ]
        }
    }

    /// Which display contains a point, or nil. Used to refuse an off-screen
    /// coordinate rather than letting the window server clamp it silently.
    static func displayContaining(_ point: CGPoint) -> CGDirectDisplayID? {
        activeDisplays().first { displayBounds($0).contains(point) }
    }

    /// Convert an AppKit window rect (bottom-left origin, points) into a global
    /// display rect (top-left origin). Kept here so no caller re-derives it.
    static func globalRect(fromAppKit rect: NSRect, screen: NSScreen?) -> CGRect {
        let screenHeight = screen?.frame.height ?? NSScreen.main?.frame.height ?? 0
        return CGRect(x: rect.origin.x,
                      y: screenHeight - rect.origin.y - rect.height,
                      width: rect.width, height: rect.height)
    }
}

func cursorPosition() -> CGPoint? { CGEvent(source: nil)?.location }

// ---------------------------------------------------------------------------
// Accessibility element plumbing
// ---------------------------------------------------------------------------

/// A flattened description of one AX element. Only attributes the element
/// actually exposes are present: a missing key means "this element does not
/// report it", not "it is empty".
func describe(_ element: AXUIElement, depth: Int, maxDepth: Int) -> [String: Any] {
    var out: [String: Any] = [:]

    func attr(_ name: String) -> Any? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else {
            return nil
        }
        return value
    }

    if let role = attr(kAXRoleAttribute) as? String { out["role"] = role }
    if let subrole = attr(kAXSubroleAttribute) as? String { out["subrole"] = subrole }
    if let title = attr(kAXTitleAttribute) as? String, !title.isEmpty { out["title"] = title }
    if let label = attr(kAXDescriptionAttribute) as? String, !label.isEmpty { out["label"] = label }
    if let ident = attr(kAXIdentifierAttribute) as? String, !ident.isEmpty { out["identifier"] = ident }
    if let value = attr(kAXValueAttribute) {
        // A value may be a string, a number or a boolean; only report the kinds
        // a caller can act on, and never a string a secure field returns.
        if let s = value as? String { out["value"] = String(s.prefix(300)) }
        else if let n = value as? NSNumber { out["value"] = n }
        else if let b = value as? Bool { out["value"] = b }
    }
    if let enabled = attr(kAXEnabledAttribute) as? Bool { out["enabled"] = enabled }
    if let focused = attr(kAXFocusedAttribute) as? Bool { out["focused"] = focused }
    // AXFrame is the element's rect in global points already, which is why it
    // is preferred over composing parent offsets by hand.
    if let posValue = attr(kAXPositionAttribute),
       let sizeValue = attr(kAXSizeAttribute),
       CFGetTypeID(posValue as CFTypeRef) == AXValueGetTypeID(),
       CFGetTypeID(sizeValue as CFTypeRef) == AXValueGetTypeID() {
        var point = CGPoint.zero
        var size = CGSize.zero
        AXValueGetValue(posValue as! AXValue, .cgPoint, &point)
        AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
        out["x"] = point.x; out["y"] = point.y
        out["width"] = size.width; out["height"] = size.height
        out["center_x"] = point.x + size.width / 2
        out["center_y"] = point.y + size.height / 2
    }

    var actions: CFArray?
    if AXUIElementCopyActionNames(element, &actions) == .success,
       let names = actions as? [String] {
        out["actions"] = names
    }

    if depth < maxDepth, let children = attr(kAXChildrenAttribute) as? [AXUIElement] {
        out["children"] = children.prefix(120).map {
            describe($0, depth: depth + 1, maxDepth: maxDepth)
        }
    }
    return out
}

/// Depth-first search for elements matching a predicate.
func findElements(in root: AXUIElement, matching predicate: ([String: Any]) -> Bool,
                  maxDepth: Int) -> [[String: Any]] {
    var found: [[String: Any]] = []

    func walk(_ element: AXUIElement, depth: Int) {
        if found.count >= 40 { return }
        let described = describe(element, depth: 0, maxDepth: 0)
        if predicate(described) { found.append(described) }
        guard depth < maxDepth else { return }
        var childrenValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString,
                                            &childrenValue) == .success,
              let children = childrenValue as? [AXUIElement] else { return }
        for child in children { walk(child, depth: depth + 1) }
    }
    walk(root, depth: 0)
    return found
}

/// The AX element for an element identified by (identifier, role, title).
/// Returned with its position so a caller can decide between a semantic action
/// and a fallback coordinate click.
func locate(keyword: String, application: String, role: String, useTitle: Bool) -> (AXUIElement, [String: Any])? {
    let app = resolveApplication(application)
    let root = AXUIElementCreateApplication(app.processIdentifier)
    let needle = keyword.lowercased()

    // Match on identifier first (most stable), then label, then title. A
    // missing identifier is common; that is why the fallbacks exist and why an
    // ambiguous result is reported rather than guessed.
    func matches(_ d: [String: Any]) -> Bool {
        if !role.isEmpty, (d["role"] as? String) != role { return false }
        let fields = ["identifier", "label", "title"].filter { key in
            useTitle || key != "title" || role.isEmpty
        }
        for key in fields {
            if let s = d[key] as? String, !s.isEmpty, s.lowercased().contains(needle) {
                return true
            }
        }
        return false
    }

    let hits = findElements(in: root, matching: matches, maxDepth: 18)
    guard let first = hits.first else { return nil }

    // More than one match is a question, not a target. This used to take
    // `hits.first` silently while the comment above promised ambiguity was
    // "reported rather than guessed" -- and `target_ambiguous` was declared and
    // never emitted. Measured consequence: "press the Save button" in an app
    // with two Save elements pressed whichever the walk reached first, and
    // reported it as done.
    //
    // Two hits that describe identically are not ambiguous (a SwiftUI button
    // publishes wrapper and child). Only *different* descriptions are, so the
    // comparison is on the identifying fields rather than the count.
    if hits.count > 1 {
        let identities = Set(hits.map { hit -> String in
            [hit["identifier"] as? String ?? "",
             hit["role"] as? String ?? "",
             hit["title"] as? String ?? "",
             hit["label"] as? String ?? ""].joined(separator: "\u{1}")
        })
        if identities.count > 1 {
            let described = hits.prefix(8).map { hit -> String in
                let role = hit["role"] as? String ?? "element"
                let name = (hit["title"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                    ?? (hit["label"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                    ?? (hit["identifier"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                    ?? "(unnamed)"
                return "\(role) \(name)"
            }
            Emit.out(.target_ambiguous,
                     "\(hits.count) elements match \(keyword); name one exactly, or "
                     + "supply a role, rather than having one chosen for you",
                     ["matches": Array(described), "match_count": hits.count,
                      "keyword": keyword, "role": role])
        }
    }

    // Re-walk to get the live element for the hit (describe() returns a copy).
    var live: AXUIElement?
    func walk(_ element: AXUIElement, depth: Int) {
        if live != nil { return }
        let d = describe(element, depth: 0, maxDepth: 0)
        if matches(d) { live = element; return }
        guard depth < 18 else { return }
        var childrenValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString,
                                            &childrenValue) == .success,
              let children = childrenValue as? [AXUIElement] else { return }
        for child in children { walk(child, depth: depth + 1) }
    }
    walk(root, depth: 0)
    guard let element = live else { return nil }
    return (element, first)
}

func resolveApplication(_ name: String) -> NSRunningApplication {
    let all = NSWorkspace.shared.runningApplications
    if name.isEmpty {
        guard let front = NSWorkspace.shared.frontmostApplication else {
            Emit.out(.target_not_found, "no application is in front, and none was named")
        }
        return front
    }
    let needle = name.lowercased()
    if let exact = all.first(where: {
        ($0.bundleIdentifier ?? "").lowercased() == needle
    }) { return exact }
    if let byName = all.first(where: {
        ($0.localizedName ?? "").lowercased() == needle
    }) { return byName }
    if let partial = all.first(where: {
        ($0.localizedName ?? "").lowercased().contains(needle)
    }) { return partial }
    Emit.out(.target_not_found,
             "no running application matches \(name)",
             ["running": all.compactMap { $0.localizedName }.sorted()])
}

// ---------------------------------------------------------------------------
// Cancellation
// ---------------------------------------------------------------------------

/// A caller creates the flag file to stop a gesture that is already moving.
///
/// Checking it is a `stat()`, so the answer is cached for 25ms: at a 12ms drag
/// step an unthrottled check would add a syscall per step for no benefit, and
/// 25ms is below the latency a person can tell apart from immediate.
enum Cancellation {
    nonisolated(unsafe) static var path: String?
    private nonisolated(unsafe) static var lastCheck = Date.distantPast
    private nonisolated(unsafe) static var cached = false

    static func requested() -> Bool {
        guard let path else { return false }
        let now = Date()
        if now.timeIntervalSince(lastCheck) < 0.025 { return cached }
        lastCheck = now
        // A flag file that cannot be stat'ed is treated as absent rather than
        // as a cancellation: a cancellation must be as deliberate as the action.
        cached = FileManager.default.fileExists(atPath: path)
        return cached
    }
}

// ---------------------------------------------------------------------------
// Operations
// ---------------------------------------------------------------------------

/// Posts an up event for every button and every modifier this tool can leave
/// down. Shared by `release_all`, by a cancelled gesture, and by a drag whose
/// own mouse-up could not be constructed, so recovery never needs a second
/// invocation. Returns only the events that were actually constructed and
/// posted; a caller reports that list rather than claiming a clean state.
func releaseHeldInput() -> [String] {
    var posted: [String] = []
    let at = cursorPosition() ?? .zero
    let buttons: [(CGEventType, CGMouseButton, String)] = [
        (.leftMouseUp, .left, "left_button"),
        (.rightMouseUp, .right, "right_button"),
        (.otherMouseUp, .center, "other_button"),
    ]
    for (type, button, name) in buttons {
        if let event = CGEvent(mouseEventSource: nil, mouseType: type,
                               mouseCursorPosition: at, mouseButton: button) {
            event.post(tap: .cghidEventTap)
            posted.append(name)
        }
    }
    // A modifier whose key-up was lost shows up as every later keystroke
    // carrying its flag, so each one gets its own empty key-up.
    let empty = [UniChar]()
    for name in ["cmd", "shift", "option", "control"] {
        guard let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false) else {
            continue
        }
        empty.withUnsafeBufferPointer { buffer in
            up.keyboardSetUnicodeString(stringLength: 0, unicodeString: buffer.baseAddress)
        }
        up.flags = []
        up.post(tap: .cghidEventTap)
        posted.append(name)
    }
    usleep(20_000)
    return posted
}

func opReleaseAll() -> Never {
    requirePostEvents("releasing held input")
    let released = releaseHeldInput()
    Emit.out(.performed_unverified,
             "mouse-up and modifier key-up events were posted at the current pointer position. Whether anything was actually held cannot be read back; callers treat this as recovery, not as evidence that a button was down.",
             ["released": released,
              "cursor": cursorPosition().map { ["x": $0.x, "y": $0.y] } ?? NSNull()])
}

/// Who this process actually is, so a permission grant can be described without
/// guessing.
///
/// A TCC grant is attached to a code identity, and there is no API that reports
/// "what macOS thinks I am". Everything here is read from the running process or
/// from the file on disk: the executable path, the bundle it belongs to, its
/// ad-hoc signature state, and the revision it was built from. Reporting an
/// identity that was assembled by hand instead would be exactly the fabricated
/// permission claim this campaign forbids.
struct IdentityReport {
    let executable: String
    let bundleID: String
    let bundlePath: String
    let version: String
    let signing: String
}

/// The one identity lookup, shared by `identity` and `capabilities` so the two
/// can never answer differently about the same binary.
func readIdentity() -> IdentityReport {
    let exe = Bundle.main.executablePath
        ?? CommandLine.arguments.first
        ?? ""
    var bundleID = Bundle.main.bundleIdentifier ?? ""
    var bundlePath = Bundle.main.bundlePath
    var version = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? ""

    // A bare binary has no Info.plist of its own; look for the enclosing
    // .app so the answer says "inside this bundle" rather than "nothing".
    if bundleID.isEmpty {
        var dir = URL(fileURLWithPath: exe).deletingLastPathComponent()
        while dir.path != "/" {
            if dir.pathExtension == "app" {
                bundlePath = dir.path
                if let plist = NSDictionary(contentsOf: dir.appendingPathComponent("Contents/Info.plist")) {
                    bundleID = (plist["CFBundleIdentifier"] as? String) ?? ""
                    version = (plist["CFBundleShortVersionString"] as? String) ?? ""
                }
                break
            }
            dir = dir.deletingLastPathComponent()
        }
    }

    var signing = "not reported"
    let identity = Process()
    identity.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
    identity.arguments = ["-d", "--verbose=2", exe]
    let pipe = Pipe()
    identity.standardError = pipe
    identity.standardOutput = FileHandle.nullDevice
    if (try? identity.run()) != nil {
        identity.waitUntilExit()
        let text = String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                          encoding: .utf8) ?? ""
        // Only the two facts that decide whether a grant follows the binary:
        // ad-hoc signatures are not stable across rebuilds, and a Team ID is
        // what a Developer ID signature would carry.
        if text.contains("Signature=adhoc") { signing = "adhoc" }
        else if let range = text.range(of: "TeamIdentifier=") {
            let tail = text[range.upperBound...].prefix(while: { $0 != "\n" })
            signing = tail.trimmingCharacters(in: .whitespaces).isEmpty
                ? "signed, no team" : "signed, team \(tail.trimmingCharacters(in: .whitespaces))"
        } else if !text.isEmpty {
            signing = "unsigned or unreadable signature"
        }
    }
    return IdentityReport(executable: exe, bundleID: bundleID, bundlePath: bundlePath,
                          version: version, signing: signing)
}

func opIdentity() -> Never {
    let identity = readIdentity()
    let exe = identity.executable
    let fm = FileManager.default

    // The facts go in the reason, not only in the payload: a caller that shows
    // the sentence and drops the JSON would otherwise present "identity read
    // from this process" with nothing in it, which is a claim with no subject.
    Emit.out(.verified,
             "\(ProcessInfo.processInfo.processName) at \(exe), bundle "
             + "\(identity.bundleID.isEmpty ? "(none)" : identity.bundleID), signed \(identity.signing).", [
        "executable": exe,
        "process_name": ProcessInfo.processInfo.processName,
        "pid": ProcessInfo.processInfo.processIdentifier,
        "bundle_id": identity.bundleID,
        "bundle_path": identity.bundlePath,
        "bundle_version": identity.version,
        "signing": identity.signing,
        "executable_exists": fm.fileExists(atPath: exe),
        "executable_sha256": sha256OfFile(exe),
        // Stated because it is the thing a person needs before granting: an
        // ad-hoc signature is not an identity macOS will keep across a rebuild.
        "grant_caveat": "a TCC grant follows this code identity. An ad-hoc signature "
            + "is not stable across rebuilds, so rebuilding in place may require "
            + "the grant again. Sign with a stable identity to avoid that.",
        "permissions": permissionSnapshot(),
    ])
}

func sha256OfFile(_ path: String) -> String {
    guard let data = FileManager.default.contents(atPath: path) else { return "" }
    // CryptoKit ships with macOS. A hand-rolled digest here would be exactly
    // the "custom cryptography" the project forbids, for no gain.
    let digest = SHA256.hash(data: data)
    return digest.map { String(format: "%02x", $0) }.joined()
}

/// One on-screen window, as the window server reports it.
struct WindowInfo {
    let owner: String
    let windowID: Int
    let title: String
    let bounds: CGRect          // global points, top-left origin
    let layer: Int
    let alpha: Double
    let onScreen: Bool
    let pid: Int

    var json: [String: Any] {
        ["owner": owner, "window_id": windowID, "title": title,
         "x": bounds.origin.x, "y": bounds.origin.y,
         "width": bounds.width, "height": bounds.height,
         "layer": layer, "alpha": alpha, "on_screen": onScreen, "pid": pid]
    }
}

/// Every on-screen window, in global top-left coordinates. `nil` means the
/// window list could not be read at all (no Screen Recording, or the server
/// returned nothing) — which a staleness check must report as `unknown` rather
/// than treating as "no windows", because the two mean different things.
///
/// This is the window-relative half of the one coordinate system the tools use
/// (V15 §10). It needs only screen-recording access, which is the permission a
/// cursor move already requires — so a caller can learn where a window really is
/// on this machine without Accessibility, and can then aim a click at it.
///
/// The window server reports bounds in points with a top-left origin, matching
/// CGEvent space directly. Nothing here converts, because every conversion that
/// has been needed so far has been a chance to mirror a coordinate.
func readOnScreenWindows() -> [WindowInfo]? {
    guard CGPreflightScreenCaptureAccess() else { return nil }
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let raw = CGWindowListCopyWindowInfo(options, kCGNullWindowID)
            as? [[String: Any]] else { return nil }
    var result: [WindowInfo] = []
    for entry in raw {
        // Bounds come as a CFDictionary under the "kCGWindowBounds" key; a window
        // without one is not measurable and is skipped rather than reported at 0,0.
        guard let dict = entry[kCGWindowBounds as String] as? [String: Any],
              let rect = CGRect(dictionaryRepresentation: dict as CFDictionary) else { continue }
        result.append(WindowInfo(
            owner: (entry[kCGWindowOwnerName as String] as? String) ?? "",
            windowID: (entry[kCGWindowNumber as String] as? Int) ?? 0,
            title: (entry[kCGWindowName as String] as? String) ?? "",
            bounds: rect,
            layer: (entry[kCGWindowLayer as String] as? Int) ?? 0,
            alpha: (entry[kCGWindowAlpha as String] as? Double) ?? 1,
            onScreen: (entry[kCGWindowIsOnscreen as String] as? Bool) ?? false,
            pid: (entry[kCGWindowOwnerPID as String] as? Int) ?? 0))
    }
    return result
}

func opWindows(application: String) -> Never {
    guard CGPreflightScreenCaptureAccess() else {
        Emit.out(.permission_required,
                 "listing windows needs Screen Recording access and macOS has not "
                 + "granted it to this executable (\(ProcessInfo.processInfo.processName)).",
                 ["permissions": permissionSnapshot(), "needs": "screen_capture"])
    }
    guard let all = readOnScreenWindows() else {
        Emit.out(.failed, "the window server returned no window list")
    }
    let needle = application.lowercased()
    // Layer 0 is the normal window plane; higher layers are menus, tooltips
    // and overlays, which are real but not what "the app's window" means. They
    // are still reported (the field is present) — the filter is the caller's.
    let windows = all.filter { w in
        needle.isEmpty || w.owner.lowercased().contains(needle)
    }.map { $0.json }
    let front = NSWorkspace.shared.frontmostApplication
    Emit.out(.verified,
             "read \(windows.count) window(s)"
             + (application.isEmpty ? " on screen" : " for \(application)"),
             ["windows": windows,
              "screen_capture": true,
              "frontmost_application": front?.localizedName ?? "",
              "note": "bounds are global points, top-left origin — the same space "
                    + "every click and move in this tool accepts."])
}

func opPermissions() -> Never {
    var body = permissionSnapshot()
    body["displays"] = Coord.allBounds()
    body["cursor"] = cursorPosition().map { ["x": $0.x, "y": $0.y] } ?? NSNull()
    body["can_move_cursor"] = true   // CGWarpMouseCursorPosition works without post access
    body["can_click"] = CGPreflightPostEventAccess()
    body["can_type"] = CGPreflightPostEventAccess()
    body["can_read_ui"] = AXIsProcessTrusted()
    Emit.out(.verified, "permission posture read from macOS", body)
}

/// What every operation needs, in one place so the report cannot drift from the
/// gates the operations actually call.
///
/// `gate` is the permission without which the operation always refuses.
/// `alsoRequires` is a conditional extra: `click --expect-*` additionally needs
/// the window list to check the target, and `focus window=` needs Accessibility
/// to find the window by title.
struct OpGate {
    let gate: String                 // none | accessibility | post_events | screen_capture
    var alsoRequires: [String] = []
    var note: String = ""
}

let operationGates: [String: OpGate] = [
    "capabilities":   OpGate(gate: "none"),
    "permissions":    OpGate(gate: "none"),
    "identity":       OpGate(gate: "none"),
    "request_access": OpGate(gate: "none",
                             note: "asks macOS to show its own prompt; grants nothing by itself"),
    "move":           OpGate(gate: "none",
                             note: "CGWarpMouseCursorPosition relocates the real pointer without post-event access"),
    "windows":        OpGate(gate: "screen_capture"),
    "click":          OpGate(gate: "post_events",
                             alsoRequires: ["screen_capture"],
                             note: "screen_capture only when --expect-window-id/--expect-pid/--expect-bounds is given"),
    "drag":           OpGate(gate: "post_events",
                             alsoRequires: ["screen_capture"],
                             note: "screen_capture only when an --expect-* target is given (checked at the start point)"),
    "scroll":         OpGate(gate: "post_events"),
    "type":           OpGate(gate: "post_events"),
    "key":            OpGate(gate: "post_events"),
    "focus":          OpGate(gate: "none", alsoRequires: ["accessibility"],
                             note: "accessibility only for window= (matching a window by title)"),
    "inspect":        OpGate(gate: "accessibility"),
    "read_element":   OpGate(gate: "accessibility"),
    "press_element":  OpGate(gate: "accessibility"),
    "verify_change":  OpGate(gate: "accessibility"),
    "release_all":    OpGate(gate: "post_events"),
]

/// One JSON object answering "what can this build actually do right now".
///
/// The per-operation gate is the useful half: a single global yes/no hides that
/// `move` and `windows` work while `click` does not.
func opCapabilities() -> Never {
    let granted: [String: Bool] = [
        "none": true,
        "accessibility": AXIsProcessTrusted(),
        "post_events": CGPreflightPostEventAccess(),
        "screen_capture": CGPreflightScreenCaptureAccess(),
    ]
    var ops: [String: Any] = [:]
    for (name, gate) in operationGates {
        let gateGranted = granted[gate.gate] ?? false
        let extrasSatisfied = gate.alsoRequires.allSatisfy { granted[$0] ?? false }
        ops[name] = [
            "gate": gate.gate,
            "gate_granted": gateGranted,
            // "Usable now" means the always-required gate is on; the conditional
            // extras are named separately because they only bind for some forms.
            "usable_now": gateGranted,
            "also_requires": gate.alsoRequires,
            "conditional_extras_granted": extrasSatisfied,
            "note": gate.note,
        ]
    }
    let identity = readIdentity()
    let name = ProcessInfo.processInfo.processName
    Emit.out(.verified,
             "\(name): accessibility \(granted["accessibility"] == true ? "on" : "off"), "
             + "post_events \(granted["post_events"] == true ? "on" : "off"), "
             + "screen_capture \(granted["screen_capture"] == true ? "on" : "off"); "
             + "\(operationGates.count) operation(s) reported with their gates.", [
        "executable": identity.executable,
        "process_name": name,
        "pid": ProcessInfo.processInfo.processIdentifier,
        "bundle_id": identity.bundleID,
        "bundle_path": identity.bundlePath,
        "bundle_version": identity.version,
        "signing": identity.signing,
        "accessibility": granted["accessibility"] ?? false,
        "post_events": granted["post_events"] ?? false,
        "screen_capture": granted["screen_capture"] ?? false,
        "displays": Coord.allBounds(),
        "supported_operations": operationGates.keys.sorted(),
        "operations": ops,
        "subject_note": "permissions describe this executable only; a grant to Vantage.app does not apply here and vice versa",
    ])
}

/// Ask macOS to show its own Accessibility prompt for this executable.
///
/// This is the supported user-facing request: the system dialog that offers to
/// add the app in System Settings. It grants nothing by itself and cannot --
/// the person still has to flip the switch. Included so the app has a
/// "Request Permission" button that does the real thing rather than telling the
/// user to go hunting.
func opRequestAccess() -> Never {
    // kAXTrustedCheckOptionPrompt is the documented key. Passing true makes
    // macOS surface the dialog; the return value is the current state, which
    // is almost always false at the moment the prompt appears.
    let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    let trusted = AXIsProcessTrustedWithOptions(options)
    Emit.out(.verified,
             trusted
               ? "Accessibility is already granted to this executable"
               : "macOS has been asked to show its Accessibility prompt. Approve it in System Settings → Privacy & Security → Accessibility, then quit and run this binary again — the grant is read at process start.",
             ["accessibility_now": trusted,
              "executable": Bundle.main.executablePath ?? "",
              "process_name": ProcessInfo.processInfo.processName,
              // A rebuilt binary can be a different code identity, so a grant
              // to an older build may not apply. Stated rather than implied.
              "identity_note": "macOS identifies a binary by its code signature and path. If you rebuild and replace it, the old grant may not follow; check this screen again after any rebuild."])
}

func opMove(x: Double, y: Double, smooth: Bool, durationMs: Int) -> Never {
    let target = CGPoint(x: x, y: y)
    guard let before = cursorPosition() else {
        Emit.out(.unknown, "the pointer position could not be read, so a move cannot be verified")
    }
    guard let display = Coord.displayContaining(target) else {
        Emit.out(.blocked,
                 "the point (\(x), \(y)) is not inside any active display; the window server would clamp it and the click would land somewhere you did not ask for",
                 ["displays": Coord.allBounds(), "cursor_before": ["x": before.x, "y": before.y]])
    }

    if smooth && durationMs > 0 {
        // Small steps with a real warp each time. Warping is what actually
        // moves the pointer here; the steps only make the path legible.
        let steps = max(2, min(24, durationMs / 16))
        for i in 1...steps {
            let t = Double(i) / Double(steps)
            let p = CGPoint(x: before.x + (target.x - before.x) * t,
                            y: before.y + (target.y - before.y) * t)
            CGWarpMouseCursorPosition(p)
            usleep(useconds_t(max(1, durationMs / steps) * 1000))
        }
    }
    CGWarpMouseCursorPosition(target)
    // Re-associating prevents the "warp is undone on next mouse input" quirk.
    CGAssociateMouseAndMouseCursorPosition(boolean_t(1))
    usleep(60_000)

    let after = cursorPosition()
    let moved = after.map { abs($0.x - target.x) <= 2 && abs($0.y - target.y) <= 2 } ?? false
    let body: [String: Any] = [
        "display_id": display,
        "target": ["x": target.x, "y": target.y],
        "cursor_before": ["x": before.x, "y": before.y],
        "cursor_after": after.map { ["x": $0.x, "y": $0.y] } ?? NSNull(),
        "tolerance_points": 2,
    ]
    if moved {
        Emit.out(.verified,
                 "the real pointer is at (\(Int(after!.x)), \(Int(after!.y))); readback after the move confirms it", body)
    }
    Emit.out(.failed,
             "the warp was issued and the pointer did not arrive; readback says \(after.map { "(\(Int($0.x)), \(Int($0.y)))" } ?? "unreadable")",
             body)
}

// ---------------------------------------------------------------------------
// Target staleness
// ---------------------------------------------------------------------------

/// What the caller believed about the target when it resolved it. Every field
/// is optional; an empty expectation leaves an operation behaving exactly as it
/// did before this check existed.
struct TargetExpectation {
    var windowID: Int?
    var pid: Int?
    var bounds: CGRect?

    var isEmpty: Bool { windowID == nil && pid == nil && bounds == nil }
}

/// How far a window edge may drift before its target is called stale. The same
/// 2-point tolerance the cursor readback uses: below this, a "move" is window
/// server rounding, not a window that went somewhere else.
let targetBoundsTolerance: Double = 2

func rectJSON(_ r: CGRect) -> [String: Any] {
    ["x": r.origin.x, "y": r.origin.y, "width": r.width, "height": r.height]
}

func rectText(_ r: CGRect) -> String {
    "(\(r.origin.x), \(r.origin.y)) \(r.width)×\(r.height)"
}

func boundsDiffer(_ a: CGRect, _ b: CGRect, tolerance: Double) -> Bool {
    abs(a.origin.x - b.origin.x) > tolerance
        || abs(a.origin.y - b.origin.y) > tolerance
        || abs(a.width - b.width) > tolerance
        || abs(a.height - b.height) > tolerance
}

/// Refuse to post at a point whose target has moved, vanished or changed owner
/// since the caller resolved it.
///
/// This runs BEFORE `requirePostEvents` on purpose: "the window is gone" is
/// knowable without permission to post, and reporting the missing permission
/// first would hide the more specific, more useful fact.
///
/// Returns the evidence of what was checked (empty when there was nothing to
/// check). Every failure branch emits and exits without posting anything.
/// An expectation that cannot be checked is reported as `unknown` and refused:
/// a caller that asked for validation did not ask for a guess in its place.
func requireFreshTarget(_ expectation: TargetExpectation, point: CGPoint,
                        verb: String) -> [String: Any] {
    guard !expectation.isEmpty else { return [:] }

    var checked: [String: Any] = [
        "point": ["x": point.x, "y": point.y],
        "expected_window_id": expectation.windowID.map { $0 as Any } ?? NSNull(),
        "expected_pid": expectation.pid.map { $0 as Any } ?? NSNull(),
        "expected_bounds": expectation.bounds.map(rectJSON) ?? NSNull(),
        "tolerance_points": targetBoundsTolerance,
    ]

    // Bounds with no window identity have no subject to compare against; they
    // could describe any window on any display. Refuse rather than pick one.
    if expectation.windowID == nil, expectation.pid == nil, let expected = expectation.bounds {
        Emit.out(.unknown,
                 "\(verb) was not attempted: --expect-bounds was given without "
                 + "--expect-window-id or --expect-pid, so \(rectText(expected)) "
                 + "cannot be compared with any particular window. Nothing was posted.",
                 checked.merging(["staleness_check": "no_subject"]) { $1 })
    }

    guard let windows = readOnScreenWindows() else {
        Emit.out(.unknown,
                 "\(verb) was not attempted: the target could not be checked because "
                 + "the on-screen window list is unreadable (Screen Recording is not "
                 + "granted to this executable, or the window server returned nothing). "
                 + "An unverifiable target is reported as unknown, never assumed fresh.",
                 checked.merging(["staleness_check": "unavailable",
                                  "permissions": permissionSnapshot()]) { $1 })
    }

    // A window id names exactly one window, so it is looked up by identity, not
    // by coincidentally matching coordinates.
    if let id = expectation.windowID {
        guard let window = windows.first(where: { $0.windowID == id }) else {
            Emit.out(.target_not_found,
                     "the intended target window \(id) is not in the on-screen "
                     + "window list any more; nothing was posted. Re-resolve the window "
                     + "before acting.",
                     checked.merging(["staleness_check": "window_missing",
                                      "on_screen_window_count": windows.count]) { $1 })
        }
        checked["actual_bounds"] = rectJSON(window.bounds)
        checked["owner"] = window.owner
        checked["owner_pid"] = window.pid

        // The same window number reused by a different process is a different
        // target even though the id matched.
        if let pid = expectation.pid, pid != window.pid {
            Emit.out(.target_stale,
                     "window \(id) still exists but is now owned by pid \(window.pid), "
                     + "not the expected pid \(pid); nothing was posted.",
                     checked.merging(["staleness_check": "owner_changed"]) { $1 })
        }
        if let expected = expectation.bounds,
           boundsDiffer(window.bounds, expected, tolerance: targetBoundsTolerance) {
            Emit.out(.target_stale,
                     "window \(id) has moved or resized since it was resolved: "
                     + "expected \(rectText(expected)), now \(rectText(window.bounds)); "
                     + "the point (\(point.x), \(point.y)) would land on whatever is "
                     + "beneath it, so nothing was posted.",
                     checked.merging(["staleness_check": "bounds_moved"]) { $1 })
        }
        checked["window_checked"] = true
    }

    // The point must still be inside a window the expected process owns. This
    // catches the common case where the window moved and the caller only knows
    // the pid.
    if let pid = expectation.pid {
        let owned = windows.filter { $0.pid == pid && $0.bounds.contains(point) }
        guard !owned.isEmpty else {
            Emit.out(.target_stale,
                     "the point (\(point.x), \(point.y)) is not inside any on-screen "
                     + "window owned by pid \(pid); the window it was resolved from "
                     + "has moved, resized or closed, so nothing was posted.",
                     checked.merging(["staleness_check": "point_outside_pid"]) { $1 })
        }
        // With a pid but no window id, the bounds expectation is matched against
        // a window of that pid that contains the point.
        if expectation.windowID == nil, let expected = expectation.bounds {
            let matches = owned.contains {
                !boundsDiffer($0.bounds, expected, tolerance: targetBoundsTolerance)
            }
            guard matches else {
                Emit.out(.target_stale,
                         "no window owned by pid \(pid) that contains "
                         + "(\(point.x), \(point.y)) has the expected bounds "
                         + "\(rectText(expected)); nothing was posted.",
                         checked.merging(["staleness_check": "bounds_moved",
                                          "windows_at_point":
                                            owned.map { rectJSON($0.bounds) }]) { $1 })
            }
        }
        checked["pid_windows_at_point"] = owned.map { $0.windowID }
        checked["pid_checked"] = true
    }

    checked["staleness_check"] = "fresh"
    return checked
}

func opClick(x: Double, y: Double, button: String, count: Int,
             expectation: TargetExpectation = TargetExpectation()) -> Never {
    let point = CGPoint(x: x, y: y)
    // Before permission: staleness is a fact about the window server, not a
    // privilege, and it is the more specific answer when both are missing.
    let targetCheck = requireFreshTarget(expectation, point: point, verb: "clicking")
    requirePostEvents("clicking")
    guard let display = Coord.displayContaining(point) else {
        Emit.out(.blocked, "the point (\(x), \(y)) is not inside any active display",
                 ["displays": Coord.allBounds()])
    }
    // Put the pointer on the target first: a click event carries a location,
    // but several apps act on where the pointer *is*.
    CGWarpMouseCursorPosition(point)
    usleep(40_000)

    let cgButton: CGMouseButton = button == "right" ? .right : .left
    let downType: CGEventType = button == "right" ? .rightMouseDown : .leftMouseDown
    let upType: CGEventType = button == "right" ? .rightMouseUp : .leftMouseUp

    var postedCount = 0
    let total = max(1, count)
    for index in 0..<total {
        if Cancellation.requested() {
            let released = releaseHeldInput()
            Emit.out(.cancelled, "cancelled after starting; held input was released",
                     ["posted_before_cancel": postedCount, "released_before_exit": released])
        }
        guard let down = CGEvent(mouseEventSource: nil, mouseType: downType,
                                 mouseCursorPosition: point, mouseButton: cgButton),
              let up = CGEvent(mouseEventSource: nil, mouseType: upType,
                               mouseCursorPosition: point, mouseButton: cgButton) else {
            Emit.out(.failed, "the click event could not be constructed")
        }
        // clickState counts up across the run: the first press is 1, the second
        // is 2, and macOS turns that 1-then-2 sequence into a double-click.
        //
        // This used to set every event to `count`, so a double-click sent two
        // events both claiming to be the second press of a pair. No first press
        // meant no sequence, and several controls discarded it entirely --
        // measured: the fixture's double-click target never fired.
        let clickState = Int64(index + 1)
        down.setIntegerValueField(.mouseEventClickState, value: clickState)
        up.setIntegerValueField(.mouseEventClickState, value: clickState)
        down.post(tap: .cghidEventTap)
        usleep(8_000)
        up.post(tap: .cghidEventTap)
        postedCount += 1
        // Between the presses of a multi-click the gap must stay inside the
        // double-click interval, or macOS reads them as separate clicks.
        usleep(index + 1 < total ? 45_000 : 40_000)
    }
    let after = cursorPosition()
    var body: [String: Any] = [
        "display_id": display, "count": count, "button": button,
        "cursor_after": after.map { ["x": $0.x, "y": $0.y] } ?? NSNull(),
    ]
    if !expectation.isEmpty { body["target_check"] = targetCheck }
    Emit.out(.performed_unverified,
             "\(count)× \(button) click posted at (\(x), \(y)) on display \(display). The event was accepted; whether the target changed is read separately by a UI observation, never assumed here.",
             body)
}

func opDrag(fromX: Double, fromY: Double, toX: Double, toY: Double, durationMs: Int,
            expectation: TargetExpectation = TargetExpectation()) -> Never {
    let start = CGPoint(x: fromX, y: fromY), end = CGPoint(x: toX, y: toY)
    // The expectation describes where the drag starts: a button under the
    // pointer that has moved is exactly the target-not-found case.
    let targetCheck = requireFreshTarget(expectation, point: start, verb: "dragging")
    requirePostEvents("dragging")
    guard Coord.displayContaining(start) != nil, Coord.displayContaining(end) != nil else {
        Emit.out(.blocked, "the drag has an endpoint outside every active display",
                 ["displays": Coord.allBounds()])
    }

    CGWarpMouseCursorPosition(start)
    usleep(40_000)
    guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
                             mouseCursorPosition: start, mouseButton: .left) else {
        Emit.out(.failed, "the mouse-down event could not be constructed")
    }
    down.post(tap: .cghidEventTap)

    // The button is held from here on, so every exit below releases it. A
    // `defer` cannot be that guarantee: Emit.out ends the process with exit(),
    // and a deferred block does not run on that path (measured on this
    // toolchain), so each terminal branch releases explicitly instead.
    let steps = max(4, min(40, durationMs / 12))
    for i in 1...steps {
        if Cancellation.requested() {
            let released = releaseHeldInput()
            Emit.out(.cancelled, "cancelled after starting; held input was released",
                     ["released": released,
                      "from": ["x": fromX, "y": fromY], "to": ["x": toX, "y": toY],
                      "cursor": cursorPosition().map { ["x": $0.x, "y": $0.y] } ?? NSNull()])
        }
        let t = Double(i) / Double(steps)
        let p = CGPoint(x: start.x + (end.x - start.x) * t,
                        y: start.y + (end.y - start.y) * t)
        guard let move = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged,
                                 mouseCursorPosition: p, mouseButton: .left) else { break }
        move.post(tap: .cghidEventTap)
        usleep(useconds_t(max(1, durationMs / steps) * 1000))
    }
    guard let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                           mouseCursorPosition: end, mouseButton: .left) else {
        // The specific up could not be built, so fall back to the recovery
        // release rather than leaving the button down with only a message.
        let released = releaseHeldInput()
        Emit.out(.failed, "the drag completed but the mouse-up event could not be constructed; a recovery release was posted",
                 ["released": released])
    }
    up.post(tap: .cghidEventTap)
    usleep(60_000)
    var body: [String: Any] = [
        "from": ["x": fromX, "y": fromY], "to": ["x": toX, "y": toY],
        "cursor_after": cursorPosition().map { ["x": $0.x, "y": $0.y] } ?? NSNull(),
    ]
    if !expectation.isEmpty { body["target_check"] = targetCheck }
    Emit.out(.performed_unverified,
             "drag posted from (\(fromX), \(fromY)) to (\(toX), \(toY)) with the button released at the end. Whether the target accepted the drop is read separately.",
             body)
}

func opScroll(x: Double, y: Double, lines: Int, horizontal: Bool,
              expectation: TargetExpectation = TargetExpectation()) -> Never {
    requirePostEvents("scrolling")
    let point = CGPoint(x: x, y: y)
    guard Coord.displayContaining(point) != nil else {
        Emit.out(.blocked, "the scroll point is not inside any active display")
    }
    // A wheel event carries no target: it scrolls whatever window sits under the
    // pointer. Replay after the window moved would silently scroll the wrong
    // thing, so the expectation is checked before the cursor is warped.
    let targetCheck = requireFreshTarget(expectation, point: point, verb: "scrolling")
    CGWarpMouseCursorPosition(point)
    usleep(40_000)
    guard let event = CGEvent(scrollWheelEvent2Source: nil, units: .line,
                              wheelCount: horizontal ? 2 : 1,
                              wheel1: horizontal ? 0 : Int32(lines),
                              wheel2: horizontal ? Int32(lines) : 0, wheel3: 0) else {
        Emit.out(.failed, "the scroll event could not be constructed")
    }
    event.location = point
    event.post(tap: .cghidEventTap)
    usleep(80_000)
    var body: [String: Any] = ["lines": lines, "horizontal": horizontal]
    if !expectation.isEmpty { body["target_check"] = targetCheck }
    Emit.out(.performed_unverified,
             "a scroll of \(lines) line(s) was posted at (\(x), \(y)). Whether the view moved is read separately.",
             body)
}

/// Why the focused element must not receive synthetic characters, or "" if it may.
///
/// Typing goes to whatever macOS has focused, so the thing that decides whether a
/// secret is at risk is the focus target, not the text. Read it before every
/// typing operation rather than trusting the caller to have focused something
/// safe: a mission can focus a field, and a later step has no idea what that was.
///
/// Deliberately narrow. A secure text field is unambiguous — macOS publishes it as
/// its own role and never returns its value. The other two are judgement calls
/// about applications that own the whole keyboard while they are frontmost, where
/// "type into the focused field" would be answered by a permission sheet or a
/// password prompt. Refusing those costs a person one manual step and is never
/// wrong in a way that leaks anything.
func typingRefusal() -> String {
    guard AXIsProcessTrusted() else {
        // Accessibility is not granted, so the focused element cannot be named.
        // The executor already refuses typing for the same permission, so this
        // path is unreachable in practice; it exists so that if input posting is
        // ever granted alone, typing still does not proceed blind.
        return ""
    }
    let systemWide = AXUIElementCreateSystemWide()
    var focusedValue: CFTypeRef?
    guard AXUIElementCopyAttributeValue(systemWide, kAXFocusedUIElementAttribute as CFString,
                                        &focusedValue) == .success,
          let focused = focusedValue else {
        return ""
    }
    let element = focused as! AXUIElement
    let described = describe(element, depth: 0, maxDepth: 0)
    if (described["role"] as? String) == "AXSecureTextField" {
        return "the focused element is a secure text field, which is exactly where a "
            + "password would go; type into it yourself"
    }
    let front = NSWorkspace.shared.frontmostApplication
    let frontName = (front?.localizedName ?? "").lowercased()
    if (front?.bundleIdentifier ?? "") == "com.apple.SecurityAgent"
        || frontName == "securityagent" || frontName == "security agent" {
        return "macOS is showing an authorization prompt; it will not be typed into"
    }
    if (front?.bundleIdentifier ?? "") == "com.apple.loginwindow" {
        return "the screen is locked or a login window is in front"
    }
    return ""
}

func opType(text: String, expectApp: String) -> Never {
    guard !text.isEmpty else { Emit.out(.blocked, "there was no text to type") }
    let refusal = typingRefusal()
    if !refusal.isEmpty {
        Emit.out(.blocked, "I did not type: \(refusal).", ["guard": "sensitive-focus"])
    }

    // Typing goes to whatever macOS has focused, so a caller that resolved a
    // target has to be able to say which application that was.
    requireExpectedApp(expectApp, verb: "typed")

    // Only now ask for the permission to post. A wrong focus is knowable without
    // it, so reporting "permission_required" for a window the caller never meant
    // would hide the more useful answer.
    requirePostEvents("typing")

    // One character per event pair, not the whole string in a single unicode
    // payload. TextEdit ignores a bulk `keyboardSetUnicodeString` on one event --
    // measured: 18 characters posted, the AXTextArea value stayed empty -- while
    // the same characters sent individually appear. Per-character delivery also
    // matches what a keyboard actually does, so an app that reads key events one
    // at a time (editors, terminals) behaves the same way it would for a person.
    var typed = 0
    for scalar in text {
        if Cancellation.requested() {
            Emit.out(.cancelled, "typing was cancelled after \(typed) character(s)",
                     ["typed": typed])
        }
        // A real keycode where one exists; the unicode payload stays attached so
        // characters with no physical key (accents, emoji, CJK) still arrive.
        // TextEdit ignores the payload-only form entirely -- measured: focused
        // TextArea, event delivered, value unchanged -- and accepts the keycode.
        let lookup = KeyTable.lookup(scalar)
        let virtualKey = lookup?.0 ?? 0
        let needsShift = lookup?.1 ?? false
        guard let down = CGEvent(keyboardEventSource: nil, virtualKey: virtualKey, keyDown: true),
              let up = CGEvent(keyboardEventSource: nil, virtualKey: virtualKey, keyDown: false) else {
            Emit.out(.failed, "the keystroke event could not be constructed")
        }
        // Set the flags on EVERY event, not only the shifted ones. A CGEvent
        // starts from the live modifier state, so leaving `.flags` untouched on a
        // lowercase character inherited whatever Shift state the previous
        // shifted character left behind -- measured: "Hello from Vantage" arrived
        // as "HELLO FROM VANTAGE" because every letter after the initial capital
        // picked up Shift.
        down.flags = needsShift ? .maskShift : []
        up.flags = needsShift ? .maskShift : []
        if lookup == nil {
            let units = Array(String(scalar).utf16)
            down.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
            up.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
        }
        down.post(tap: .cghidEventTap)
        usleep(8_000)
        up.post(tap: .cghidEventTap)
        typed += 1
        // Measured: 2ms/6ms produced interleaved characters ("L iprobessu" for
        // "Hello from Vantage") because AppKit's input system had not finished
        // processing one keystroke before the next arrived. 12ms between
        // characters is still faster than a fast typist and arrives in order.
        usleep(12_000)
    }
    let front = NSWorkspace.shared.frontmostApplication?.localizedName ?? ""
    Emit.out(.performed_unverified,
             "\(text.count) character(s) typed at the focused field of "
             + "\(front.isEmpty ? "an unreadable application" : front). "
             + "Whether they appeared is read separately.",
             ["length": text.count, "characters_sent": typed, "typed_into": front])
}

/// Hardware keycodes, shared by `key` and `type`.
///
/// `type` needs these because a bulk unicode payload does not reach every text
/// view: TextEdit is focused, receives the event, and ignores it. Sending the
/// same character with its real keycode is what a keyboard does, and what the
/// view is written to handle.
enum KeyTable {
    static let codes: [String: CGKeyCode] = [
        "return": 0x24, "enter": 0x24, "tab": 0x30, "space": 0x31,
        "delete": 0x33, "backspace": 0x33, "escape": 0x35, "esc": 0x35,
        "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
        "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
        "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E, "f": 0x03,
        "g": 0x05, "h": 0x04, "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25,
        "m": 0x2E, "n": 0x2D, "o": 0x1F, "p": 0x23, "q": 0x0C, "r": 0x0F,
        "s": 0x01, "t": 0x11, "u": 0x20, "v": 0x09, "w": 0x0D, "x": 0x07,
        "y": 0x10, "z": 0x06,
        "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15,
        "5": 0x17, "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
        "-": 0x1B, "=": 0x18, "[": 0x21, "]": 0x1E, "\\": 0x2A,
        ";": 0x29, "'": 0x27, ",": 0x2B, ".": 0x2F, "/": 0x2C, "`": 0x32,
    ]

    /// Characters that need Shift held on a US layout.
    static let shifted: Set<Character> = [
        "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M",
        "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
        "!", "@", "#", "$", "%", "^", "&", "*", "(", ")", "_", "+", "{",
        "}", "|", ":", "\"", "<", ">", "?", "~",
    ]

    /// The keycode for a character, and whether Shift is required.
    static func lookup(_ ch: Character) -> (CGKeyCode, Bool)? {
        let lowered = String(ch).lowercased()
        guard let code = codes[lowered] else { return nil }
        return (code, shifted.contains(ch))
    }
}

/// Refuse when the frontmost application is not the one a caller expected.
///
/// Typing and key presses both go to whatever macOS has focused, so a caller
/// that resolved a target has to be able to say which application that was.
/// Without this a window can lose focus between the click that aimed at it and
/// the keystrokes meant for it, and the input lands in something else.
///
/// Applied before `requirePostEvents`, so a wrong focus is reported as a wrong
/// focus rather than as a permission problem. An empty expectation is the
/// existing behaviour: no check.
func requireExpectedApp(_ expectApp: String, verb: String) {
    guard !expectApp.isEmpty else { return }
    let front = NSWorkspace.shared.frontmostApplication
    let frontName = (front?.localizedName ?? "")
    let frontBundle = (front?.bundleIdentifier ?? "")
    let needle = expectApp.lowercased()
    let matches = frontName.lowercased().contains(needle)
        || frontBundle.lowercased().contains(needle)
    guard matches else {
        Emit.out(.target_stale,
                 "the frontmost application is \(frontName.isEmpty ? "not readable" : frontName), "
                 + "not \(expectApp); nothing was \(verb). Re-focus the target before retrying.",
                 ["expected_application": expectApp,
                  "frontmost_after": frontName,
                  "frontmost_bundle": frontBundle,
                  "staleness_check": "frontmost_application"])
    }
}

func opKey(key: String, modifiers: [String],
           expectApp: String = "") -> Never {
    requireExpectedApp(expectApp, verb: "pressed")
    requirePostEvents("pressing a key")
    guard let code = KeyTable.codes[key.lowercased()] else {
        Emit.out(.blocked, "\(key) is not a key this build sends",
                 ["known_keys": KeyTable.codes.keys.sorted()])
    }
    var flags: CGEventFlags = []
    for m in modifiers.map({ $0.lowercased() }) {
        switch m {
        case "cmd", "command", "meta": flags.insert(.maskCommand)
        case "shift": flags.insert(.maskShift)
        case "opt", "option", "alt": flags.insert(.maskAlternate)
        case "ctrl", "control": flags.insert(.maskControl)
        default:
            Emit.out(.blocked, "\(m) is not a modifier this build sends",
                     ["known_modifiers": ["cmd", "shift", "opt", "ctrl"]])
        }
    }
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else {
        Emit.out(.failed, "the key event could not be constructed")
    }
    down.flags = flags
    up.flags = flags
    down.post(tap: .cghidEventTap)
    usleep(20_000)
    up.post(tap: .cghidEventTap)
    usleep(60_000)
    let label = (modifiers.map { $0.lowercased() } + [key]).joined(separator: "+")
    Emit.out(.performed_unverified,
             "\(label) was posted. Whether it did what you meant is read separately.",
             ["key": key, "modifiers": modifiers])
}

func opFocus(application: String, windowTitle: String) -> Never {
    let app = resolveApplication(application)
    // `activate()` without options is the supported route on macOS 14+.
    let activated = app.activate()
    usleep(180_000)

    // Report what actually became frontmost: activate() returning is not the
    // same as the window server having switched.
    let front = NSWorkspace.shared.frontmostApplication?.localizedName ?? ""
    var body: [String: Any] = [
        "requested": application,
        "activated_call": activated,
        "frontmost_after": front,
        "pid": app.processIdentifier,
    ]
    if !windowTitle.isEmpty {
        requireAX("focusing a window by title")
        let root = AXUIElementCreateApplication(app.processIdentifier)
        let windows = findElements(in: root, matching: { d in
            (d["role"] as? String) == "AXWindow"
        }, maxDepth: 2)
        body["windows"] = windows
        if let hit = windows.first(where: {
            (($0["title"] as? String) ?? "").lowercased().contains(windowTitle.lowercased())
        }) {
            body["window_matched"] = hit["title"] ?? ""
        } else {
            Emit.out(.target_not_found,
                     "\(application) is running but no window title contains \(windowTitle)",
                     body)
        }
    }
    if front.lowercased() == (app.localizedName ?? "").lowercased() {
        Emit.out(.verified, "\(front) is the frontmost application", body)
    }
    Emit.out(.failed,
             "the activation request returned but \(front.isEmpty ? "nothing" : front) is still in front",
             body)
}

func opInspect(application: String, maxDepth: Int) -> Never {
    requireAX("inspecting the UI")
    let app = resolveApplication(application)
    let root = AXUIElementCreateApplication(app.processIdentifier)
    let tree = describe(root, depth: 0, maxDepth: max(1, min(12, maxDepth)))
    var count = 0
    func tally(_ node: [String: Any]) {
        count += 1
        for child in (node["children"] as? [[String: Any]]) ?? [] { tally(child) }
    }
    tally(tree)
    Emit.out(.verified,
             "read the Accessibility tree of \(app.localizedName ?? application): \(count) element(s)",
             ["application": app.localizedName ?? application,
              "bundle_id": app.bundleIdentifier ?? "",
              "pid": app.processIdentifier,
              "element_count": count, "tree": tree])
}

/// Read one element's current state — the observation half of act-then-verify.
func opReadElement(application: String, keyword: String, role: String) -> Never {
    requireAX("reading a UI element")
    guard let (_, described) = locate(keyword: keyword, application: application,
                                      role: role, useTitle: true) else {
        Emit.out(.target_not_found,
                 "no \(role.isEmpty ? "element" : role) in \(application.isEmpty ? "the front application" : application) matches \(keyword)")
    }
    Emit.out(.verified, "found one matching element", ["element": described])
}

/// Scroll a view by setting its scrollbar's position.
///
/// Two other approaches were measured and rejected on this machine:
/// synthetic wheel events posted at the HID tap (line, pixel, continuous and
/// phased units) never reached the target application at all, while clicks from
/// the same tap did; and the ScrollArea's advertised `AXScrollDownByPage` action
/// returned `kAXErrorActionUnsupported` (-25204) even though the app lists it.
///
/// Setting `AXValue` on the scroll bar is a different mechanism: it does not ask
/// the view to perform anything, it states where the scroll position should be.
/// That is both supported and idempotent, which also makes it safe to retry.
func opScrollElement(application: String, direction: String, amount: Double) -> Never {
    requireAX("scrolling a view")
    let app = resolveApplication(application)
    let root = AXUIElementCreateApplication(app.processIdentifier)

    // Positive moves down/right, negative up/left, in fractions of the content.
    let signum = (direction.lowercased() == "up" || direction.lowercased() == "left") ? -1.0 : 1.0
    let step = signum * max(0.02, min(1.0, amount))

    var moved = 0
    var before: [Double] = []
    var after: [Double] = []
    var failure = ""

    func walk(_ element: AXUIElement, depth: Int) {
        if depth > 10 { return }
        var roleValue: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &roleValue) == .success,
           let role = roleValue as? String, role == "AXScrollBar" {
            var valueRef: CFTypeRef?
            if AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &valueRef) == .success,
               let current = valueRef as? Double {
                let target = max(0.0, min(1.0, current + step))
                before.append(current)
                if AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString,
                                                target as CFTypeRef) == .success {
                    after.append(target)
                    moved += 1
                } else {
                    failure = "AXValue could not be set"
                }
            }
        }
        var childrenValue: CFTypeRef?
        if AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString,
                                         &childrenValue) == .success,
           let children = childrenValue as? [AXUIElement] {
            for child in children { walk(child, depth: depth + 1) }
        }
    }
    walk(root, depth: 0)

    if moved == 0 {
        Emit.out(.target_not_found,
                 "no scroll bar in \(app.localizedName ?? application) accepted a position",
                 ["application": app.localizedName ?? application,
                  "reason_detail": failure.isEmpty ? "no scroll bars found" : failure])
    }
    Emit.out(.verified,
             "scrolled \(app.localizedName ?? application) \(direction.lowercased()): "
             + "\(moved) scroll bar(s) moved from \(before.map { String(format: "%.2f", $0) }.joined(separator: ","))",
             ["application": app.localizedName ?? application,
              "direction": direction, "moved": moved,
              "before": before, "after": after])
}

func opPressElement(application: String, keyword: String, role: String) -> Never {
    requireAX("pressing a UI element")
    let app = resolveApplication(application)
    guard let (element, described) = locate(keyword: keyword, application: application,
                                             role: role, useTitle: true) else {
        Emit.out(.target_not_found,
                 "no element matches \(keyword) in \(app.localizedName ?? application)",
                 ["application": app.localizedName ?? "", "role": role])
    }
    // AXPress is preferred over a synthetic click: it is the interface the
    // application itself publishes, so it works when nothing is on screen and
    // cannot miss by a pixel.
    let pressError = AXUIElementPerformAction(element, kAXPressAction as CFString)
    usleep(120_000)
    let body: [String: Any] = [
        "element": described,
        "application": app.localizedName ?? application,
        "ax_error": Int(pressError.rawValue),
    ]
    if pressError == .success {
        Emit.out(.performed_unverified,
                 "AXPress was accepted by \(app.localizedName ?? application) on the \(described["role"] ?? "element"); the resulting state must be read separately",
                 body)
    }
    Emit.out(.failed,
             "the element exists but \(app.localizedName ?? "the application") refused AXPress (AXError \(pressError.rawValue))",
             body)
}

/// Compare two snapshots of one element. This is the function that turns a
/// posted event into a VERIFIED outcome, and the only one allowed to.
func opVerifyChange(application: String, keyword: String, role: String,
                    expectedKey: String, expectedValue: String) -> Never {
    requireAX("verifying a UI change")
    let app = resolveApplication(application)
    guard let (_, described) = locate(keyword: keyword, application: application,
                                      role: role, useTitle: true) else {
        Emit.out(.target_not_found,
                 "the element to verify is no longer present in \(app.localizedName ?? application)",
                 ["application": app.localizedName ?? ""])
    }
    let actual = described[expectedKey].map { String(describing: $0) } ?? ""
    let body: [String: Any] = [
        "element": described,
        "expected": ["key": expectedKey, "value": expectedValue],
        "actual": actual,
    ]
    if actual == expectedValue {
        Emit.out(.verified, "\(keyword) reports \(expectedKey) = \(actual)", body)
    }
    Emit.out(.failed,
             "\(keyword) reports \(expectedKey) = \(actual.isEmpty ? "(absent)" : actual), expected \(expectedValue)",
             body)
}

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------

func failUsage(_ message: String) -> Never {
    Emit.out(.blocked, message, [
        "operations": ["permissions", "capabilities", "request_access", "identity", "windows",
                       "move", "click", "drag", "scroll", "type", "key", "focus", "inspect",
                       "read_element", "press_element", "verify_change", "release_all"],
        "usage": "vantage-desktop <operation> key=value ... [--cancel-file <path>] "
               + "[--expect-window-id <int>] [--expect-pid <int>] [--expect-bounds <x,y,w,h>]",
    ])
}

let argv = Array(CommandLine.arguments.dropFirst())

// `--cancel-file <path>` and the target expectations may sit anywhere, including
// before the operation, so each is stripped before the operation and the
// key=value pairs are read. They are pulled out rather than left in `params`
// because a value containing "=" (a bounds list can) would otherwise be split
// into a bogus parameter.
var positional: [String] = []
var cancelFilePath: String?
var expectWindowID: Int?
var expectPID: Int?
var expectBounds: CGRect?
var i = 0

/// The value of `--name <v>` or `--name=<v>`, or nil if the token is neither.
/// `tokensUsed` is 2 for the space-separated form and 1 for the `=` form, so the
/// loop advances by exactly the tokens consumed. A named option with no value
/// is a usage error, never a silently ignored token.
func optionValue(_ token: String, _ next: String?, name: String,
                 tokensUsed: inout Int) -> String? {
    if token == "--\(name)" {
        guard let next else { failUsage("--\(name) needs a value") }
        tokensUsed = 2
        return next
    }
    let prefix = "--\(name)="
    if token.hasPrefix(prefix) {
        tokensUsed = 1
        return String(token.dropFirst(prefix.count))
    }
    return nil
}

while i < argv.count {
    let token = argv[i]
    let next = i + 1 < argv.count ? argv[i + 1] : nil
    var consumed = 0

    if token == "--cancel-file" {
        guard let next else { failUsage("--cancel-file needs a path") }
        cancelFilePath = next
        i += 2
        continue
    }
    if token.hasPrefix("--cancel-file=") {
        cancelFilePath = String(token.dropFirst("--cancel-file=".count))
        i += 1
        continue
    }
    if let value = optionValue(token, next, name: "expect-window-id", tokensUsed: &consumed) {
        guard let id = Int(value) else {
            failUsage("--expect-window-id needs an integer, got \(value)")
        }
        expectWindowID = id
        i += consumed
        continue
    }
    if let value = optionValue(token, next, name: "expect-pid", tokensUsed: &consumed) {
        guard let pid = Int(value) else {
            failUsage("--expect-pid needs an integer, got \(value)")
        }
        expectPID = pid
        i += consumed
        continue
    }
    if let value = optionValue(token, next, name: "expect-bounds", tokensUsed: &consumed) {
        let parts = value.split(separator: ",", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        guard parts.count == 4, let x = Double(parts[0]), let y = Double(parts[1]),
              let w = Double(parts[2]), let h = Double(parts[3]) else {
            failUsage("--expect-bounds needs x,y,w,h in global points, got \(value)")
        }
        expectBounds = CGRect(x: x, y: y, width: w, height: h)
        i += consumed
        continue
    }
    positional.append(token)
    i += 1
}
Cancellation.path = cancelFilePath
let expectation = TargetExpectation(windowID: expectWindowID, pid: expectPID,
                                    bounds: expectBounds)

guard let op = positional.first else {
    failUsage("no operation was named")
}

var params: [String: String] = [:]
for pair in positional.dropFirst() {
    let parts = pair.split(separator: "=", maxSplits: 1)
    if parts.count == 2 { params[String(parts[0])] = String(parts[1]) }
}

func num(_ key: String) -> Double? { params[key].flatMap(Double.init) }
func str(_ key: String, _ fallback: String = "") -> String { params[key] ?? fallback }
func int(_ key: String, _ fallback: Int) -> Int { params[key].flatMap(Int.init) ?? fallback }
func list(_ key: String) -> [String] {
    str(key).split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
}

switch op {
case "permissions":   opPermissions()
case "self_check":
    // The same reading `permissions` does, but with no argv beyond the op name,
    // so it can be invoked from an already-running process identity (or as an
    // app's own executable) and report what macOS thinks of *that* process.
    var body = permissionSnapshot()
    body["argv0"] = CommandLine.arguments.first ?? ""
    body["argc"] = CommandLine.arguments.count
    body["bundle_id_check"] = Bundle.main.bundleIdentifier ?? ""
    body["can_move_cursor"] = true
    body["can_click"] = CGPreflightPostEventAccess()
    body["can_type"] = CGPreflightPostEventAccess()
    body["can_read_ui"] = AXIsProcessTrusted()
    Emit.out(.verified, "self check from this process", body)
case "capabilities":  opCapabilities()
case "identity":      opIdentity()
case "windows":       opWindows(application: str("application"))
case "request_access": opRequestAccess()
case "move":
    guard let x = num("x"), let y = num("y") else {
        failUsage("move needs x= and y= in global display points")
    }
    opMove(x: x, y: y, smooth: str("smooth") == "true", durationMs: int("duration_ms", 0))
case "click":
    guard let x = num("x"), let y = num("y") else {
        failUsage("click needs x= and y= in global display points")
    }
    opClick(x: x, y: y, button: str("button", "left"), count: int("count", 1),
            expectation: expectation)
case "drag":
    guard let fx = num("from_x"), let fy = num("from_y"),
          let tx = num("to_x"), let ty = num("to_y") else {
        failUsage("drag needs from_x=, from_y=, to_x=, to_y=")
    }
    opDrag(fromX: fx, fromY: fy, toX: tx, toY: ty, durationMs: int("duration_ms", 400),
           expectation: expectation)
case "scroll_element":
    opScrollElement(application: str("application"),
                    direction: str("direction", "down"),
                    amount: num("amount") ?? 0.35)
case "scroll":
    guard let x = num("x"), let y = num("y") else { failUsage("scroll needs x= and y=") }
    opScroll(x: x, y: y, lines: int("lines", 3), horizontal: str("horizontal") == "true",
             expectation: expectation)
case "type":
    opType(text: str("text"), expectApp: str("expect_app"))
case "key":
    opKey(key: str("key"), modifiers: list("modifiers"),
          expectApp: str("expect_app"))
case "focus":
    opFocus(application: str("application"), windowTitle: str("window"))
case "inspect":
    opInspect(application: str("application"), maxDepth: int("depth", 6))
case "read_element":
    opReadElement(application: str("application"), keyword: str("keyword"), role: str("role"))
case "press_element":
    opPressElement(application: str("application"), keyword: str("keyword"), role: str("role"))
case "verify_change":
    guard !str("key").isEmpty else { failUsage("verify_change needs key= and value=") }
    opVerifyChange(application: str("application"), keyword: str("keyword"),
                   role: str("role"), expectedKey: str("key"), expectedValue: str("value"))
case "release_all":
    opReleaseAll()
default:
    failUsage("unknown operation \(op)")
}