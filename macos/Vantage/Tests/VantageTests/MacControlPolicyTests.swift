import XCTest
@testable import Vantage

/// The Mac-control boundary, exercised without touching the system.
///
/// Everything here runs through the real `MacActionRouter` and real path guard;
/// only the permission answer and the executor are substituted. That is the
/// point: the checks that decide whether an action may run must not need a
/// live desktop to be proven.
final class MacControlPolicyTests: XCTestCase {

    /// A router whose permission answer is fixed, so a test states the
    /// situation explicitly instead of inheriting the machine's.
    private func router(accessibility: Bool?, automationOK: Bool = true) -> MacActionRouter {
        MacActionRouter(permissions: {
            MacPermissionStatus(accessibilityGranted: accessibility)
        })
    }

    // MARK: - Every identifier has a policy

    func test_every_action_kind_has_a_policy_row() {
        for kind in MacActionKind.allCases {
            XCTAssertNotNil(MacActionPolicy.forKind(kind),
                            "\(kind.id) has no policy; an unlisted action would fall through")
        }
        XCTAssertEqual(MacActionPolicy.all.count, MacActionKind.allCases.count)
    }

    func test_identifiers_are_unique_and_stable() {
        let ids = MacActionKind.allCases.map(\.id)
        XCTAssertEqual(Set(ids).count, ids.count)
        // These are what the history and tests refer to; a rename is a change
        // of contract, so it should be a deliberate edit here too.
        XCTAssertTrue(ids.contains("open_application"))
        XCTAssertTrue(ids.contains("set_volume"))
        XCTAssertTrue(ids.contains("minimize_window"))
    }

    func test_unknown_action_ids_are_refused() {
        XCTAssertThrowsError(try router(accessibility: true)
            .prepare(kindID: "run_shell", arguments: MacActionArguments(), origin: .local)) { error in
            XCTAssertEqual(error as? MacControlRefusal, .unknownAction("run_shell"))
        }
    }

    // MARK: - Risk classes

    func test_risk_classes_match_the_policy() {
        XCTAssertEqual(MacActionPolicy.forKind(.frontmostApp)?.risk, .read)
        XCTAssertEqual(MacActionPolicy.forKind(.openApplication)?.risk, .low)
        XCTAssertEqual(MacActionPolicy.forKind(.closeWindow)?.risk, .mutating)
        XCTAssertEqual(MacActionPolicy.forKind(.quitApplication)?.risk, .mutating)
        XCTAssertEqual(MacActionPolicy.forKind(.setVolume)?.risk, .low)
    }

    // MARK: - Arguments

    func test_a_required_argument_cannot_be_missing() {
        XCTAssertThrowsError(try router(accessibility: true)
            .prepare(kind: .openApplication, arguments: MacActionArguments(), origin: .local)) { error in
            guard case .missingArgument(.openApplication, _)? = error as? MacControlRefusal else {
                return XCTFail("expected missingArgument, got \(error)")
            }
        }
    }

    func test_only_http_and_https_urls_are_accepted() {
        var args = MacActionArguments()
        for bad in ["file:///etc/passwd", "javascript:alert(1)", "x-apple.systempreferences:com.apple",
                    "not a url", ""] {
            args.url = bad
            XCTAssertThrowsError(try router(accessibility: true)
                .prepare(kind: .openURL, arguments: args, origin: .local), "accepted \(bad)")
        }
        args.url = "https://example.com/page"
        XCTAssertNoThrow(try router(accessibility: true)
            .prepare(kind: .openURL, arguments: args, origin: .local))
    }

    func test_volume_percent_is_bounded() {
        var args = MacActionArguments()
        for bad in [-1, 101, 500] {
            args.percent = bad
            XCTAssertThrowsError(try router(accessibility: true)
                .prepare(kind: .setVolume, arguments: args, origin: .local))
        }
        for good in [0, 30, 100] {
            args.percent = good
            XCTAssertNoThrow(try router(accessibility: true)
                .prepare(kind: .setVolume, arguments: args, origin: .local))
        }
    }

    func test_an_unsupported_settings_pane_is_refused() {
        var args = MacActionArguments()
        args.settingsPane = "firewall"
        XCTAssertThrowsError(try router(accessibility: true)
            .prepare(kind: .openSystemSettings, arguments: args, origin: .local))
    }

    // MARK: - Permissions

    func test_window_actions_are_blocked_without_accessibility() {
        for kind in [MacActionKind.minimizeWindow, .closeWindow, .arrangeWindow,
                     .listWindows, .focusWindow] {
            XCTAssertThrowsError(
                try router(accessibility: false).prepare(kind: kind,
                                                         arguments: MacActionArguments(),
                                                         origin: .local),
                "\(kind.id) ran without Accessibility") { error in
                guard case .permissionRequired(.accessibility, _)? = error as? MacControlRefusal else {
                    return XCTFail("expected a permission refusal for \(kind.id), got \(error)")
                }
            }
        }
    }

    func test_unknown_permission_state_is_treated_as_not_granted() {
        // The native call fails closed, so claiming the capability would be a lie.
        XCTAssertThrowsError(try router(accessibility: nil)
            .prepare(kind: .minimizeWindow, arguments: MacActionArguments(), origin: .local))
    }

