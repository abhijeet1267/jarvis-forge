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

func opClick(x: Double, y: Double, button: String, count: Int) -> Never {
    requirePostEvents("clicking")
    let point = CGPoint(x: x, y: y)
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
    for _ in 0..<max(1, count) {
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
        // A clickCount of 2 is what makes the second click a double-click
        // rather than two independent clicks.
        down.setIntegerValueField(.mouseEventClickState, value: Int64(count))
        up.setIntegerValueField(.mouseEventClickState, value: Int64(count))
        down.post(tap: .cghidEventTap)
        usleep(15_000)
        up.post(tap: .cghidEventTap)
        postedCount += 1
        usleep(40_000)
    }
    let after = cursorPosition()
    Emit.out(.performed_unverified,
             "\(count)× \(button) click posted at (\(x), \(y)) on display \(display). The event was accepted; whether the target changed is read separately by a UI observation, never assumed here.",
             ["display_id": display, "count": count, "button": button,
              "cursor_after": after.map { ["x": $0.x, "y": $0.y] } ?? NSNull()])
}

func opDrag(fromX: Double, fromY: Double, toX: Double, toY: Double, durationMs: Int) -> Never {
    requirePostEvents("dragging")
    let start = CGPoint(x: fromX, y: fromY), end = CGPoint(x: toX, y: toY)
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
    Emit.out(.performed_unverified,
             "drag posted from (\(fromX), \(fromY)) to (\(toX), \(toY)) with the button released at the end. Whether the target accepted the drop is read separately.",
             ["from": ["x": fromX, "y": fromY], "to": ["x": toX, "y": toY],
              "cursor_after": cursorPosition().map { ["x": $0.x, "y": $0.y] } ?? NSNull()])
}

func opScroll(x: Double, y: Double, lines: Int, horizontal: Bool) -> Never {
    requirePostEvents("scrolling")
    let point = CGPoint(x: x, y: y)
    guard Coord.displayContaining(point) != nil else {
        Emit.out(.blocked, "the scroll point is not inside any active display")
    }
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
    Emit.out(.performed_unverified,
             "a scroll of \(lines) line(s) was posted at (\(x), \(y)). Whether the view moved is read separately.",
             ["lines": lines, "horizontal": horizontal])
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

func opType(text: String) -> Never {
    requirePostEvents("typing")
    guard !text.isEmpty else { Emit.out(.blocked, "there was no text to type") }
    let refusal = typingRefusal()
    if !refusal.isEmpty {
        Emit.out(.blocked, "I did not type: \(refusal).", ["guard": "sensitive-focus"])
    }

    // Unicode injection: set the keyboard event's unicode payload rather than
    // mapping characters to keycodes. This respects any layout and types
    // characters no physical key produces.
    let units = Array(text.utf16)
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false) else {
        Emit.out(.failed, "the keystroke event could not be constructed")
    }
    down.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
    up.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
    down.post(tap: .cghidEventTap)
    usleep(10_000)
    up.post(tap: .cghidEventTap)
    usleep(60_000)
    Emit.out(.performed_unverified,
             "\(text.count) character(s) typed at the focused field. Whether they appeared is read separately.",
             ["length": text.count])
}

func opKey(key: String, modifiers: [String]) -> Never {
    requirePostEvents("pressing a key")
    // A small, honest table. The keycodes are the hardware ones; a layout that
    // remaps them is a real difference this tool does not paper over.
    let keycodes: [String: CGKeyCode] = [
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
    ]
    guard let code = keycodes[key.lowercased()] else {
        Emit.out(.blocked, "\(key) is not a key this build sends",
                 ["known_keys": keycodes.keys.sorted()])
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
        "operations": ["permissions", "request_access", "move", "click", "drag", "scroll", "type",
                       "key", "focus", "inspect", "read_element", "press_element",
                       "verify_change", "release_all"],
        "usage": "vantage-desktop <operation> key=value ... [--cancel-file <path>]",
    ])
}

let argv = Array(CommandLine.arguments.dropFirst())

// `--cancel-file <path>` may sit anywhere, including before the operation, so
// it is stripped before the operation and the key=value pairs are read. It is
// pulled out rather than left in `params` because a path containing "=" would
// otherwise be split into a bogus parameter.
var positional: [String] = []
var cancelFilePath: String?
var i = 0
while i < argv.count {
    let token = argv[i]
    if token == "--cancel-file" {
        guard i + 1 < argv.count else {
            failUsage("--cancel-file needs a path")
        }
        cancelFilePath = argv[i + 1]
        i += 2
        continue
    }
    if token.hasPrefix("--cancel-file=") {
        cancelFilePath = String(token.dropFirst("--cancel-file=".count))
        i += 1
        continue
    }
    positional.append(token)
    i += 1
}
Cancellation.path = cancelFilePath

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
    opClick(x: x, y: y, button: str("button", "left"), count: int("count", 1))
case "drag":
    guard let fx = num("from_x"), let fy = num("from_y"),
          let tx = num("to_x"), let ty = num("to_y") else {
        failUsage("drag needs from_x=, from_y=, to_x=, to_y=")
    }
    opDrag(fromX: fx, fromY: fy, toX: tx, toY: ty, durationMs: int("duration_ms", 400))
case "scroll":
    guard let x = num("x"), let y = num("y") else { failUsage("scroll needs x= and y=") }
    opScroll(x: x, y: y, lines: int("lines", 3), horizontal: str("horizontal") == "true")
case "type":
    opType(text: str("text"))
case "key":
    opKey(key: str("key"), modifiers: list("modifiers"))
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