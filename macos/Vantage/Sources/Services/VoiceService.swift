import Foundation
import AVFoundation
import Speech

/// Native macOS voice: Speech framework for STT, AVSpeechSynthesizer for TTS.
///
/// This lives in the *signed Vantage.app*, not the backend. macOS attributes
/// microphone and Speech-Recognition consent to the app bundle, so a voice
/// pipeline that runs here asks the user once and then just works; the backend's
/// `/voice/transcribe` runs as a bare Python process and Apple's Speech framework
/// SIGABRTs when it cannot attribute the caller to a signed bundle.
final class VoiceService: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {

    static let shared = VoiceService()

    @Published private(set) var isRecording = false
    @Published private(set) var liveTranscript = ""
    @Published private(set) var microphoneGranted = false
    @Published private(set) var speechGranted = false
    @Published var errorMessage: String?

    /// True while the synthesizer is actually producing speech.
    ///
    /// The UI needs to show that a reply is being spoken, and needs a Stop
    /// control next to that state. Without it, "on" is only a preference: the
    /// person cannot tell whether the silence means off, finished, or broken.
    /// Driven by the synthesizer's own delegate callbacks rather than by a
    /// timer or a guess, so it cannot drift from what is audible.
    @Published private(set) var isSpeaking = false

    /// Whether Vantage may speak its replies.
    ///
    /// Every `speak(...)` call site used to speak unconditionally, so a reply
    /// was audible with no way to turn it off — including in a quiet room or
    /// with headphones off. The preference lives here, on the object that owns
    /// the synthesizer, so all call sites honour it without each screen
    /// re-implementing the rule.
    ///
    /// Persisted, because a voice preference that resets on launch is not a
    /// preference. `false` is the default: speech is a deliberate choice, and
    /// an assistant that talks before being asked is a surprise.
    @Published var speakReplies: Bool {
        didSet {
            UserDefaults.standard.set(speakReplies, forKey: Self.speakRepliesKey)
            if !speakReplies { stopSpeaking() }
        }
    }

    /// Play a short sample, so the toggle proves it works rather than only
    /// claiming to. Deliberately bypasses the gate: the person asked to hear
    /// it, and the setting stays exactly as they left it.
    func previewVoice() {
        speakNow("This is how Vantage will read its replies.")
    }

    static let speakRepliesKey = "VantageSpeakReplies"

    /// Called on the main thread when a transcription completes with its final text.
    var onFinalTranscript: ((String) -> Void)?

    private let audioEngine = AVAudioEngine()
    private let synthesizer = AVSpeechSynthesizer()
    private var recognizer: SFSpeechRecognizer?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?

    override init() {
        // Read the stored preference before the first `speak` can run. A
        // `@Published var` with a default would reset to that default on every
        // launch, which for a voice setting means talking when the person had
        // turned it off.
        speakReplies = UserDefaults.standard.object(forKey: Self.speakRepliesKey) as? Bool ?? false
        super.init()
        // The synthesizer's own callbacks are what make `isSpeaking` true to
        // what is audible. No second synthesizer: one object owns one queue.
        synthesizer.delegate = self
    }

    // MARK: - Permissions

    @discardableResult
    func requestPermissions() async -> Bool {
        let mic = await requestMicrophone()
        let speech = await requestSpeech()
        return mic && speech
    }

    private func requestMicrophone() async -> Bool {
        let status = AVCaptureDevice.authorizationStatus(for: .audio)
        switch status {
        case .authorized:
            microphoneGranted = true
            return true
        case .notDetermined:
            let granted = await AVCaptureDevice.requestAccess(for: .audio)
            microphoneGranted = granted
            if !granted { errorMessage = "Microphone access was denied." }
            return granted
        case .denied, .restricted:
            microphoneGranted = false
            errorMessage = "Microphone access is off. Enable it in System Settings → Privacy & Security → Microphone."
            return false
        @unknown default:
            return false
        }
    }

