import Foundation

/// One turn in a conversation.
///
/// A message is either something the person said, or something Vantage
/// answered. `meta` carries the facts about *how* the answer was produced —
/// which model answered, how long the server took, whether an action is
/// waiting for approval — so the transcript can show them without the
/// transcript text itself having to carry a status prefix.
struct ChatMessage: Identifiable, Equatable {
    enum Role: String, Equatable {
        case user
        case assistant
        /// A result of something Vantage *did* (an attachment was read, an
        /// approval was executed), not a chat answer. It renders as a compact
        /// inset row so a tool result cannot be mistaken for the model's reply.
        case tool
        case error
    }

    let id = UUID()
    let text: String
    let role: Role
    /// The facts about how this message came to be. Empty for a user's own words.
    var meta: MessageMeta

    struct MessageMeta: Equatable {
        /// Who answered — e.g. "9router:ag/gemini-2.5-flash", or "rules" when
        /// the rule router answered on its own. Empty when the server did not say.
        var model: String = ""
        /// The server's own timing for the turn.
        var durationMS: Int?
        /// The action is held for a person's approval.
        var awaitingApproval: Bool = false
        /// The action was refused by the permission engine.
        var blocked: Bool = false
        /// The conversation this message belongs to.
        var sessionID: String = ""

        /// Whether a *model* answered, as opposed to the rule router or a
        /// lookup.
        ///
        /// The server's `model_used` is "rules" when the answer came from the
        /// deterministic path — including the Wikipedia fallback it uses when
        /// no provider is configured. That fallback can return an article
        /// about a different subject from the one that was asked about, so
        /// "rules" is not the same as "a model answered this".
        var isModelAnswer: Bool {
            let label = model.lowercased()
            return !label.isEmpty && label != "rules" && label != "none"
        }

        var isEmpty: Bool {
            model.isEmpty && durationMS == nil && !awaitingApproval && !blocked
        }
    }
}

/// A conversation: an ordered list of turns, and the id that scopes them.
///
/// The session id is the whole point. The backend mints a fresh `SESS-` id for
/// every request that arrives without one, so a surface that does not send a
/// session gets a brand new conversation on every message — and a follow-up
/// like "explain the second part" has nothing to refer to. The server echoes
/// the session it used back in its response, so a surface can adopt it.
///
/// Starting a new conversation is a deliberate act: `startNewConversation()`
/// clears the transcript *and* the session id, so the next message begins a
/// new thread rather than appending to the old one.
struct Conversation {
    private(set) var messages: [ChatMessage] = []
    private(set) var sessionID: String?

    /// The session to send with the next message, if there is one.
    var outgoingSessionID: String? { sessionID }

    var isEmpty: Bool { messages.isEmpty }

    /// Add the person's own turn.
    mutating func appendUser(_ text: String) {
        messages.append(ChatMessage(text: text, role: .user, meta: .init()))
    }

    /// Add Vantage's answer.
    mutating func appendAssistant(_ text: String, meta: ChatMessage.MessageMeta) {
        messages.append(ChatMessage(text: text, role: .assistant, meta: meta))
    }

    /// Add a failure as a turn of its own, so the conversation does not lose
    /// its shape when a request fails.
    mutating func appendError(_ text: String) {
        messages.append(ChatMessage(text: text, role: .error, meta: .init()))
    }

    /// Add a turn that is not a chat reply — an attachment's result, say.
    ///
    /// It goes into the same transcript because it happened in the same
    /// conversation, and a result the person cannot see is a result they
    /// cannot trust.
    mutating func append(_ text: String, role: ChatMessage.Role, meta: ChatMessage.MessageMeta = .init()) {
        messages.append(ChatMessage(text: text, role: role, meta: meta))
    }

    /// Drop the last turn, when it is a failure that is about to be retried.
    ///
    /// A retry must not repeat the question in the transcript, but it must not
    /// hide the failure either — so the failed turn is removed only when it is
    /// the very last one and is about to be replaced by a new attempt.
    mutating func removeLastIfError() {
        if messages.last?.role == .error {
            messages.removeLast()
        }
    }

    /// Adopt the session the server says this turn belongs to.
    ///
    /// Only adopted when the server actually named one. A response without a
    /// session id must not clear a session that is working, or the next turn
    /// would start a new conversation and lose the history.
    mutating func adoptSession(_ id: String?) {
        if let id, !id.isEmpty {
            sessionID = id
        }
    }

    /// Begin a new conversation.
    ///
    /// The session id is cleared, not just the messages: the next message must
    /// not be threaded onto the conversation that just ended.
    mutating func startNewConversation() {
        messages.removeAll()
        sessionID = nil
    }
}
