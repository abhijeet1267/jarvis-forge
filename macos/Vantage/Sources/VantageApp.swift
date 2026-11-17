import SwiftUI

@main
struct VantageApp: App {

    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var backend = BackendManager.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .frame(minWidth: 800, minHeight: 520)

        }
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(replacing: .appInfo) {
                Button("About Vantage") {
                    NSApp.orderFrontStandardAboutPanel(nil)
                }
            }
            CommandMenu("Backend") {
                Button("Connect") {
                    Task { await backend.connect() }
                }
                .keyboardShortcut("r", modifiers: [.command, .shift])
                Button("Disconnect") {
                    backend.disconnect()
                }
                Divider()
                Button("Refresh Health") {
                    Task { await backend.refreshHealth() }
                }
                .keyboardShortcut("r", modifiers: [.command])
            }
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {

    /// Never restore a previous session's windows.
    ///
    /// macOS persists per-bundle-id window state, and a stale entry can leave the app
    /// running with *no window at all* — the process is alive, but the SwiftUI scene is
    /// never activated, so `.task` never fires and the app never connects to the
    /// backend. Measured on this machine: the same binary, same bundle, showed zero
    /// windows on launch and a window on the next line of the same A/B, differing only
    /// by `-ApplePersistenceIgnoreState YES`.
    ///
    /// The window is rebuilt from live backend state on every launch anyway (the
    /// sidebar, the metrics and the connection badge are all fetched fresh), so
    /// restoring a remembered frame buys nothing and can cost the user a window that
    /// never appears. Setting the default before AppKit restores state is what makes
    /// this take effect.
    func applicationWillFinishLaunching(_ notification: Notification) {
        // Refuse to be a second instance before any backend work begins. This is
        // the fix for a measured incident: a development build launched with a
        // flag that does not exist started a second GUI whose backend bound :8765
        // and displaced the running app, orphaning its backend. One process must
        // never be able to do that to another.
        //
        // The check is a kernel exclusive lock, not a bundle-id lookup, because a
        // dev binary has no bundle id and is exactly the case that caused it.
        if !InstanceGuard.acquire() {
            InstanceGuard.activateExistingAndExit()
        }

        UserDefaults.standard.register(defaults: [
            "ApplePersistenceIgnoreState": true,
            "NSQuitAlwaysKeepsWindows": false,
        ])
    }





    /// The menu bar companion. Created once, on launch, holding only the window
    /// and palette it already owns — no second app, no helper process.
    private var statusItem: NSStatusItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Register the global shortcut and build the menu bar item once. Both are
        // idempotent (`start` guards on its own flag), so a restored session that
        // calls this twice cannot double-register anything.
        AnywhereController.shared.start()
        installMenuBarItem()
        installTerminationSignals()
    }

    /// Tear down on SIGTERM and SIGINT as well as on a normal Quit.
    ///
    /// `applicationWillTerminate` only runs for AppKit's own quit path. Measured:
    /// `kill -TERM` left the app running *and* its backend orphaned on the port,
    /// because nothing caught the signal. A service that spawns a child has to
    /// stop it on every exit route, not just the polite one.
    ///
    /// The handler runs the same teardown, then exits — doing it on the signal
    /// itself rather than dispatching to the main queue, because a second signal
    /// while waiting would kill the process with the child still alive.
    private func installTerminationSignals() {
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)          // stop the default "die now" behaviour
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler {
                BackendManager.shared.disconnect()
                AnywhereController.shared.stop()
                exit(0)
            }
            source.resume()
            terminationSources.append(source)   // keep alive: a released source stops firing
        }
    }

    /// A lightweight menu bar entry: the palette is reachable even if the
    /// shortcut is taken by another app, which is the honest fallback the brief
    /// asks for.
    private func installMenuBarItem() {
        guard statusItem == nil else { return }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "sparkle",
                                     accessibilityDescription: "Vantage")
        item.button?.image?.isTemplate = true
        let menu = NSMenu()
        menu.addItem(withTitle: "Vantage Anywhere…  \u{2325}\u{2423}",
                     action: #selector(openAnywhere), keyEquivalent: "")
        menu.addItem(withTitle: "Open Vantage", action: #selector(openMainWindow),
                     keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Recent commands", action: nil, keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Vantage", action: #selector(quit), keyEquivalent: "q")
        for menuItem in menu.items where menuItem.action != nil {
            menuItem.target = self
        }
        // Fill "Recent commands" from the palette's own store so the menu and the
        // palette never disagree about what was run.
        let recentIndex = menu.items.firstIndex { $0.title == "Recent commands" }
        if let recentIndex, !AnywhereController.shared.recent.isEmpty {
            let submenu = NSMenu()
            for text in AnywhereController.shared.recent.prefix(6) {
                let entry = NSMenuItem(title: text, action: #selector(runRecent(_:)),
                                       keyEquivalent: "")
                entry.target = self
                entry.representedObject = text
                submenu.addItem(entry)
            }
            menu.items[recentIndex].submenu = submenu
        } else if let recentIndex {
            menu.items[recentIndex].isEnabled = false
        }
        item.menu = menu
        statusItem = item
    }

    @objc private func openAnywhere() { AnywhereController.shared.show() }
    @objc private func openMainWindow() { AnywhereController.shared.openMainWindow() }

    @objc private func runRecent(_ sender: NSMenuItem) {
        guard let text = sender.representedObject as? String else { return }
        AnywhereController.shared.show()
        NotificationCenter.default.post(name: .anywhereAskConversation,
                                        object: nil, userInfo: ["prefill": text])
    }

    @objc private func quit() { NSApp.terminate(nil) }

    /// Held so the signal sources stay alive for the process's lifetime.
    private var terminationSources: [DispatchSourceSignal] = []

    func applicationWillTerminate(_ notification: Notification) {
        // Release the Carbon hotkey and its handler before the process goes away,
        // so a relaunch cannot inherit a registration the old process still owns.
        AnywhereController.shared.stop()
        if let statusItem {
            NSStatusBar.system.removeStatusItem(statusItem)
            self.statusItem = nil
        }
        BackendManager.shared.disconnect()
    }
}