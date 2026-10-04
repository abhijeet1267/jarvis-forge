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
