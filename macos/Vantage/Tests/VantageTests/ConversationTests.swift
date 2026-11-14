import XCTest
@testable import Vantage

/// The Command Center's conversation contract.
///
/// The app talks to `/command`, and three things about that contract were
/// wrong — and none of them would fail a build or a unit test of the view:
///
/// 1. **The request carried no session.** `CommandRequest.init` hardcoded
///    `session_id = nil`. The backend mints a fresh `SESS-` id for every
///    request that arrives without one, so every message started a *new*
///    conversation: the second message could not see the first, and a
///    follow-up like "explain the second part" had nothing to refer to.
///
/// 2. **The response model read fields the server does not send.** The struct
///    declared `status`, `message`, `confirmation_required` and `elapsed_ms`.
///    The server sends `reply`, `decision`, `needs_confirmation`,
///    `duration_ms`, `model_used`, `intent` and `session_id`. Because the
///    wrong ones were all optional, decoding *succeeded* — and the app
///    silently showed less than the backend had said.
///
/// 3. **Nothing distinguished who answered.** The transcript could not say
///    "answered by 9router", so a rules answer and a model answer looked
///    identical.
///
/// These tests pin the contract from the app's side. The backend's own side
/// is pinned by `tests/test_macos_*` in the Python suite.
final class ConversationTests: XCTestCase {

    // MARK: - The request carries the conversation

    func test_a_request_carries_the_session_it_is_given() {
        let request = APIModels.CommandRequest(text: "hello", sessionId: "SESS-abc123")
        XCTAssertEqual(request.session_id, "SESS-abc123",
                       "the request must carry the session, or every message is a new conversation")
        XCTAssertEqual(request.text, "hello")
        XCTAssertEqual(request.source, "native")
    }

    func test_the_first_request_of_a_conversation_has_no_session() {
        // Nothing to carry yet — the server mints one and echoes it back.
        let request = APIModels.CommandRequest(text: "hello")
        XCTAssertNil(request.session_id)
    }

    // MARK: - The response model matches what the server actually sends

    /// The server's own field names, from a live `/command` response.
    ///
    /// If the server changes a field, this test fails and says which one —
    /// rather than the app quietly rendering an empty caption.
    func test_the_response_decodes_the_servers_real_fields() throws {
        let json = """
        {
          "request_id": "REQ-abc",
          "session_id": "SESS-xyz",
          "reply": "Solar panels use the photovoltaic effect.",
          "intent": "unknown",
          "actions": [],
          "decision": null,
          "needs_confirmation": false,
          "confirmation_id": null,
          "blocked_reason": null,
          "job_id": "",
          "model_used": "9router:ag/gemini-2.5-flash",
          "duration_ms": 2335,
          "created_at": "2026-10-22T12:00:00Z"
        }
        """.data(using: .utf8)!

        let response = try JSONDecoder().decode(APIModels.CommandResponse.self, from: json)

        XCTAssertEqual(response.reply, "Solar panels use the photovoltaic effect.")
        XCTAssertEqual(response.session_id, "SESS-xyz")
        XCTAssertEqual(response.model_used, "9router:ag/gemini-2.5-flash")
        XCTAssertEqual(response.duration_ms, 2335)
        XCTAssertEqual(response.needs_confirmation, false)
        XCTAssertEqual(response.intent, "unknown")
    }

    /// A response with a confirmation decodes, and the gate is visible.
    func test_a_pending_confirmation_is_visible_in_the_response() throws {
        let json = """
        {
          "reply": "This needs your confirmation before I run it.",
          "needs_confirmation": true,
          "confirmation_id": "CONF-123",
          "decision": "CONFIRM",
          "model_used": "rules",
          "duration_ms": 40,
          "session_id": "SESS-1"
        }
        """.data(using: .utf8)!

        let response = try JSONDecoder().decode(APIModels.CommandResponse.self, from: json)
        XCTAssertTrue(response.needs_confirmation == true,
                      "an action waiting for a person must be visible to the app")
        XCTAssertEqual(response.decision, "CONFIRM")
        XCTAssertEqual(response.confirmation_id, "CONF-123")
    }

    // MARK: - The conversation

    func test_the_conversation_keeps_its_turns_in_order() {
        var conversation = Conversation()
        conversation.appendUser("What is a solar panel?")
        conversation.appendAssistant("It converts light to electricity.", meta: .init())

        XCTAssertEqual(conversation.messages.count, 2)
        XCTAssertEqual(conversation.messages[0].role, .user)
        XCTAssertEqual(conversation.messages[1].role, .assistant)
        XCTAssertEqual(conversation.messages[1].text, "It converts light to electricity.")
    }

    func test_a_response_without_a_session_does_not_clear_a_working_one() {
        var conversation = Conversation()
        conversation.adoptSession("SESS-good")
        // A response that names no session must not wipe the session that is
        // working — that would silently start a new conversation.
        conversation.adoptSession(nil)
        conversation.adoptSession("")
        XCTAssertEqual(conversation.outgoingSessionID, "SESS-good",
                       "an empty session in a response must not discard a live one")
    }

    func test_a_new_conversation_clears_both_the_turns_and_the_session() {
        var conversation = Conversation()
        conversation.appendUser("first question")
        conversation.adoptSession("SESS-first")
        XCTAssertEqual(conversation.outgoingSessionID, "SESS-first")

        conversation.startNewConversation()

        XCTAssertTrue(conversation.isEmpty, "a new conversation has no turns")
        XCTAssertNil(conversation.outgoingSessionID,
                     "a new conversation must not carry the old session, or the next "
                     + "message continues the conversation that just ended")
    }

    func test_the_server_session_is_adopted_so_the_next_turn_joins_the_conversation() {
        var conversation = Conversation()
        XCTAssertNil(conversation.outgoingSessionID)

        conversation.adoptSession("SESS-from-server")

        XCTAssertEqual(conversation.outgoingSessionID, "SESS-from-server",
                       "the server's session is what scopes the conversation")
    }

    // MARK: - The caption's facts

    func test_the_caption_is_empty_when_the_server_said_nothing() {
        // An empty caption is a real absence. A caption showing "rules" or
        // "0ms" when the server sent neither would be a fabrication.
        XCTAssertTrue(ChatMessage.MessageMeta().isEmpty)
    }

    func test_the_caption_carries_only_what_the_server_sent() {
        let meta = ChatMessage.MessageMeta(model: "9router:ag/gemini-2.5-flash",
                                            durationMS: 1200)
        XCTAssertFalse(meta.isEmpty)
        XCTAssertEqual(meta.model, "9router:ag/gemini-2.5-flash")
        XCTAssertEqual(meta.durationMS, 1200)
        XCTAssertFalse(meta.awaitingApproval)
    }

    func test_a_waiting_approval_is_visible_in_the_meta() {
        let meta = ChatMessage.MessageMeta(awaitingApproval: true)
        XCTAssertTrue(meta.awaitingApproval)
        XCTAssertFalse(meta.isEmpty)
    }
}
