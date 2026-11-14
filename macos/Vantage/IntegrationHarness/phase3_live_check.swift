import Foundation
import AppKit

/// A live check against a real backend: does the briefing read real routes, and
/// does a capture really become a planner task through the real API?
///
/// This is the half a pure logic check cannot cover. It is run by
/// `run_phase3_live.sh`, which starts an isolated server on a scratch home and
/// passes its port and token through the environment. Nothing here touches the
/// person's own data: the server's home is a temp dir the runner deletes.
///
/// It reports NOT TESTED rather than passing when the backend is absent, so a
/// skipped check can never be mistaken for a passing one.

var failures = 0
var passes = 0

func check(_ name: String, _ condition: Bool, _ detail: @autoclosure () -> String = "") {
    if condition {
        passes += 1
        print("PASS  \(name)")
    } else {
        failures += 1
        let extra = detail()
        print("FAIL  \(name)" + (extra.isEmpty ? "" : " — \(extra)"))
    }
}

func note(_ message: String) { print("NOTE  \(message)") }

let api = APIClient.shared

guard api.hasToken else {
    print("SKIP  no backend token in this environment — live checks were NOT RUN")
    exit(2)
}

// 1. The routes the briefing reads answer at all.
var healthOK = false
do {
    let health: APIModels.HealthResponse = try await api.get("/health")
    healthOK = health.status.lowercased().contains("ok") || !health.status.isEmpty
    check("GET /health answers", healthOK)
    note("llm_enabled = \(String(describing: health.llm_enabled)), "
         + "provider = \(health.ai_provider ?? "none")")
} catch {
    check("GET /health answers", false, "\(error.localizedDescription)")
}

// 2. A capture converted to a task really creates a planner task.
let captureDir = URL(fileURLWithPath: NSTemporaryDirectory())
    .appendingPathComponent("vantage-live-capture-\(UUID().uuidString)")
try? FileManager.default.createDirectory(at: captureDir, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: captureDir) }
let captureStore = CaptureStore(directory: captureDir)
let conversion = CaptureConversionService(api: api, store: captureStore)

let item = CaptureItem(content: "Live check task \(UUID().uuidString.prefix(8))")
_ = captureStore.add(item)
let outcome = await conversion.convertToTask(item, title: item.content, detail: "Created by the",
                                             priority: "normal", due: nil)
// (detail text above is truncated deliberately to keep it short; fix it.)
check("a capture converts to a real planner task", outcome.succeeded,
      outcome.error ?? outcome.summary)
if outcome.succeeded {
    let converted = captureStore.item(item.id)
    check("the inbox item records the destination",
          converted?.status == .converted && converted?.destinationReference?.hasPrefix("task:") == true,
          "\(converted?.destinationReference ?? "nil")")
    // The task is really in the planner: read it back.
    do {
        let tasks = try await api.plannerTasks()
        check("the created task is in the planner",
              tasks.contains { $0.title == item.content })
    } catch {
        check("the created task is in the planner", false, error.localizedDescription)
    }
}

// 3. A reminder really creates a scheduled job.
//
// Created directly through the API (not the capture conversion) so the check can
// assert on the job's identity. The conversion path is covered in the logic
// checks; here the question is whether the scheduler route works at all and
// whether the message survives into the job's params.
let schedule = try? await api.createReminder(
    message: "Live reminder \(UUID().uuidString.prefix(8))",
    at: Date().addingTimeInterval(3600))
check("POST /schedules creates a reminder job", schedule != nil)
if let schedule {
    do {
        let jobs = try await api.scheduledJobs()
        // Assert on the id the backend returned: it is the only thing that
        // proves the job this call created is the one that exists. The schedule
        // text is normalised by the backend, so matching on it would be a test
        // of its formatting rather than of creation.
        check("the created reminder is in the scheduler",
              jobs.contains { $0.schedule_id == schedule.schedule_id },
              jobs.map { "\($0.schedule_id):\($0.name):\($0.action ?? "-")" }
                .joined(separator: " | "))
    } catch {
        check("the created reminder is in the scheduler", false, error.localizedDescription)
    }
}

