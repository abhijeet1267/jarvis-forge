// permission_state.swift
//
// Asks macOS what it actually thinks about each privacy-sensitive capability, and prints
// one JSON object on stdout.
//
// Compiled and run by jarvis/permissions/service.py -- which means the answers describe
// whoever ran it. That is the whole reason this file exists in the bundle's helper set
// rather than being a Python call: TCC attributes a permission to the responsible process,
// so the same question asked from a terminal reports the terminal's grants. Measured
// 2026-10-04: run from the agent's shell this prints speech=notDetermined for a laptop
// where the user granted Speech Recognition to Vantage an hour earlier.
//
// Nothing here *requests* a permission. Requesting prompts the user, and a status check
// that pops a dialog every time the dashboard polls is not a status check. Where the
// status is undetermined the caller says so and offers a separate, deliberate action.

import Foundation
import CoreGraphics
import ApplicationServices
import AVFoundation
import Speech

func microphone(_ v: AVAuthorizationStatus) -> String {
    switch v {
    case .notDetermined: return "not_determined"
    case .restricted:    return "restricted"
    case .denied:        return "denied"
    case .authorized:    return "granted"
    @unknown default:    return "unknown"
    }
}

func speech(_ v: SFSpeechRecognizerAuthorizationStatus) -> String {
    switch v {
    case .notDetermined: return "not_determined"
    case .denied:        return "denied"
    case .restricted:    return "restricted"
    case .authorized:    return "granted"
    @unknown default:    return "unknown"
    }
}

var out: [String: Any] = [:]

// ---------------------------------------------------------------------------
// REQUEST PATH -- runs only behind a deliberate action by someone at the laptop.
// Everything between this heading and the STATUS READ PATH heading below is the only
// place in this file where a system prompt may be raised.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Request mode: `permission_state request <key>`.
//
// Separate from the status read on purpose. The status path must never prompt, because a
// dashboard polls it; this path runs only when a person pressed "Grant access" on the laptop
// in front of the screen. It reports what the *request* said, and the caller then asks the
// status query again -- because for Accessibility especially, the request's own return value
// describes the state before the dialog, not after it.
// ---------------------------------------------------------------------------
func pumpUntil(_ deadline: Date, pending: () -> Bool) {
    while pending() && Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
    }
}

func requestStatus(_ key: String) -> (Bool, String) {
    switch key {
    case "microphone":
        var decided = false
        var granted = false
        AVCaptureDevice.requestAccess(for: .audio) { ok in
            granted = ok
            decided = true
        }
        pumpUntil(Date().addingTimeInterval(60)) { !decided }
        if !decided { return (false, "the microphone request was never answered") }
        return (granted, microphone(AVCaptureDevice.authorizationStatus(for: .audio)))

    case "speech_recognition":
        var decided = false
        var result = "unknown"
        SFSpeechRecognizer.requestAuthorization { status in
            result = speech(status)
            decided = true
        }
        pumpUntil(Date().addingTimeInterval(60)) { !decided }
        if !decided { return (false, "the speech-recognition request was never answered") }
        return (result == "granted", result)

    case "screen_recording":
        // Adds Vantage to the Screen Recording list and prompts. macOS applies the grant to a
        // process at launch, so a true return here does NOT mean this running process can see
        // the screen -- the caller's re-read and the recovery note both have to say that.
        let asked = CGRequestScreenCaptureAccess()
        return (asked, CGPreflightScreenCaptureAccess() ? "granted" : "denied")

    case "accessibility":
        // The prompting variant opens System Settings and returns the trust flag as it was
        // *before* the user did anything. It cannot grant, so nothing downstream may treat it
        // as an answer.
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
        let trusted = AXIsProcessTrustedWithOptions(options as CFDictionary)
        return (false, trusted ? "granted" : "denied")

    default:
        return (false, "unknown capability")
    }
}

let args = CommandLine.arguments
if args.count >= 3 && args[1] == "request" {
    let (asked, state) = requestStatus(args[2])
    var reply: [String: Any] = [
        "requested": asked,
        "state_after_request": state,
        "key": args[2],
    ]
    if args[2] == "accessibility" {
        reply["note"] = "the prompt opens System Settings; it grants nothing by itself"
    }
    if let data = try? JSONSerialization.data(withJSONObject: reply),
       let text = String(data: data, encoding: .utf8) {
        print(text)
        exit(0)
    }
    print("{\"requested\":false,\"error\":\"could not encode the request result\"}")
    exit(1)
}

// ---------------------------------------------------------------------------
// STATUS READ PATH -- must never prompt. A dashboard polls this, and a status check
// that raises a system dialog on every refresh is not a status check.
// ---------------------------------------------------------------------------

// Screen Recording. CGPreflightScreenCaptureAccess answers without prompting, which is
// the only acceptable behaviour for a status read; the request variant shows a dialog.
out["screen_recording"] = CGPreflightScreenCaptureAccess() ? "granted" : "denied"

// Accessibility. The non-prompting query: the "options" variant would add a system prompt.
out["accessibility"] = AXIsProcessTrusted() ? "granted" : "denied"

out["microphone"] = microphone(AVCaptureDevice.authorizationStatus(for: .audio))
out["speech_recognition"] = speech(SFSpeechRecognizer.authorizationStatus())

// Which process answered, because a status with no subject is a claim about nothing.
let bundle = Bundle.main
out["answered_by"] = [
    "pid": ProcessInfo.processInfo.processIdentifier,
    "executable": bundle.executablePath,
    "bundle_id": bundle.bundleIdentifier ?? "none",
]

if let data = try? JSONSerialization.data(withJSONObject: out),
   let text = String(data: data, encoding: .utf8) {
    print(text)
    exit(0)
}

print("{\"error\":\"could not encode the permission state\"}")
exit(1)