    private func requestSpeech() async -> Bool {
        let status = SFSpeechRecognizer.authorizationStatus()
        switch status {
        case .authorized:
            speechGranted = true
            return true
        case .notDetermined:
            let granted = await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
                SFSpeechRecognizer.requestAuthorization { s in
                    cont.resume(returning: s == .authorized)
                }
            }
            speechGranted = granted
            if !granted { errorMessage = "Speech Recognition was denied." }
            return granted
        case .denied, .restricted:
            speechGranted = false
            errorMessage = "Speech Recognition is off. Enable it in System Settings → Privacy & Security → Speech Recognition."
            return false
        @unknown default:
            return false
        }
    }

    // MARK: - Recording + transcription

    func startRecording() {
        errorMessage = nil
        liveTranscript = ""

        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US")),
              recognizer.isAvailable else {
            errorMessage = "Speech recognition is unavailable on this Mac."
            return
        }
        self.recognizer = recognizer
        recognizer.defaultTaskHint = .dictation

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        recognitionRequest = request

        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            guard let self else { return }
            let isFinal = result?.isFinal == true || error != nil
            DispatchQueue.main.async {
                if let text = result?.bestTranscription.formattedString {
                    self.liveTranscript = text
                }
                if isFinal {
                    self.teardownAudio()
                    self.isRecording = false
                    let final = result?.bestTranscription.formattedString ?? ""
                    if !final.isEmpty {
                        self.onFinalTranscript?(final)
                    } else if let error {
                        self.errorMessage = "Transcription failed: \(error.localizedDescription)"
                    }
                }
            }
        }

        let inputNode = audioEngine.inputNode
        let format = inputNode.outputFormat(forBus: 0)
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.recognitionRequest?.append(buffer)
        }

        audioEngine.prepare()
        do {
            try audioEngine.start()
            isRecording = true
        } catch {
            errorMessage = "Could not start recording: \(error.localizedDescription)"
            teardownAudio()
        }
    }

    func stopRecording() {
        guard isRecording else { return }
        // No more buffers; the completion handler above finalises and teardowns.
        recognitionRequest?.endAudio()
        isRecording = false
    }

    private func teardownAudio() {
        audioEngine.inputNode.removeTap(onBus: 0)
        audioEngine.stop()
        recognitionRequest = nil
        recognitionTask?.cancel()
        recognitionTask = nil
        recognizer = nil
    }

    // MARK: - Speech output

    func speak(_ text: String) {
        // The preference is enforced here rather than at each call site: every
        // reply used to be spoken with no way to turn it off, and a rule
        // repeated in three screens is a rule one of them will forget.
        guard speakReplies else { return }
        speakNow(text)
    }

    /// Say it regardless of the preference. Only the settings preview uses
    /// this; everything that answers a person goes through `speak`.
    private func speakNow(_ text: String) {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        if synthesizer.isSpeaking { synthesizer.stopSpeaking(at: .immediate) }
        let utterance = AVSpeechUtterance(string: clean)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        synthesizer.speak(utterance)
    }

    func stopSpeaking() {
        if synthesizer.isSpeaking { synthesizer.stopSpeaking(at: .immediate) }
        // The cancel callback normally clears this, but a stop issued mid-start
        // can race it. Clearing here means the UI never shows "Speaking…" for
        // silence that the person just asked to end.
        if isSpeaking { isSpeaking = false }
    }

    // MARK: - AVSpeechSynthesizerDelegate

    // Callbacks may arrive on a non-main thread; publishing state must happen
    // on main. `MainActor.assumeIsolated` is not safe without a guarantee, so
    // hop explicitly when needed.

    private func setSpeaking(_ value: Bool) {
        if Thread.isMainThread {
            isSpeaking = value
        } else {
            DispatchQueue.main.async { [weak self] in self?.isSpeaking = value }
        }
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                           didStart utterance: AVSpeechUtterance) {
        setSpeaking(true)
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                           didFinish utterance: AVSpeechUtterance) {
        // If another utterance is already speaking (a queued reply), the
        // synthesizer is still producing speech: do not clear the state.
        if !synthesizer.isSpeaking { setSpeaking(false) }
    }

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer,
                           didCancel utterance: AVSpeechUtterance) {
        // `speakNow` stops the current utterance and immediately queues the
        // next one; the cancel callback for the old utterance can land while
        // the new one is already playing. Clearing unconditionally there left
        // the UI saying "not speaking" during audible speech.
        if !synthesizer.isSpeaking { setSpeaking(false) }
    }
}