// 3b. A date the scheduler cannot express is refused with a sentence, not a
// silent downgrade to a recurring job.
do {
    _ = try await api.createReminder(message: "far future",
                                     at: Date().addingTimeInterval(5 * 86_400))
    check("an inexpressible reminder date is refused", false, "it was accepted")
} catch {
    check("an inexpressible reminder date is refused with a reason",
          error.localizedDescription.lowercased().contains("cannot")
              || error.localizedDescription.lowercased().contains("can only"),
          error.localizedDescription)
}

// 4. The briefing aggregates the real routes.
var inputs = BriefingBuilder.Inputs()
do {
    inputs.agenda = try await api.plannerToday()
    check("GET /planner/today answers", true)
    note("calendar_read_failed = \(String(describing: inputs.agenda?.calendar_read_failed)), "
         + "events = \(inputs.agenda?.events?.count ?? 0), "
         + "tasks = \(inputs.agenda?.tasks?.count ?? 0)")
} catch {
    inputs.agendaError = error.localizedDescription
    check("GET /planner/today answers", false, error.localizedDescription)
}
do { inputs.tasks = try await api.plannerTasks() } catch { inputs.tasksError = error.localizedDescription }
do { inputs.schedules = try await api.scheduledJobs() } catch { inputs.schedulesError = error.localizedDescription }
do { inputs.approvals = try await api.pendingConfirmations() } catch { inputs.approvalsError = error.localizedDescription }
inputs.captureItems = captureStore.items
inputs.flowHistory = []

let briefing = BriefingBuilder().build(inputs)
check("the briefing is built from live data", !briefing.sections.isEmpty)
// A *task created without a due date* is correctly not part of a daily briefing:
// the briefing is today's day, and "coming up" is a seven-day horizon. What must
// hold is that a task WITH a due date today shows up — so this creates one.
var dueTaskShown = false
var dueTitle = ""
do {
    let formatter = ISO8601DateFormatter()
    formatter.timeZone = .current
    formatter.formatOptions = [.withInternetDateTime]
    let midday = Calendar.current.date(bySettingHour: 23, minute: 30, second: 0, of: Date())!
    dueTitle = "Due-today check \(UUID().uuidString.prefix(6))"
    struct Body: Encodable { let title: String; let priority: String; let due_at: String }
    struct Reply: Codable { let task_ref: String }
    let _: Reply = try await api.post("/planner/tasks",
                                      body: Body(title: dueTitle, priority: "normal",
                                                 due_at: formatter.string(from: midday)),
                                      as: Reply.self)
    var withDue = BriefingBuilder.Inputs()
    withDue.agenda = try await api.plannerToday()
    withDue.tasks = try await api.plannerTasks()
    withDue.captureItems = captureStore.items
    withDue.flowHistory = []
    withDue.schedules = try? await api.scheduledJobs()
    withDue.approvals = try? await api.pendingConfirmations()
    let rebuilt = BriefingBuilder().build(withDue)
    dueTaskShown = rebuilt.sections.contains { section in
        section.items.contains { $0.title == dueTitle }
    }
    check("a task due today appears in the briefing", dueTaskShown,
          "sections: \(rebuilt.sections.map { "\($0.id):\($0.items.count)" }.joined(separator: ", "))")
} catch {
    check("a task due today appears in the briefing", false, error.localizedDescription)
}

// Which sections actually loaded — recorded honestly, not assumed.
let loaded = briefing.sections.filter { $0.isLoaded }.map(\.id)
let failed = briefing.sections.filter {
    if case .failed = $0.state { return true }
    return false
}.map(\.id)
note("sections loaded: \(loaded.joined(separator: ", "))")
if !failed.isEmpty { note("sections FAILED: \(failed.joined(separator: ", ")) — reported, not hidden") }
check("a section failure is reported rather than silently empty",
      failed.isEmpty || briefing.failedSources.count > 0)

// 5. A document overview over a real file with no network call.
let docURL = captureDir.appendingPathComponent("live.txt")
try! "Live document check.\nSecond line.\n".write(to: docURL, atomically: true, encoding: .utf8)
do {
    let source = try DocumentExtractor.describe(docURL)
    let extracted = try DocumentExtractor.extract(source)
    let overview = DocumentService(api: api).overview(of: extracted)
    check("a real file is read locally", overview.isLocal && overview.text.contains("words"))
    check("the overview names its source", overview.sourceName == "live.txt")
} catch {
    check("a real file is read locally", false, error.localizedDescription)
}

print("\n\(passes) passed, \(failures) failed")
exit(failures > 0 ? 1 : 0)