package com.vantage.companion.data.api

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject

/**
 * The one decoder every wire model is read with.
 *
 * `ignoreUnknownKeys`: a newer backend may add fields this app has never
 * heard of; that must not blank a screen.
 * `coerceInputValues`: an explicit `null` (or an out-of-range enum) for a
 * non-nullable field with a default falls back to that default instead of
 * throwing. A `"connection": null` or `"tasks": null` from a degraded backend
 * is a fact to render, not an exception to crash the refresh with.
 */
internal val WireJson: Json = Json {
    ignoreUnknownKeys = true
    encodeDefaults = true
    coerceInputValues = true
}

// ── Health ──

/** `GET /health/identity` — unauthenticated build/process identity.
 *
 *  Every field is optional except `service`: an older backend may omit any of
 *  them, and a missing field must never fail the decode that decides whether the
 *  backend is current.
 */
@Serializable
data class BackendIdentity(
    val service: String = "",
    @SerialName("instance_id") val instanceId: String? = null,
    val pid: Long? = null,
    @SerialName("started_at") val startedAt: Double? = null,
    @SerialName("app_version") val appVersion: String? = null,
    val revision: String? = null,
    val branch: String? = null,
    @SerialName("source_root") val sourceRoot: String? = null,
)

@Serializable
data class BackendCodeFreshness(
    val stale: Boolean? = null,
    val note: String? = null,
)

@Serializable
data class BackendIdentityResponse(
    val identity: BackendIdentity? = null,
    val code: BackendCodeFreshness? = null,
    val fingerprint: String? = null,
)

@Serializable
data class HealthResponse(
    val status: String,
    val tools: Int = 0,
    val agents: List<String> = emptyList(),
    @SerialName("llm_enabled") val llmEnabled: Boolean = false,
    @SerialName("llm_source") val llmSource: String? = null,
    @SerialName("ai_provider") val aiProvider: String? = null,
    @SerialName("provider_paths") val providerPaths: Map<String, String>? = null,
    @SerialName("pending_confirmations") val pendingConfirmations: Int? = 0,
    @SerialName("phone_urls") val phoneUrls: List<String>? = null,
    // Nested objects with mixed value types (bools, lists, sub-objects) —
    // the real /health returns e.g. auth_enabled: true, allowed_origins: [...].
    val security: JsonObject? = null,
    val privacy: JsonObject? = null,
    val devices: DeviceHealthInfo? = null,
    val peripherals: JsonObject? = null,
    // Real backend shape (verified against /health): provider names, not
    // nested status objects — {"email": "local_maildir", "calendar": "local_calendar"}.
    val integrations: Map<String, String>? = null,
    val memory: JsonObject? = null,
    val system: SystemHealthInfo? = null,
    val companion: JsonObject? = null,
) {
    val isOk: Boolean get() = status == "ok"
}

@Serializable
data class DeviceHealthInfo(
    val pairing: JsonObject? = null,
    // Real backend shape (verified): {"available": bool, "fingerprint": "...",
    // "cert_path": "...", "openssl": bool} — not a plain string.
    val transport: JsonObject? = null,
    @SerialName("paired_devices") val pairedDevices: Int? = 0,
)

@Serializable
data class SystemHealthInfo(
    val hostname: String? = null,
    val platform: String? = null,
    val version: String? = null,
)

// ── Command ──
//
// Wire shape verified against the live backend (jarvis/core/models.py
// CommandRequest/CommandResponse). The response is the whole pipeline trace:
// `reply` is the human sentence, `actions[]` carries per-step verification,
// `needs_confirmation`/`confirmation_id` drive the approval flow.

@Serializable
data class CommandRequest(
    val text: String,
    @SerialName("device_id") val deviceId: String = "phone",
    val source: String = "mobile",
    @SerialName("session_id") val sessionId: String? = null,
    val context: Map<String, String> = emptyMap(),
)

