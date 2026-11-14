import Foundation

// Command Center end-to-end harness: drives the REAL ConversationService and
// APIClient (exactly what CommandCenterView uses) against a local stub backend
// on a chosen port (via the internal JARVIS_BACKEND_PORT seam; production
// default 8765 untouched). Proves: a model answer is shown, the session id is
// adopted for follow-ups, and provider failure / auth failure / malformed
// response each become an honest visible error — never a silent fake success.

let BASE = "/tmp/cc_harness"
let MODEF = BASE + "/mode"
let PORTF = BASE + "/port"

func readFile(_ p: String) -> String {
    (try? String(contentsOfFile: p, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}
func setMode(_ m: String) { try? m.write(toFile: MODEF, atomically: true, encoding: .utf8) }

var port = 0
for _ in 0..<40 {
    if let v = Int(readFile(PORTF)), v > 0 { port = v; break }
    Thread.sleep(forTimeInterval: 0.1)
}
if port == 0 { print("FAIL: stub backend did not start"); exit(1) }

let scratch = NSTemporaryDirectory() + "/cc_home_" + String(Int.random(in: 0..<999999))
try? FileManager.default.createDirectory(atPath: scratch, withIntermediateDirectories: true)
try? "harness-token".write(toFile: scratch + "/api_token", atomically: true, encoding: .utf8)
setenv("JARVIS_BACKEND_PORT", String(port), 1)
setenv("JARVIS_HOME", scratch, 1)
print("harness: stub backend on \(port)")

var lines: [String] = []
var allOK = true
func check(_ label: String, _ cond: Bool, _ detail: String) {
    lines.append("\(cond ? "PASS" : "FAIL")  \(label)   [\(!cond ? detail : "")]")
    if !cond { allOK = false }
}

let svc = ConversationService(timeout: 20)

// 1. A model answer is shown, attributed to the model.
setMode("success")
let r1 = await svc.send("What is the sun?")
check("success: a reply was returned", r1 != nil, "nil")
check("success: reply text shown", (r1?.text ?? "").contains("star"), "\(String(describing: r1?.text))")
check("success: role is assistant", r1?.role == .assistant, "\(String(describing: r1?.role))")
check("success: attributed to a model (not 'rules')", r1?.meta.isModelAnswer == true, r1?.meta.model ?? "")
check("success: no error recorded", svc.lastError == nil, svc.lastError ?? "")

// 2. Session adopted -> the follow-up carries the same conversation.
check("session adopted from response", svc.conversation.sessionID == "SESS-harness",
      "\(String(describing: svc.conversation.sessionID))")

// 3. Provider failure (503) -> honest error, no fake success, no assistant turn.
setMode("provider_fail")
let before = svc.conversation.messages.count
let r2 = await svc.send("Ask the model something")
check("provider-fail: no reply returned", r2 == nil, "returned a reply")
check("provider-fail: user turn recorded", svc.conversation.messages.count == before + 2,
      "count \(before) -> \(svc.conversation.messages.count)")
check("provider-fail: last turn is an error", svc.conversation.messages.last?.role == .error,
      "\(String(describing: svc.conversation.messages.last?.role))")
check("provider-fail: error names the provider/config", {
    let e = (svc.lastError ?? "").lowercased()
    return e.contains("provider") || e.contains("configured") || e.contains("available")
}(), svc.lastError ?? "")
check("provider-fail: NOT reported as a fake answer", !(svc.conversation.messages.last?.text ?? "").contains("star"), "")
let providerErr = (svc.lastError ?? "")

// 4. Auth failure (401) -> names the credential problem, distinct message.
setMode("auth_fail")
_ = await svc.send("Another question")
let authErr = (svc.lastError ?? "").lowercased()
check("auth-fail: error mentions the credential/token",
      authErr.contains("credential") || authErr.contains("token") || authErr.contains("authoriz"),
      svc.lastError ?? "")
check("auth-fail: distinct from the provider-fail message", authErr != providerErr.lowercased(), "same message as provider-fail: \(svc.lastError ?? "")")

// 5. Malformed response -> decode error surfaced honestly, not a crash.
setMode("malformed")
_ = await svc.send("Ask after malformed")
check("malformed: an error is surfaced", svc.lastError != nil && !(svc.lastError ?? "").isEmpty,
      "no error")
check("malformed: last turn is an error (not a fake answer)", svc.conversation.messages.last?.role == .error,
      "\(String(describing: svc.conversation.messages.last?.role))")

// 6. Retry re-sends the question WITHOUT recording it a second time.
//
// `retry()` used to call `send(lastPrompt)`, and `send` appends the user turn —
// so a retry left two copies of the same question in the transcript. A retry
// is only safe to offer when it sends the same prompt; it must not double the
// question either.
setMode("provider_fail")
_ = await svc.send("A question that fails once")
let question = "A question that fails once"
let copiesBefore = svc.conversation.messages.filter {
    $0.role == .user && $0.text == question
}.count
check("retry setup: the question is in the transcript once", copiesBefore == 1,
      "\(copiesBefore) copies")
setMode("success")
await svc.retry()
let copiesAfter = svc.conversation.messages.filter {
    $0.role == .user && $0.text == question
}.count
check("retry: the question is still in the transcript exactly once", copiesAfter == 1,
      "\(copiesAfter) copies after retry")
check("retry: the answer follows the question", svc.conversation.messages.last?.role == .assistant,
      "\(String(describing: svc.conversation.messages.last?.role))")
check("retry: no failure is reported", svc.lastError == nil, svc.lastError ?? "")

// 6b. A tool/attachment result is its own kind of turn, not an answer.
//
// The transcript draws a result as a compact inset row. That only works if the
// service marks it as one; classifying it as `.assistant` drew "file read" in
// the same shape as a model answer.
svc.appendResult("invoice.pdf: 2 pages, total $41.00", isError: false)
check("tool result: recorded with the tool role", svc.conversation.messages.last?.role == .tool,
      "\(String(describing: svc.conversation.messages.last?.role))")
svc.appendResult("Could not read nope.bin", isError: true)
check("tool failure: recorded as an error turn", svc.conversation.messages.last?.role == .error,
      "\(String(describing: svc.conversation.messages.last?.role))")

// 7. Recovery: a good answer after failures is shown, and the failed turns remain visible.
setMode("success")
let r3 = await svc.send("Is it working again?")
check("recovery: a reply is shown again", r3 != nil && r3?.role == .assistant, "\(String(describing: r3?.role))")
check("recovery: earlier failures still in the transcript",
      svc.conversation.messages.contains { $0.role == .error }, "failures were hidden")

// 8. A slow model turn (9s) still succeeds.
//
// APIClient's default 8s request timeout was applied to /command too, so any
// model turn slower than 8 seconds was killed in transport -- before the
// service's own 60s budget was consulted -- and reported as an error. A real
// provider on a cold route routinely takes longer than that. The turn now
// carries its own timeout.
setMode("slow9")
let t0 = Date()
let r4 = await svc.send("A question the model takes nine seconds to answer")
let elapsed = Date().timeIntervalSince(t0)
check("slow turn: a reply is shown (not a transport error)", r4?.role == .assistant,
      svc.lastError ?? "no error, but nil reply")
check("slow turn: it really was slow", elapsed >= 8.5, "\(elapsed)s")

print("\n===== COMMAND CENTER END-TO-END RESULTS =====")
for l in lines { print(l) }
print(allOK ? "===== ALL CHECKS PASSED =====" : "===== SOME CHECKS FAILED =====")
try? FileManager.default.removeItem(atPath: scratch)
exit(allOK ? 0 : 1)