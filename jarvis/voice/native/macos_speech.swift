// jarvis_speech.swift
//
// Minimal offline speech-to-text helper for Vantage.
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

// --- Waiting without deadlocking ourselves ---------------------------------
// Both callbacks below arrive on the main queue. A semaphore wait on the main
// thread therefore blocks the only thread that can release it, so every call
// ran to its deadline and reported "timed out" — which reads like an Apple
// refusal and is not one. Pumping the run loop is the difference between a
// machine that cannot hear and one that was never given the chance to answer.
func pumpUntil(_ deadline: Date, pending: () -> Bool) {
    while pending() && Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
    }
}

// --- Authorisation ---------------------------------------------------------
var authStatus: SFSpeechRecognizerAuthorizationStatus = .notDetermined
var authAnswered = false

SFSpeechRecognizer.requestAuthorization { status in
    DispatchQueue.main.async {
        authStatus = status
        authAnswered = true
    }
}

pumpUntil(Date().addingTimeInterval(20)) { !authAnswered }
if !authAnswered {
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

var transcript = ""
var confidence: Float = 0
var failureReason: String?
var taskAnswered = false
var keepAlive: SFSpeechRecognitionTask?

keepAlive = recognizer.recognitionTask(with: request) { result, error in
    DispatchQueue.main.async {
        if let error = error {
            failureReason = error.localizedDescription
            taskAnswered = true
            return
        }
        guard let result = result, result.isFinal else { return }

        transcript = result.bestTranscription.formattedString
        let segments = result.bestTranscription.segments
        if !segments.isEmpty {
            confidence = segments.map { $0.confidence }.reduce(0, +) / Float(segments.count)
        }
        taskAnswered = true
    }
}

pumpUntil(Date().addingTimeInterval(60)) { !taskAnswered }
if !taskAnswered {
    fail("transcription timed out")
}
keepAlive?.cancel()

if let reason = failureReason {
    fail("transcription failed: \(reason)")
}

emit(["ok": true, "text": transcript, "confidence": Double(confidence)])