@Serializable
data class ActionResult(
    val ok: Boolean? = null,
    val tool: String? = null,
    val device: String? = null,
    val output: JsonObject? = null,
    val message: String? = null,
    val error: String? = null,
    val verified: Boolean? = null,
    @SerialName("verification") val verification: String? = null,
    @SerialName("verification_note") val verificationNote: String? = null,
) {
    /** Verification is tri-state: SUCCESS / FAILED / UNKNOWN (the honest default). */
    val isVerified: Boolean get() = verification == "SUCCESS"
}

@Serializable
data class CommandResponse(
    val reply: String? = null,
    @SerialName("request_id") val requestId: String? = null,
    val intent: String? = null,
    val decision: String? = null,
    val actions: List<ActionResult> = emptyList(),
    @SerialName("needs_confirmation") val needsConfirmation: Boolean? = false,
    @SerialName("confirmation_id") val confirmationId: String? = null,
    @SerialName("blocked_reason") val blockedReason: String? = null,
    @SerialName("recovery_notes") val recoveryNotes: List<String> = emptyList(),
) {
    val confirmationRequired: Boolean get() = needsConfirmation == true
    val isBlocked: Boolean get() = decision == "BLOCK"

    /** One-line outcome for the chat bubble: the reply, or the block reason. */
    val displayText: String get() = when {
        !reply.isNullOrBlank() -> reply
        !blockedReason.isNullOrBlank() -> blockedReason
        actions.any { it.ok == false } -> "Something went wrong"
        else -> "Done"
    }

    /** True only when at least one action reports a verified external effect. */
    val hasVerifiedEffect: Boolean get() = actions.any { it.isVerified }
}

// ── Confirmations ──
//
// A row exactly as jarvis/security/policy.py `_row_to_confirmation` returns
// (the `confirmations` table): confirmation_id / tool / params / device /
// risk (int tier) / preview / state / created_at / requested_surface. The UI
// keeps its `id`/`action`/`riskLevel` names as computed views over the real
// fields, so the list key and approve/deney paths are unchanged.

@Serializable
data class Confirmation(
    @SerialName("confirmation_id") val confirmationId: String,
    @SerialName("request_id") val requestId: String? = null,
    val tool: String? = null,
    val params: JsonObject? = null,
    val device: String? = null,
    val risk: Int? = null,
    val preview: String? = null,
    val state: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("requested_surface") val requestedSurface: String? = null,
) {
    /** The id the list keys on and approve/deny send. */
    val id: String get() = confirmationId
    /** The action name (the tool), for the card header. */
    val action: String get() = tool ?: "unknown"
    /** Human one-liner: the server-generated preview, else the tool. */
    val description: String get() = preview?.ifBlank { null } ?: tool ?: "unknown"
    /**
     * Risk tier as a label. Backend `risk` is the RiskLevel int
     * (0=READ,1=LOW,2=EXTERNAL,3=DESTRUCTIVE,4=PHYSICAL).
     */
    val riskLevel: String get() = when (risk) {
        0 -> "read"
        1 -> "low"
        2 -> "external"
        3 -> "destructive"
        4 -> "physical"
        else -> "medium"
    }
}

// ── Devices ──
//
// A row exactly as jarvis/api/app.py `/devices` returns (the device registry
// model): device_id / type / platform / capabilities / trust_level / online /
// auth_state / paired_at / last_seen / is_host. There is no `id`, `state`,
// `device_name` or `surface` field on the wire — those were invented and a
// required `id` made decoding throw against the live backend. The UI keeps
// those names as computed views over the real fields.

