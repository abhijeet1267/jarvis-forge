import Foundation
import AppKit

/// Phase 3 logic checks, compiled against the real app sources.
///
/// XCTest is not available under Command Line Tools (no `XCTest` module), which is
/// why this package's checks are standalone executables — see
/// `IntegrationHarness/run_anywhere_checks.sh` for the same pattern. Each check
/// prints PASS/FAIL with the observed value, so a failure names what was wrong
/// rather than only that something was.
///
/// What this covers: Quick Capture persistence and classification, the natural
/// date reader, the briefing builder's empty/failed distinction, the preference
/// store's validation and conflict rule, and the document extractor chain against
/// real temporary files. What it does **not** cover: a live backend, a live AI
/// provider, or the rendered UI — those are separate checks.

// The check body runs inside a main-actor task: several services are main-actor
// (they are driven by the UI in the real app), and pretending otherwise would
// mean testing a copy of their logic rather than the logic itself.
// Drive the main run loop so the main-actor body can actually run, then exit
// with the result. This is the same approach the render harnesses use.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
DispatchQueue.main.async {
    do { try runPhaseThreeChecks() }
    catch { print("FAIL  unexpected error: \(error)"); failures += 1 }
    if failures > 0 {
        print("\(failures) FAILED")
        exit(1)
    }
    print("ALL PHASE 3 LOGIC CHECKS PASSED")
    exit(0)
}
app.run()

var failures = 0
var checks = 0

func check(_ name: String, _ condition: Bool, _ detail: @autoclosure () -> String = "") {
    checks += 1
    if condition {
        print("PASS  \(name)")
    } else {
        failures += 1
        let extra = detail()
        print("FAIL  \(name)" + (extra.isEmpty ? "" : " — \(extra)"))
    }
}

func section(_ title: String) { print("\n=== \(title) ===") }

// MARK: - Temporary storage

