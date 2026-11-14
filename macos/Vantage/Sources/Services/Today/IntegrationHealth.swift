import Foundation

/// A real, per-service test, and the vocabulary to describe its result.
///
/// The Connections screen used to infer "healthy" from the connection state:
/// one Google token, one pill. That was true and useless at the same time —
/// measured on this machine, the first project had Calendar enabled and Tasks
/// disabled, and the second had the mirror image, so the single pill read
/// "Connected" while one of the two services could not be used at all.
///
/// This type separates the question "is there a connection" from "does each
/// service actually work", and the test is the read itself: the same
/// `/google/calendar/events` and `/google/tasks` calls the screens make. There
/// is no synthetic ping, because a ping that is not the real read can be green
/// while the read fails.
enum IntegrationHealth {

    /// One service's real outcome. Every case is a different fact and a
    /// different fix, which is why they are separate values rather than a bool.
    enum State: Equatable {
        /// The read succeeded with data, just now.
        case working(items: Int)
        /// The read succeeded and there is nothing — a real, empty answer.
        case workingEmpty
        /// A token exists but the service refused it. Reconnect.
        case authRequired(String)
        /// The service is disabled for this OAuth project, or the account
        /// lacks the permission. A console/consent problem, not a token one.
        case permissionDenied(String)
        /// The connection is read-only and the operation needed a write.
        case readOnly(String)
        /// The request did not finish in time.
        case timedOut(String)
        /// The backend or the provider could not be reached.
        case unavailable(String)
        /// Nothing has established this service's state — including "connected
        /// but not tested since this app started".
        case unknown(String)

        var label: String {
            switch self {
            case .working: return "Working"
            case .workingEmpty: return "Working"
            case .authRequired: return "Sign-in required"
            case .permissionDenied: return "Permission or API disabled"
            case .readOnly: return "Read-only"
            case .timedOut: return "Timed out"
            case .unavailable: return "Unavailable"
            case .unknown: return "Not tested"
            }
        }

        /// Green only where the service was actually exercised successfully.
        /// Deliberately: there is no case that means "probably fine".
        var isHealthy: Bool {
            switch self {
            case .working, .workingEmpty: return true
            default: return false
            }
        }

        /// The sentence that tells a person what to do next. Nil only for the
        /// healthy cases, which need no recovery.
        var guidance: String? {
            switch self {
            case .working(let count):
                return count == 0 ? nil
                    : "Read succeeded just now — \(count) item(s) returned."
            case .workingEmpty:
                return "Read succeeded just now. There is nothing there, which is a "
                    + "real answer rather than a failure."
            case .authRequired(let detail):
                return detail + " Reconnect the account in the Google connection above."
            case .permissionDenied(let detail):
                return detail
            case .readOnly(let detail):
                return detail
            case .timedOut(let detail):
                return detail + " Press Test again, or check that the backend can reach Google."
            case .unavailable(let detail):
                return detail
            case .unknown(let detail):
                return detail
            }
        }
    }

    /// Which service a test belongs to.
    enum Service: String, Equatable {
        case calendar, tasks
        var title: String {
            switch self {
            case .calendar: return "Google Calendar"
            case .tasks: return "Google Tasks"
            }
        }
    }

    /// Classify a Calendar read. The response's own `status` and
    /// `read_succeeded` decide — never the HTTP code alone, because a 200 with
    /// `provider_error` is a failed read.
    static func calendar(_ response: APIModels.GoogleEventsResponse) -> State {
        if response.read_succeeded != true {
            return classify(detail: response.detail, status: response.status)
        }
        let count = response.count ?? response.events?.count ?? 0
        return count > 0 ? .working(items: count) : .workingEmpty
    }

    static func tasks(_ response: APIModels.GoogleTasksResponse) -> State {
        if response.read_succeeded != true {
            return classify(detail: response.detail, status: response.status)
        }
        let count = response.count ?? response.tasks?.count ?? 0
        return count > 0 ? .working(items: count) : .workingEmpty
    }

    /// A transport failure (the request itself never produced a response).
    static func transport(_ error: Error) -> State {
        let text = error.localizedDescription
        let lowered = text.lowercased()
        if lowered.contains("timed out") || lowered.contains("timeout") {
            return .timedOut(text)
        }
        if lowered.contains("401") || lowered.contains("unauthor") {
            return .authRequired(text)
        }
        return .unavailable("The request did not complete: \(text)")
    }

    /// Map a payload's own words onto a state.
    ///
    /// The backend already distinguishes these cases in its `detail` sentences
    /// (a `SERVICE_DISABLED` 403 reads differently from a scope problem). This
    /// reads those sentences rather than flattening every failure into
    /// "unavailable", which would send a person to the wrong fix.
    static func classify(detail: String?, status: String?) -> State {
        let text = (detail ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let lowered = text.lowercased()
        let fallback = text.isEmpty ? "The service did not explain the failure." : text

        if lowered.contains("not enabled for this oauth project")
            || lowered.contains("has not been used in project")
            || lowered.contains("api is not enabled")
            || lowered.contains("service_disabled") {
            return .permissionDenied(fallback)
        }
        if lowered.contains("read-only") || lowered.contains("read only") {
            return .readOnly(fallback)
        }
        if lowered.contains("reconnect") || lowered.contains("rejected the access token")
            || status == "auth_error" {
            return .authRequired(fallback)
        }
        if lowered.contains("timed out") || lowered.contains("timeout") {
            return .timedOut(fallback)
        }
        if lowered.contains("could not reach") || status == "unavailable"
            || lowered.contains("not configured") || status == "not_configured" {
            return .unavailable(fallback)
        }
        return .unavailable(fallback)
    }
}