@Serializable
data class DeviceInfo(
    @SerialName("device_id") val deviceId: String? = null,
    val type: String? = null,
    val platform: String? = null,
    val capabilities: List<String> = emptyList(),
    @SerialName("trust_level") val trustLevel: String? = null,
    val online: Boolean = false,
    @SerialName("auth_state") val authState: String? = null,
    @SerialName("paired_at") val pairedAt: String? = null,
    @SerialName("last_seen") val lastSeen: String? = null,
    val fingerprint: String? = null,
    @SerialName("is_host") val isHost: Boolean = false,
) {
    /** Stable key / fallback display identity. */
    val id: String get() = deviceId ?: type ?: "device"
    /** Pairing state (the real `auth_state`: unpaired / paired / revoked). */
    val state: String? get() = authState
    /** A phone-shaped device, by type or platform. */
    val isPhone: Boolean get() = type == "android" || platform == "android"
    /** Human name: the id (there is no separate name field on the backend). */
    val displayName: String get() = deviceId ?: type ?: "device"
    val isPaired: Boolean get() = authState == "paired"
}

// ── Pairing ──

@Serializable
data class PairingRequest(
    @SerialName("device_id") val deviceId: String,
    @SerialName("device_name") val deviceName: String,
    @SerialName("public_key") val publicKey: String = "",
)

@Serializable
data class PairingRedeemRequest(
    val code: String,
    /**
     * The device id this phone calls itself, and its name.
     *
     * Redeeming the pairing code is where the phone's identity is established,
     * so this is where the backend learns it. Without it the id travels in the
     * `X-Vantage-Device-Id` header naming a device the registry has never
     * heard of, and the permission gate has nothing to aim at.
     */
    @SerialName("device_id") val deviceId: String? = null,
    @SerialName("device_name") val deviceName: String? = null,
)

/**
 * POST /companion/pairing-code on the companion gateway (the Mac displays
 * this as a QR code or short code). One-shot, 120 s TTL; the fingerprint is
 * what the client pins for TLS verification (R1.4).
 */
@Serializable
data class PairingCodeResponse(
    val code: String? = null,
    @SerialName("certificate_fingerprint") val certificateFingerprint: String? = null,
    @SerialName("expires_in_seconds") val expiresInSeconds: Int? = null,
    @SerialName("companion_port") val companionPort: Int? = null,
)

@Serializable
data class PairingResponse(
    @SerialName("pairing_id") val pairingId: String? = null,
    @SerialName("device_id") val deviceId: String? = null,
    val state: String? = null,
    val message: String? = null,
    val code: String? = null,
)

// ── Session ──

@Serializable
data class SessionResponse(
    // The gateway's redeem response does not carry this; it is present only
    // on the legacy /auth/session shape. Default so either decodes.
    @SerialName("auth_enabled") val authEnabled: Boolean = false,
    @SerialName("session_token") val sessionToken: String? = null,
    @SerialName("expires_in") val expiresIn: Double? = null,
    val surface: String? = null,
)

// ── Emergency Stop ──

@Serializable
data class EmergencyStopResult(
    val message: String? = null,
    val verified: Boolean? = null,
    @SerialName("jobs_cancelled") val jobsCancelled: Int? = null,
    @SerialName("cancelled_job_ids") val cancelledJobIds: List<String> = emptyList(),
    @SerialName("jobs_stuck") val jobsStuck: List<String> = emptyList(),
    // Honest capability boundary: "not_available" means no actuator bridge
    // exists, and the UI must say so rather than claim motion was halted.
    @SerialName("physical_stop") val physicalStop: String? = null,
)

@Serializable
data class EmergencyStopResponse(
    val ok: Boolean? = null,
    val verified: Boolean? = null,
    val result: EmergencyStopResult? = null,
) {
    val detail: String? get() = result?.message
    /** True only if physical actuators were actually signalled off. */
    val physicalStopAvailable: Boolean get() = result?.physicalStop != "not_available"
}

// ── Confirm Action ──

@Serializable
data class ConfirmActionResponse(
    val status: String? = null,
    val action: String? = null,
    val result: JsonObject? = null,
    val error: String? = null,
)
// ---------------------------------------------------------------------------
// Google Calendar + Tasks
//
// Shapes returned by the backend's /google/* read routes. `status` and
// `readSucceeded` are kept because the phone must be able to tell a failed
// read from a genuinely empty day -- the two look identical otherwise.
// ---------------------------------------------------------------------------

