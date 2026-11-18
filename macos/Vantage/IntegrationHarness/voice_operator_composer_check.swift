#!/usr/bin/env swift
import Foundation

// Executable assertions for VoiceOperatorComposer.
//
// `swift test` cannot run on this machine: the Swift test target imports XCTest
// and only the Command Line Tools are installed, with no XCTest framework and no
// Xcode. That is pre-existing -- the pristine checkout fails identically. So the
// rules that decide what the composer sends are checked here instead, in the same
// `check()` style the other IntegrationHarness scripts use.

var failures = 0
func check(_ name: String, _ ok: Bool, _ detail: @autoclosure () -> String = "") {
    print("\(ok ? "PASS" : "FAIL")  \(name)\(ok || detail().isEmpty ? "" : "  [\(detail())]")")
    if !ok { failures += 1 }
}

// MARK: - What gets sent

check("a spoken transcript is sent",
      VoiceOperatorComposer.text(draftTranscript: "open Calculator", typedCommand: "") == "open Calculator")

check("a typed command is sent when nothing was spoken",
      VoiceOperatorComposer.text(draftTranscript: "", typedCommand: "open Calculator") == "open Calculator")

check("the transcript wins when both are present",
      VoiceOperatorComposer.text(draftTranscript: "close it", typedCommand: "open Calculator") == "close it")

check("whitespace-only speech does not shadow a typed command",
      VoiceOperatorComposer.text(draftTranscript: "   \n ", typedCommand: "open Calculator") == "open Calculator",
      "got \(VoiceOperatorComposer.text(draftTranscript: "   \n ", typedCommand: "open Calculator"))")

check("whitespace is trimmed from what is sent",
      VoiceOperatorComposer.text(draftTranscript: "  open Calculator  ", typedCommand: "") == "open Calculator")

check("nothing at all yields an empty string",
      VoiceOperatorComposer.text(draftTranscript: "", typedCommand: "").isEmpty)

check("whitespace on both sides yields an empty string",
      VoiceOperatorComposer.text(draftTranscript: "  ", typedCommand: "\t").isEmpty)

// MARK: - Whether Send is enabled

func can(_ t: String, _ c: String, sending: Bool = false, thinking: Bool = false) -> Bool {
    VoiceOperatorComposer.canSend(draftTranscript: t, typedCommand: c,
                                  isSending: sending, isThinking: thinking)
}

check("Send is enabled with text and no work in flight", can("hi", ""))
check("Send is disabled with no text", !can("", ""))
check("Send is disabled with only whitespace", !can("   ", "  "))
check("Send is disabled while a send is in flight", !can("hi", "", sending: true))
check("Send is disabled while the service is thinking", !can("hi", "", thinking: true))
check("Send is disabled when both flags are set", !can("hi", "", sending: true, thinking: true))
check("a typed command enables Send", can("", "open Calculator"))

// MARK: - Double submit

// The rule the bug would be: a second Return resending the first turn while it
// is still in flight. The view clears both fields before awaiting, and refuses
// while `isSending` is set. Reproduce that sequence here.
var draft = "open Calculator"
var typed = ""
check("before send, there is something to send", can(draft, typed))

let cleared = VoiceOperatorComposer.afterSend()
draft = cleared.draftTranscript
typed = cleared.typedCommand
check("after send, both fields are empty", draft.isEmpty && typed.isEmpty)
check("after send, a second press cannot send (empty)",
      !can(draft, typed, sending: true))

// Even if the fields somehow still held text, `isSending` alone must refuse.
check("isSending alone refuses a resend",
      !can("open Calculator", "", sending: true))

// MARK: - Result

print(failures == 0 ? "all composer checks passed" : "\(failures) composer check(s) failed")
exit(failures == 0 ? 0 : 1)