// jarvis_speech.swift
//
// Minimal offline speech-to-text helper for JARVIS Forge.
//
// Uses Apple's Speech framework (SFSpeechRecognizer) to transcribe a local
// audio file. Compiled on demand by jarvis/voice/stt.py via `swiftc`.
//
// Usage:  jarvis_speech <audio-file> [language]
// Output: a single JSON object on stdout, e.g.
//         {"ok": true, "text": "open vs code", "confidence": 0.87}
//
// Requires Speech Recognition permission. When it is missing the process
// exits non-zero with an explanatory JSON error rather than a silent empty
// transcript.

import Foundation
import Speech

func emit(_ payload: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: payload),
          let line = String(data: data, encoding: .utf8) else {
        FileHandle.standardError.write(Data("{\"ok\":false,\"error\":\"encoding failure\"}\n".utf8))
        return
    }
    print(line)
    fflush(stdout)
}

func fail(_ message: String) -> Never {
    emit(["ok": false, "error": message])
    exit(1)
}

let arguments = CommandLine.arguments
guard arguments.count >= 2 else {
    fail("usage: jarvis_speech <audio-file> [language]")
}

let audioPath = arguments[1]
let language = arguments.count >= 3 ? arguments[2] : "en-US"

guard FileManager.default.fileExists(atPath: audioPath) else {
    fail("audio file not found: \(audioPath)")
}

// --- Authorisation ---------------------------------------------------------
let authSemaphore = DispatchSemaphore(value: 0)
var authStatus: SFSpeechRecognizerAuthorizationStatus = .notDetermined

SFSpeechRecognizer.requestAuthorization { status in
    authStatus = status
    authSemaphore.signal()
}

if authSemaphore.wait(timeout: .now() + 20) == .timedOut {
    fail("speech authorisation request timed out")
}

switch authStatus {
case .authorized:
    break
case .denied:
    fail("Speech Recognition permission denied. Enable it in System Settings -> "
        + "Privacy & Security -> Speech Recognition for the host application.")
case .restricted:
    fail("Speech Recognition is restricted on this device.")
case .notDetermined:
    fail("Speech Recognition permission was not granted.")
@unknown default:
    fail("Unknown Speech Recognition authorisation status.")
}

// --- Recognise -------------------------------------------------------------
guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: language)) else {
    fail("unsupported language: \(language)")
}

guard recognizer.isAvailable else {
    fail("speech recogniser is currently unavailable")
}

let request = SFSpeechURLRecognitionRequest(url: URL(fileURLWithPath: audioPath))
request.shouldReportPartialResults = false

let semaphore = DispatchSemaphore(value: 0)
var transcript = ""
var confidence: Float = 0
var failureReason: String?

recognizer.recognitionTask(with: request) { result, error in
    if let error = error {
        failureReason = error.localizedDescription
        semaphore.signal()
        return
    }
    guard let result = result, result.isFinal else { return }

    transcript = result.bestTranscription.formattedString
    let segments = result.bestTranscription.segments
    if !segments.isEmpty {
        confidence = segments.map { $0.confidence }.reduce(0, +) / Float(segments.count)
    }
    semaphore.signal()
}

if semaphore.wait(timeout: .now() + 60) == .timedOut {
    fail("transcription timed out")
}

if let reason = failureReason {
    fail("transcription failed: \(reason)")
}

emit(["ok": true, "text": transcript, "confidence": Double(confidence)])
