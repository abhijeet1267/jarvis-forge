import AppKit
import Carbon

@MainActor
final class Probe {
    let hk = GlobalHotKey()
    var fires = 0
    func run() {
        let ok = hk.register { [weak self] in self?.fires += 1 }
        print("\(ok ? "PASS" : "FAIL")  register -> \(hk.registration)")

        func sendHotKeyEvent() {
            var id = EventHotKeyID(signature: 0x564E5447, id: 1)
            var e: EventRef?
            CreateEvent(nil, OSType(kEventClassKeyboard), UInt32(kEventHotKeyPressed),
                        GetCurrentEventTime(), 0, &e)
            if let e {
                SetEventParameter(e, EventParamName(kEventParamDirectObject),
                                  EventParamType(typeEventHotKeyID),
                                  MemoryLayout<EventHotKeyID>.size, &id)
                SendEventToEventTarget(e, GetApplicationEventTarget())
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.35))
        }

        sendHotKeyEvent()
        print("\(fires == 1 ? "PASS" : "FAIL")  fired once (got \(fires))")

        let before = fires
        _ = hk.update(ShortcutSpec(keyCode: UInt32(kVK_ANSI_J),
                                   modifiers: UInt32(optionKey))) { [weak self] in
            self?.fires += 1
        }
        print("\(hk.isActive ? "PASS" : "FAIL")  updated active -> \(hk.current.displayString)")
        sendHotKeyEvent()
        print("\(fires - before == 1 ? "PASS" : "FAIL")  one handler after re-registration (delta \(fires - before))")

        hk.shutdown()
        print("\(hk.registration == .inactive ? "PASS" : "FAIL")  shutdown deactivates")
        let after = fires
        sendHotKeyEvent()
        print("\(fires == after ? "PASS" : "FAIL")  silent after shutdown")

        let decoded = UserDefaults.standard.data(forKey: "vantage.anywhere.shortcut")
            .flatMap { try? JSONDecoder().decode(ShortcutSpec.self, from: $0) }
        print("\(decoded?.displayString == "⌥J" ? "PASS" : "FAIL")  persisted (\(decoded?.displayString ?? "nil"))")
        UserDefaults.standard.removeObject(forKey: "vantage.anywhere.shortcut")
        NSApp.terminate(nil)
    }
}
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
Task { @MainActor in
    let p = Probe()
    p.run()
}
app.run()