    func test_actions_that_need_no_permission_still_pass() {
        XCTAssertNoThrow(try router(accessibility: false)
            .prepare(kind: .openApplication,
                     arguments: { var a = MacActionArguments(); a.application = "Finder"; return a }(),
                     origin: .local))
    }

    // MARK: - Caller authorization

    func test_a_backend_request_cannot_run_a_mutating_action_unattended() {
        for kind in [MacActionKind.closeWindow, .quitApplication] {
            XCTAssertThrowsError(
                try router(accessibility: true).prepare(kind: kind,
                                                        arguments: { var a = MacActionArguments(); a.application = "TextEdit"; return a }(),
                                                        origin: .backend),
                "\(kind.id) was allowed from the backend") { error in
                guard case .confinedByRisk? = error as? MacControlRefusal else {
                    return XCTFail("expected confinedByRisk for \(kind.id), got \(error)")
                }
            }
        }
    }

    func test_a_backend_request_may_run_low_risk_actions() {
        var args = MacActionArguments()
        args.application = "Finder"
        XCTAssertNoThrow(try router(accessibility: true)
            .prepare(kind: .openApplication, arguments: args, origin: .backend))
    }

    func test_an_external_caller_may_only_read() {
        XCTAssertNoThrow(try router(accessibility: true)
            .prepare(kind: .frontmostApp, arguments: MacActionArguments(), origin: .external))
        var args = MacActionArguments(); args.application = "Finder"
        XCTAssertThrowsError(try router(accessibility: true)
            .prepare(kind: .openApplication, arguments: args, origin: .external))
    }

    // MARK: - Path guard

    func test_protected_locations_are_refused() {
        for path in ["/System/Library", "/usr/bin", "/Library/Preferences", "/private/etc"] {
            XCTAssertNotNil(MacPathGuard.refusal(for: URL(fileURLWithPath: path)),
                            "\(path) was not refused")
        }
    }

    func test_credential_stores_under_home_are_refused() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        for sub in [".ssh", ".aws/credentials", "Library/Keychains/login.keychain-db",
                    ".zshrc", "Library/LaunchAgents/com.evil.plist"] {
            XCTAssertNotNil(MacPathGuard.refusal(for: home.appendingPathComponent(sub)),
                            "\(sub) was not refused")
        }
    }

    func test_ordinary_folders_are_allowed() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        for sub in ["Downloads", "Documents", "Desktop", "Pictures/project.png"] {
            XCTAssertNil(MacPathGuard.refusal(for: home.appendingPathComponent(sub)),
                         "\(sub) was refused")
        }
    }

    func test_scratch_space_is_allowed_despite_the_private_root_rule() {
        XCTAssertNil(MacPathGuard.refusal(for: URL(fileURLWithPath: "/tmp/vantage-test/file.txt")))
    }

    func test_relative_paths_are_refused_and_named_folders_resolve() throws {
        XCTAssertThrowsError(try MacPathGuard.resolve("some/relative/path"))
        XCTAssertEqual(try MacPathGuard.resolve("Downloads").lastPathComponent, "Downloads")
        XCTAssertEqual(try MacPathGuard.resolve("~").path,
                       FileManager.default.homeDirectoryForCurrentUser.path)
    }

    func test_new_names_cannot_traverse_or_hide() {
        for bad in ["", "..", ".", "a/b", ".hidden", "with:colon"] {
            XCTAssertThrowsError(try MacPathGuard.validateNewName(bad), "accepted \(bad)")
        }
        XCTAssertEqual(try MacPathGuard.validateNewName("Project Notes"), "Project Notes")
    }

    func test_a_collision_is_refused_rather_than_overwritten() throws {
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("vantage-collision-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appendingPathComponent("existing.txt")
        try "keep me".write(to: file, atomically: true, encoding: .utf8)
        XCTAssertThrowsError(try MacPathGuard.requireAbsent(file)) { error in
            guard case MacPathGuard.Refusal.collision? = error as? MacPathGuard.Refusal else {
                return XCTFail("expected collision, got \(error)")
            }
        }
        XCTAssertNoThrow(try MacPathGuard.requireAbsent(dir.appendingPathComponent("new.txt")))
    }

    // MARK: - Results

    func test_only_verified_counts_as_success() {
        XCTAssertTrue(MacActionResult.verified(.setVolume, "ok").succeeded)
        XCTAssertFalse(MacActionResult.unverified(.setVolume, "asked").succeeded)
        XCTAssertFalse(MacActionResult.failed(.setVolume, "no").succeeded)
        XCTAssertFalse(MacActionResult.cancelled(.setVolume).succeeded)
    }

    func test_a_sequence_never_claims_more_than_it_did() {
        let one = MacActionResult.verified(.openApplication, "opened Finder")
        let two = MacActionResult.failed(.setVolume, "no output device")
        let combined = MacControlCoordinator.combine([one, two], total: 3)
        XCTAssertEqual(combined.status, .failed)
        XCTAssertTrue(combined.message.contains("did not run"),
                      "a stopped plan must say what did not run: \(combined.message)")
    }

    func test_a_fully_verified_sequence_is_verified() {
        let combined = MacControlCoordinator.combine(
            [.verified(.openApplication, "a"), .verified(.setVolume, "b")], total: 2)
        XCTAssertEqual(combined.status, .verified)
    }
}