/**
 * How a connection should be drawn. The states come from the backend's
 * `ConnectionState` (jarvis/integrations/google_auth.py): not_configured /
 * disconnected / connected / expired / error. Anything else -- including a
 * blank state from an older or degraded backend -- is [UNKNOWN]: it is
 * rendered as a neutral fact, never as "connected" and never as an alarm.
 */
enum class ConnectionTone { CONNECTED, ERROR, NEUTRAL, UNKNOWN }

@Serializable
data class GoogleConnection(
    val state: String = "",
    val detail: String? = null,
    val account: String? = null,
    val scopes: List<String> = emptyList(),
    val configured: Boolean = false,
    @SerialName("has_token") val hasToken: Boolean = false,
    val usable: Boolean = false,
) {
    /** Only a live, working connection counts. */
    val isConnected: Boolean get() = usable

    val tone: ConnectionTone
        get() = when {
            isConnected -> ConnectionTone.CONNECTED
            state == "expired" || state == "error" -> ConnectionTone.ERROR
            state == "not_configured" || state == "disconnected" -> ConnectionTone.NEUTRAL
            // Includes state == "connected" with usable == false: the backend
            // says connected but its live check did not pass, so the honest
            // label is "not confirmed", not "connected".
            else -> ConnectionTone.UNKNOWN
        }

    /**
     * The truthful one-line label. Never blank, and never claims "connected"
     * on an unrecognised or unverified state.
     */
    val stateLabel: String
        get() = when {
            isConnected -> "Connected to Google"
            state == "not_configured" -> "Google is not set up"
            state == "disconnected" -> "Not connected"
            state == "expired" -> "The connection expired"
            state == "error" -> "Could not reach Google"
            state == "connected" -> "Connection not confirmed"
            state.isBlank() -> "Google status unavailable"
            else -> "Google status: $state"
        }
}

@Serializable
data class GoogleSetup(
    val configured: Boolean = false,
    val setup: List<String> = emptyList(),
    @SerialName("redirect_uri") val redirectUri: String? = null,
)

@Serializable
data class GoogleStatusResponse(
    val connection: GoogleConnection = GoogleConnection(),
    val setup: GoogleSetup = GoogleSetup(),
)

@Serializable
data class GoogleEvent(
    val uid: String = "",
    val title: String = "",
    val start: String? = null,
    val end: String? = null,
    val location: String? = null,
    @SerialName("all_day") val allDay: Boolean = false,
    @SerialName("duration_minutes") val durationMinutes: Int? = null,
)

@Serializable
data class GoogleEventsResponse(
    val provider: String? = null,
    val status: String? = null,
    @SerialName("read_succeeded") val readSucceeded: Boolean = false,
    val detail: String? = null,
    val count: Int = 0,
    val events: List<GoogleEvent> = emptyList(),
)

@Serializable
data class GoogleTask(
    val id: String = "",
    val title: String = "",
    val notes: String? = null,
    val status: String? = null,
    val done: Boolean = false,
    val due: String? = null,
    @SerialName("list_id") val listId: String? = null,
    val source: String? = null,
) {
    /** A blank title must not draw a blank row (backend data can be ragged). */
    val displayTitle: String get() = title.trim().ifBlank { "(untitled task)" }

    /**
     * Only a task that is not already done and has a real title can be asked
     * to complete -- a blank title cannot be named in a /command sentence.
     */
    val isCompletable: Boolean get() = !done && title.isNotBlank()
}

@Serializable
data class GoogleTasksResponse(
    val provider: String? = null,
    val status: String? = null,
    @SerialName("read_succeeded") val readSucceeded: Boolean = false,
    val detail: String? = null,
    val count: Int = 0,
    val tasks: List<GoogleTask> = emptyList(),
)
