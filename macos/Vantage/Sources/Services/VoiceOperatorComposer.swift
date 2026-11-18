import Foundation

/// The decisions the Voice Operator composer makes, separated from its layout.
///
/// SwiftUI state is not reachable from a test without a running app, but the
/// rules that decide *what gets sent* are ordinary functions. Keeping them here
/// means the two things that could actually go wrong — sending a duplicate turn,
/// and sending an empty one — are checkable without a GUI.
///
/// This does not replace pressing the button. It rules out the specific errors
/// that a person would never notice until they had sent the same command twice.
enum VoiceOperatorComposer {

    /// The text to send, or empty when there is nothing to send.
    ///
    /// A recognised transcript wins over the typed field, because a person who
    /// just spoke expects their words to be used. A transcript of nothing but
    /// whitespace is not a transcript, so the typed field is consulted instead
    /// rather than sending a blank turn.
    static func text(draftTranscript: String, typedCommand: String) -> String {
        let spoken = draftTranscript.trimmingCharacters(in: .whitespacesAndNewlines)
        if !spoken.isEmpty { return spoken }
        return typedCommand.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Whether Send should be enabled.
    ///
    /// `isThinking` is the conversation service's own flag and `isSending` is
    /// this view's; both must be clear. Either one alone has been enough to
    /// start a second turn before the first had returned.
    static func canSend(draftTranscript: String, typedCommand: String,
                        isSending: Bool, isThinking: Bool) -> Bool {
        !isSending && !isThinking && !text(draftTranscript: draftTranscript,
                                           typedCommand: typedCommand).isEmpty
    }

    /// The composer state after a send has been accepted.
    ///
    /// Both fields are cleared *before* the request is awaited, so a second
    /// Return cannot resend the same words while the first is in flight. This is
    /// the exact shape the view uses; it is here so the rule is testable.
    static func afterSend() -> (draftTranscript: String, typedCommand: String) {
        ("", "")
    }
}