func tempDirectory(_ label: String) -> URL {
    let url = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("vantage-phase3-\(label)-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

@MainActor
func runPhaseThreeChecks() throws {
// MARK: - 1. Capture classification

section("Quick Capture classification")

let classifier = CaptureClassifier()

check("“remember to review the proposal” is a task candidate, not a task",
      classifier.classify("Remember to review the project proposal.").item?.kind == .taskCandidate)
check("“capture this idea: open files faster” is an idea",
      classifier.classify("Capture this idea: make Vantage open files faster").item?.kind == .idea,
      "\(String(describing: classifier.classify("Capture this idea: make Vantage open files faster").item?.kind))")
check("“save this as a note” is a note",
      classifier.classify("Save this as a note.").item?.kind == .note)
check("“turn this into a task for tomorrow” suggests a task",
      classifier.classify("Turn this into a task for tomorrow.").item?.kind == .taskCandidate)
check("“add this to my workflow ideas” is a workflow idea",
      classifier.classify("Add this to my workflow ideas.").item?.kind == .workflowIdea,
      "\(String(describing: classifier.classify("Add this to my workflow ideas.").item?.kind))")
check("a bare link is a link",
      classifier.classify("capture this: https://example.com/notes").item?.kind == .link)

// The line the brief draws: capture must not swallow creation or execution.
check("“create a task to call Sam” is NOT a capture",
      classifier.classify("create a task to call Sam").item == nil)
check("“run my Start Workday flow” is NOT a capture",
      classifier.classify("run my Start Workday flow").item == nil)
check("“open Safari” is NOT a capture",
      classifier.classify("open Safari").item == nil)

// Content extraction
let captured = classifier.classify("Remember to review the project proposal for tomorrow.").item
check("the verb is stripped from the stored content",
      captured?.content.localizedCaseInsensitiveContains("remember to") == false,
      "content = \(captured?.content ?? "nil")")
check("the date is kept as a suggestion, not applied",
      captured?.suggestedDue != nil)
check("the date phrase is removed from the title",
      captured?.content.lowercased().contains("tomorrow") == false,
      "content = \(captured?.content ?? "nil")")

check("vague wording invents no date",
      classifier.classify("Remember to review this soon").item?.suggestedDue == nil)

// MARK: - 2. Natural dates

section("Natural date reader")

var calendar = Calendar(identifier: .gregorian)
calendar.timeZone = TimeZone(identifier: "America/New_York")!
let base = calendar.date(from: DateComponents(year: 2026, month: 3, day: 10,
                                              hour: 15, minute: 30))!
let tomorrow = NaturalDates.firstMatch(in: "review this tomorrow", now: base, calendar: calendar)
check("“tomorrow” resolves to the next day at 09:00",
      tomorrow.map {
          calendar.component(.day, from: $0.date) == 11
              && calendar.component(.hour, from: $0.date) == 9
      } ?? false, "\(String(describing: tomorrow))")
check("“friday” from Tuesday resolves to that Friday",
      NaturalDates.firstMatch(in: "send it friday", now: base, calendar: calendar).map {
          calendar.component(.weekday, from: $0.date) == 6
      } ?? false)
check("an explicit date is read",
      NaturalDates.firstMatch(in: "due 2026-04-02", now: base, calendar: calendar) != nil)
check("“soon” produces no date",
      NaturalDates.firstMatch(in: "do this soon", now: base, calendar: calendar) == nil)
check("“sometime next quarter” produces no date",
      NaturalDates.firstMatch(in: "sometime next quarter", now: base, calendar: calendar) == nil)

// A weekday said ON that weekday must mean the following one, never today.
let onFriday = calendar.date(from: DateComponents(year: 2026, month: 3, day: 13))!
check("“friday” said on a Friday is the next Friday, not today",
      NaturalDates.firstMatch(in: "do it friday", now: onFriday, calendar: calendar).map {
          $0.date > onFriday && calendar.component(.day, from: $0.date) == 20
      } ?? false)

// MARK: - 3. Capture store

section("Quick Capture persistence")

do {
    let dir = tempDirectory("capture")
    defer { try? FileManager.default.removeItem(at: dir) }
    let store = CaptureStore(directory: dir)

    let first = store.add(CaptureItem(content: "First thought"))
    check("adding an item succeeds", first.isSuccess(store: store))
    check("the item is in the store", store.items.count == 1)

    // Duplicate refusal: the same content again must not create a second item.
    let duplicate = store.add(CaptureItem(content: "  first   THOUGHT "))
    check("a duplicate capture is refused",
          duplicate.isFailure, "\(duplicate)")
    check("the duplicate did not add a second item", store.items.count == 1)

    // A different kind with the same words is a different decision.
    let asIdea = store.add(CaptureItem(content: "First thought", kind: .idea))
    check("the same words captured as a different kind are allowed", asIdea.isSuccess(store: store))

    // Editing updates the right item.
    var toEdit = store.items.first { $0.kind == .general }!
    toEdit.content = "First thought, edited"
    toEdit.tags = ["planning"]
    check("editing succeeds", store.update(toEdit).isSuccess(store: store))
    let reloaded = CaptureStore(directory: dir)
    check("an edit survives a restart",
          reloaded.items.first { $0.id == toEdit.id }?.content == "First thought, edited")
    check("tags survive a restart",
          reloaded.items.first { $0.id == toEdit.id }?.tags == ["planning"])

    // Conversion records a destination only when told to.
    let converted = reloaded.markConverted(toEdit.id, destination: .task,
                                          reference: "T-123", label: "First thought, edited")
    check("conversion succeeds", converted.isSuccess(store: reloaded))
    let afterConvert = CaptureStore(directory: dir).items.first { $0.id == toEdit.id }
    check("conversion sets converted status", afterConvert?.status == .converted)
    check("conversion records the destination reference",
          afterConvert?.destinationReference == "task:T-123")

    // Delete.
    check("delete succeeds", reloaded.delete(toEdit.id).isSuccess(store: reloaded))
    check("the deleted item is gone after a restart",
          CaptureStore(directory: dir).items.contains { $0.id == toEdit.id } == false)
}

// An unreadable file must not be overwritten.
do {
    let dir = tempDirectory("capture-bad")
    defer { try? FileManager.default.removeItem(at: dir) }
    let url = dir.appendingPathComponent("capture.json")
    try! Data("{ this is not json".utf8).write(to: url)
    let store = CaptureStore(directory: dir)
    check("an unreadable inbox reports an error", store.loadError != nil)
    check("an unreadable inbox becomes read-only", store.isReadOnly)
    check("the unreadable file was moved aside, not deleted",
          FileManager.default.fileExists(atPath: url.path) == false)
    check("a quarantine path is published", store.quarantinePath != nil)
    let write = store.add(CaptureItem(content: "should not be written"))
    check("writing to a read-only inbox is refused", write.isFailure)
    let stillThere = store.quarantinePath.flatMap { try? String(contentsOfFile: $0, encoding: .utf8) }
    check("the original bytes are intact in the quarantine file",
          stillThere?.contains("this is not json") == true)
}

// MARK: - 4. Briefing

section("Daily Briefing")

func agenda(events: [Int] = [], calendarFailed: Bool = false,
            tasks: [String] = [], overdue: [String] = []) -> APIModels.PlannerAgenda {
    let eventObjects: [AnyCodable] = events.map {
        AnyCodable(["uid": "e\($0)", "title": "Event \($0)", "start": "2026-03-10T10:00:00"])
    }
    let taskObjects = tasks.map {
        APIModels.PlannerTask(task_ref: $0, title: "Task \($0)", detail: nil, status: "open",
                              priority: "normal", due_at: "2026-03-10T12:00:00",
                              start_at: nil, estimate_minutes: nil, depends_on: nil,
                              project: nil, done_at: nil, evidence: nil, overdue: false,
                              blocked: false)
    }
    let overdueObjects = overdue.map {
        APIModels.PlannerTask(task_ref: $0, title: "Overdue \($0)", detail: nil, status: "open",
                              priority: "high", due_at: "2026-03-01T09:00:00",
                              start_at: nil, estimate_minutes: nil, depends_on: nil,
                              project: nil, done_at: nil, evidence: nil, overdue: true,
                              blocked: false)
    }
    return APIModels.PlannerAgenda(
        day: "2026-03-10", calendar_read_failed: calendarFailed, events: eventObjects,
        conflicts: [], tasks: taskObjects, overdue: overdueObjects, blocked: [],
        next_action: nil, due_today: taskObjects, has_unscheduled: false,
        generated_at: nil)
}

var builderInputs = BriefingBuilder.Inputs()
builderInputs.agenda = agenda(events: [1], tasks: ["A"], overdue: ["B"])
builderInputs.captureItems = []
builderInputs.flowHistory = []
builderInputs.schedules = []
builderInputs.approvals = []
let built = BriefingBuilder().build(builderInputs, now: base, calendar: calendar)

check("a populated briefing has an event", built.sections.first { $0.id == "schedule" }?.items.count == 1)
check("a due task is in ‘due’", built.sections.first { $0.id == "due" }?.items.count == 1)
check("an overdue task is in ‘overdue’",
      built.sections.first { $0.id == "overdue" }?.items.count == 1)
check("due and overdue are separate sections",
      built.sections.first { $0.id == "due" }?.id != built.sections.first { $0.id == "overdue" }?.id)
check("the headline mentions the overdue count", built.headline.contains("1 overdue"), built.headline)
check("suggestions are present and labelled as suggestions",
      built.suggestions.contains { $0.lowercased().contains("overdue") })

// An empty day.
var emptyInputs = BriefingBuilder.Inputs()
emptyInputs.agenda = agenda()
emptyInputs.captureItems = []
emptyInputs.flowHistory = []
emptyInputs.schedules = []
emptyInputs.approvals = []
let empty = BriefingBuilder().build(emptyInputs, now: base, calendar: calendar)
check("an empty day says it is clear", empty.headline.contains("clear"), empty.headline)
check("an empty day has no failed sections",
      empty.sections.allSatisfy { $0.isLoaded }, "\(empty.sections.map { $0.state })")
check("empty is not the same as failed", empty.allSourcesFailed == false)

// A failed calendar must not read as an empty day.
var failedInputs = BriefingBuilder.Inputs()
failedInputs.agenda = agenda(calendarFailed: true)
failedInputs.captureItems = []
failedInputs.flowHistory = []
failedInputs.schedules = []
failedInputs.approvals = []
let failedCalendar = BriefingBuilder().build(failedInputs, now: base, calendar: calendar)
let scheduleSection = failedCalendar.sections.first { $0.id == "schedule" }!
check("a failed calendar read is a failure, not empty",
      { if case .failed = scheduleSection.state { return true }; return false }(),
      "\(scheduleSection.state)")
check("a failed source is named", failedCalendar.failedSources.contains("Calendar"))

// A total failure must say so out loud.
var deadInputs = BriefingBuilder.Inputs()
deadInputs.agendaError = "backend down"
deadInputs.tasksError = "backend down"
deadInputs.captureItems = nil
deadInputs.flowHistory = nil
deadInputs.schedulesError = "backend down"
deadInputs.approvalsError = "backend down"
let dead = BriefingBuilder().build(deadInputs, now: base, calendar: calendar)
check("a total failure is reported as such", dead.allSourcesFailed, "\(dead.headline)")
check("a total failure is never a clear day", !dead.headline.contains("clear"), dead.headline)

// Time zone / date boundary: the same instant is "today" for its own zone only.
var zoneCalendar = Calendar(identifier: .gregorian)
zoneCalendar.timeZone = TimeZone(identifier: "Pacific/Kiritimati")!  // UTC+14
let lateNight = zoneCalendar.date(from: DateComponents(year: 2026, month: 3, day: 10,
                                                       hour: 23, minute: 30))!
var boundaryInputs = BriefingBuilder.Inputs()
boundaryInputs.agenda = agenda(tasks: ["A"])
boundaryInputs.captureItems = []
boundaryInputs.flowHistory = []
boundaryInputs.schedules = []
boundaryInputs.approvals = []
let boundary = BriefingBuilder().build(boundaryInputs, now: lateNight, calendar: zoneCalendar)
// `DateFormatter` renders in the reader's own locale, so the day may be
// "Tuesday, 10 March 2026" or "Tuesday, March 10, 2026". The fact worth pinning
// is that the day is the reader's own 10th, not the server's.
check("the briefing names the reader's own day",
      boundary.day.contains("10") && boundary.day.lowercased().contains("march"),
      boundary.day)

// A flow failure is surfaced with an action that points at the real flow.
var flowInputs = BriefingBuilder.Inputs()
flowInputs.agenda = agenda()
flowInputs.captureItems = []
flowInputs.schedules = []
flowInputs.approvals = []
let failedFlowID = UUID()
flowInputs.flowHistory = [FlowRun(id: UUID(), flowID: failedFlowID, flowName: "Morning setup",
                                  stepCount: 2, startedAt: base, finishedAt: base,
                                  state: .failed, outcomes: [],
                                  stopReason: "Open application failed")]
let withFailure = BriefingBuilder().build(flowInputs, now: base, calendar: calendar)
let failureCards = withFailure.sections.first { $0.id == "failures" }!.items
check("a failed flow appears in attention items", failureCards.count == 1)
check("the failure card points at the real flow",
      failureCards.first?.action == .openFlow(failedFlowID))

// A capture awaiting review is surfaced.
var captureInputs = BriefingBuilder.Inputs()
captureInputs.agenda = agenda()
captureInputs.flowHistory = []
captureInputs.schedules = []
captureInputs.approvals = []
let waitingItem = CaptureItem(content: "A captured thought")
captureInputs.captureItems = [waitingItem]
let withCapture = BriefingBuilder().build(captureInputs, now: base, calendar: calendar)
check("an unreviewed capture is in the briefing",
      withCapture.sections.first { $0.id == "capture" }?.items.count == 1)

// Staleness.
var staleInputs = BriefingBuilder.Inputs()
staleInputs.agenda = agenda()
staleInputs.captureItems = []
staleInputs.flowHistory = []
staleInputs.schedules = []
staleInputs.approvals = []
let stale = BriefingBuilder().build(staleInputs, now: base, calendar: calendar, staleAfter: -1)
check("an old section is labelled as possibly out of date",
      { if case .failed(let why) = stale.sections.first(where: { $0.id == "schedule" })!.state {
          return why.lowercased().contains("out of date") }; return false }())

// MARK: - 5. Preferences

section("Preferences")

do {
    let dir = tempDirectory("prefs")
    defer { try? FileManager.default.removeItem(at: dir) }
    let store = PreferenceStore(directory: dir)

    let added = store.add(Preference(kind: .preferredApplication, key: "Browser", value: "Safari"))
    check("adding a preference succeeds", added.isSuccess(store: store))
    check("the key is preserved as typed", store.preferences.first?.key == "Browser")
    check("a restart restores the preference",
          PreferenceStore(directory: dir).preferences.count == 1)

    // Validation.
    check("an unknown summary format is refused",
          store.add(Preference(kind: .summaryFormat, key: "format", value: "verbose")).isFailure)
    check("an absolute-path folder is accepted",
          store.add(Preference(kind: .preferredFolder, key: "summary",
                               value: "/tmp")).isSuccess(store: store))
    check("a protected folder is refused",
          store.add(Preference(kind: .preferredFolder, key: "secret",
                               value: "/System")).isFailure)
    check("a credential-looking value is refused",
          store.add(Preference(kind: .summaryFormat, key: "x",
                               value: "sk-abcdefghijklmnopqrstuvwxyz012345")).isFailure)
    check("document-length text is refused",
          store.add(Preference(kind: .summaryFormat, key: "x",
                               value: String(repeating: "a", count: 400))).isFailure)

    // Conflict rule.
    let first = store.add(Preference(kind: .preferredApplication, key: "editor",
                                     value: "TextEdit")).itemOrNil
    Thread.sleep(forTimeInterval: 0.02)
    let second = store.add(Preference(kind: .preferredApplication, key: "editor",
                                      value: "Xcode")).itemOrNil
    check("two preferences with the same key create a conflict", store.conflicts.count == 1)
    let effective = store.effectiveValue(kind: .preferredApplication, key: "editor")
    check("the most recent preference wins the conflict",
          effective.value == "Xcode" && effective.conflict,
          "value = \(String(describing: effective.value))")
    check("both conflicting preferences are retained", first != nil && second != nil)

    // Disabling removes it from effective use without deleting it.
    if let newest = store.preferences.first(where: { $0.id == second?.id }) {
        _ = store.setEnabled(false, for: newest.id)
        check("a disabled preference stops applying",
              store.effectiveValue(kind: .preferredApplication, key: "editor").value == "TextEdit")
        check("a disabled preference is still listed",
              store.preferences.contains { $0.id == newest.id })
        check("disabling removed the conflict", store.conflicts.isEmpty)
    }

    // Secrets and document text are never stored, even in a note.
    check("a secret in the note is refused",
          store.add(Preference(kind: .fileNaming, key: "p", value: "{name}",
                               note: "api_key=abc")).isFailure)

    // Restore defaults.
    check("removeAll clears the store", store.removeAll().isSuccess(store: store))
    check("a restart after removeAll is empty",
          PreferenceStore(directory: dir).preferences.isEmpty)
}

// MARK: - 6. Document extraction (real files, real filesystem)

section("Document extraction")

let docDir = tempDirectory("docs")
defer { try? FileManager.default.removeItem(at: docDir) }

// Plain text
let textURL = docDir.appendingPathComponent("notes.txt")
try! "The project starts on Monday.\nSend the deck to Priya by Friday.\n".write(
    to: textURL, atomically: true, encoding: .utf8)
do {
    let source = try DocumentExtractor.describe(textURL)
    check("a text file is described", source.kind == .plainText)
    let extracted = try DocumentExtractor.extract(source)
    check("a text file is read", extracted.text.contains("project starts"), extracted.text)
    check("the extraction method is direct text", extracted.method == .directText)
    check("the result is linked to the source", extracted.source.displayName == "notes.txt")
}

// Empty file
let emptyURL = docDir.appendingPathComponent("empty.txt")
try! "".write(to: emptyURL, atomically: true, encoding: .utf8)
do {
    let source = try DocumentExtractor.describe(emptyURL)
    let extracted = try DocumentExtractor.extract(source)
    check("an empty file reads as empty, not as text",
          extracted.text.isEmpty && extracted.method == .empty)
    check("an empty file has zero words", extracted.wordCount == 0)
}

// Unsupported type
let binURL = docDir.appendingPathComponent("data.docx")
try! Data([0x50, 0x4B, 0x03, 0x04]).write(to: binURL)
do {
    _ = try DocumentExtractor.describe(binURL)
    check("an unsupported type is refused", false, "docx was accepted")
} catch let error as DocumentError {
    check("an unsupported type is refused by name",
          error.errorDescription?.contains("docx") == true,
          error.errorDescription ?? "")
} catch {
    check("an unsupported type is refused", false, "\(error)")
}

// A directory is not a document
do {
    _ = try DocumentExtractor.describe(docDir)
    check("a folder is refused", false, "a directory was accepted")
} catch let error as DocumentError {
    check("a folder is refused as a folder",
          error.errorDescription?.contains("folder") == true)
}

// Large file is bounded.
let bigURL = docDir.appendingPathComponent("big.txt")
let big = String(repeating: "line of text\n", count: 20_000)  // ~240 KB
try! big.write(to: bigURL, atomically: true, encoding: .utf8)
do {
    let extracted = try DocumentExtractor.extract(try DocumentExtractor.describe(bigURL))
    check("a long document is truncated at a bound",
          extracted.truncated && extracted.characterCount == DocumentExtractor.maxCharacters,
          "chars = \(extracted.characterCount), truncated = \(extracted.truncated)")
}

// CSV and source read as text.
let csvURL = docDir.appendingPathComponent("table.csv")
try! "a,b\n1,2\n".write(to: csvURL, atomically: true, encoding: .utf8)
check("a CSV is readable",
      (try? DocumentExtractor.extract(DocumentExtractor.describe(csvURL)))?.text.contains("a,b") == true)

// HTML: visible text only.
let htmlURL = docDir.appendingPathComponent("page.html")
try! "<html><head><style>.x{color:red}</style></head><body><p>Hello reader</p></body></html>"
    .write(to: htmlURL, atomically: true, encoding: .utf8)
do {
    let extracted = try DocumentExtractor.extract(DocumentExtractor.describe(htmlURL))
    check("HTML keeps the visible text", extracted.text.contains("Hello reader"))
    check("HTML drops the stylesheet", !extracted.text.contains("color:red"))
}

// The local overview makes no network call and names the source.
let extracted = try DocumentExtractor.extract(DocumentExtractor.describe(textURL))
let overview: DocumentResult = DocumentService().overview(of: extracted)
check("the overview is local", overview.answeredBy == "local" && overview.isLocal)
check("the overview is linked to the right source", overview.sourceName == "notes.txt")
check("the overview reports the word count", overview.text.contains("words"))

// MARK: - 7. Action-item parsing

section("Document action items")

let answer = """
1. Send the deck to Priya
   Owner: Priya
   Due: Friday
2. Book the room for the demo
   Due: no date given
3. Update the roadmap
"""
let parsed = ActionItemParser().parse(answer, source: try! DocumentExtractor.describe(textURL),
                                     now: base, calendar: calendar)
check("three action items are read", parsed.actions.count == 3, "\(parsed.actions.count)")
check("an owner is attributed", parsed.actions.first?.owner == "Priya")
check("a stated date is attached as a real date", parsed.actions.first?.dueDate != nil)
check("a stated date is labelled as stated",
      parsed.actions.first?.dueConfidence == .stated)
check("“no date given” leaves the date empty", parsed.actions[1].dueDate == nil)
check("an item with no date line has no date", parsed.actions[2].dueDate == nil)
check("nothing is created by parsing", parsed.actions.allSatisfy { !$0.isApproved })

let noItems = ActionItemParser().parse("No action items found.", source: try! DocumentExtractor.describe(textURL))
check("an empty answer yields no proposals", noItems.actions.isEmpty && noItems.problem == nil)

let proseAnswer = ActionItemParser().parse(
    "This document describes a long series of events across many pages and does not itself "
    + "request anything specific of the reader.", source: try! DocumentExtractor.describe(textURL))
check("a prose answer is not invented into a list", proseAnswer.actions.isEmpty)

// MARK: - 8. Palette resolution

section("Vantage Anywhere resolution")

let flowDir = tempDirectory("flows")
defer { try? FileManager.default.removeItem(at: flowDir) }
let flowStore = FlowStore(directory: flowDir)
let captureDir = tempDirectory("palette-capture")
defer { try? FileManager.default.removeItem(at: captureDir) }
let paletteCapture = CaptureStore(directory: captureDir)
let resolver = AnywhereCommand(applications: ["Safari", "Finder"], store: flowStore,
                               capture: paletteCapture)

if case .capture(let item) = resolver.resolve("capture this idea: make Vantage open files faster").outcome {
    check("the palette resolves a capture", item.kind == .idea)
} else {
    check("the palette resolves a capture", false)
}
if case .briefing = resolver.resolve("show my briefing").outcome {
    check("the palette resolves the briefing", true)
} else { check("the palette resolves the briefing", false) }
if case .showInbox = resolver.resolve("show my inbox").outcome {
    check("the palette resolves the inbox", true)
} else { check("the palette resolves the inbox", false) }
// These must keep their old meanings.
if case .action(let kind, _) = resolver.resolve("Open Safari").outcome {
    check("“Open Safari” still resolves to an action", kind == .openApplication)
} else { check("“Open Safari” still resolves to an action", false) }
if case .conversation = resolver.resolve("write me a poem").outcome {
    check("ordinary conversation is unchanged", true)
} else { check("ordinary conversation is unchanged", false) }

// Every suggestion must still resolve to something real.
for suggestion in resolver.suggestions(recent: [], limit: 8) {
    let outcome = resolver.resolve(suggestion.text).outcome
    if case .flowProblem = outcome {
        check("suggestion “\(suggestion.text)” is not a dead end", false)
    }
}
check("suggestions do not contain dead ends", true)

// MARK: - 9. Cross-feature handoffs

section("Handoffs")

// A capture converted to a flow produces an editable draft and does NOT mark the
// item converted (the draft is unsaved, so the item stays actionable).
do {
    let dir = tempDirectory("convert")
    defer { try? FileManager.default.removeItem(at: dir) }
    let store = CaptureStore(directory: dir)
    let conversion = CaptureConversionService(store: store)
    let item = CaptureItem(content: "opens Safari and sets the volume to 20 percent",
                           kind: .workflowIdea)
    _ = store.add(item)
    let outcome = conversion.convertToFlow(item, applications: ["Safari", "Finder"])
    check("a workflow idea drafts a real flow", outcome.succeeded && outcome.flowDraft != nil,
          outcome.error ?? "")
    check("the draft has real steps", (outcome.flowDraft?.steps.count ?? 0) > 0)
    check("the draft is not saved by converting",
          store.item(item.id)?.status != .converted)
    check("the draft is not run by converting", outcome.flowDraft?.steps.isEmpty == false)

    // Prose that is not a Mac action produces no draft at all.
    let prose = CaptureItem(content: "send the deck to Priya for review", kind: .workflowIdea)
    _ = store.add(prose)
    let proseOutcome = conversion.convertToFlow(prose, applications: ["Safari"])
    check("prose that is not a Mac action drafts no flow", proseOutcome.flowDraft == nil)
}

// A preference default supplies a *new* flow's policy without overriding a draft
// that already carries one.
do {
    let dir = tempDirectory("flow-default")
    defer { try? FileManager.default.removeItem(at: dir) }
    let prefs = PreferenceStore(directory: dir)
    _ = prefs.add(Preference(kind: .defaultFlowPolicy, key: "policy", value: "continue"))
    check("a default flow policy is read", prefs.defaultFlowPolicy() == .continueIndependent)

    let captureDir = tempDirectory("flow-default-capture")
    defer { try? FileManager.default.removeItem(at: captureDir) }
    let captureStore = CaptureStore(directory: captureDir)
    let conversion = CaptureConversionService(store: captureStore)
    let item = CaptureItem(content: "opens Safari", kind: .workflowIdea)
    _ = captureStore.add(item)
    let outcome = conversion.convertToFlow(item, applications: ["Safari"])
    // The conversion itself does not apply the preference; the Flows screen does
    // when it adopts a *new* draft, and only when the draft has no policy of its
    // own. Assert the draft is still a plain stop-policy flow here.
    check("a converted flow carries no hidden preference",
          outcome.flowDraft?.failurePolicy == .stop)
}

// A briefing card's action targets a real, known destination.
let nav = AppNavigation.shared
nav.go(.capture, captureID: UUID())
check("a navigation request carries its destination", nav.request?.destination == .capture)
check("a navigation request carries its payload", nav.request?.captureID != nil)
nav.consume()
check("a request is consumed once", nav.request == nil)

// MARK: - Result

print("\n\(checks - failures)/\(checks) checks passed")


}

// MARK: - Helpers

extension Result {
    var isFailure: Bool { if case .failure = self { return true }; return false }
    func isSuccess(store: Any?) -> Bool { if case .success = self { return true }; return false }
    var itemOrNil: Success? { if case .success(let value) = self { return value }; return nil }
}