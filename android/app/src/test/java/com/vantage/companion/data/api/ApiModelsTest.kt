package com.vantage.companion.data.api

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Wire-shape tests against the REAL backend contract — fixtures captured from
 * a live server (jarvis/api/app.py), not an imagined one. The previous suite
 * tested fields the backend does not return (`version`, `active_tasks`,
 * `id` on devices, string `risk` on confirmations); those decodes would have
 * thrown or silently dropped data against the real API.
 */
class ApiModelsTest {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    // ------------------------------------------------------------------
    // Health — trimmed capture of a real GET /health.
    // ------------------------------------------------------------------

    @Test
    fun health_decodes_real_payload() {
        val body = """
        {
          "status": "ok",
          "tools": 148,
          "agents": ["laptop", "memory", "phone"],
          "llm_enabled": false,
          "llm_source": "none",
          "ai_provider": "openai",
          "provider_paths": {"chat": "openai", "embed": "openai"},
          "pending_confirmations": 2,
          "phone_urls": ["http://192.168.1.20:8765/phone"],
          "security": {
            "auth_enabled": true,
            "token_path": "/Users/x/.jarvis-forge/api_token",
            "allowed_origins": ["http://127.0.0.1:8765"],
            "host_header_enforced": true,
            "rate_limit_per_minute": 600
          },
          "privacy": {
            "privacy_mode": false,
            "redaction_enabled": true,
            "delete_scopes": ["audit", "conversations", "everything", "memories", "projects", "tasks"],
            "tracked_tables": ["action_logs", "memories"]
          },
          "devices": {
            "pairing": {
              "key_dir": "/tmp/devices",
              "pending_requests": 0,
              "max_pending_requests": 5,
              "secret_bytes": 32,
              "auth_scheme": "HMAC-SHA256 pre-shared key"
            },
            "transport": {"available": true, "fingerprint": "36e4…", "cert_path": "/tmp/server.crt", "openssl": true},
            "paired_devices": 1
          },
          "peripherals": {"car": {"configured": false, "offline": true}},
          "integrations": {"email": "local_maildir", "calendar": "local_calendar"},
          "scheduler": {"running": true, "tick_seconds": 30, "total": 0, "by_status": {}},
          "memory_retrieval": {"mode": "keyword_only"},
          "embeddings": {"active": "hashing", "semantic": false, "dim": 256},
          "research": {"sources": ["arxiv", "local"]},
          "companion": {"running": false, "port": 8766, "detail": "LAN mode is off"}
        }
        """.trimIndent()

        val health = json.decodeFromString<HealthResponse>(body)

        assertEquals("ok", health.status)
        assertTrue(health.isOk)
        assertEquals(148, health.tools)
        assertEquals(listOf("laptop", "memory", "phone"), health.agents)
        assertFalse(health.llmEnabled)
        assertEquals("openai", health.aiProvider)
        assertEquals("openai", health.providerPaths?.get("chat"))
        assertEquals(2, health.pendingConfirmations)
        assertEquals(1, health.phoneUrls?.size)
        // Mixed-type nesting must survive (bools, arrays, sub-objects).
        assertEquals("true", health.security?.get("auth_enabled")?.toString())
        assertEquals(1, health.devices?.pairedDevices)
        assertEquals("true", health.devices?.transport?.get("available")?.toString())
        assertEquals("local_maildir", health.integrations?.get("email"))
        assertEquals("8766", health.companion?.get("port")?.toString())
    }

    @Test
    fun health_missing_optional_fields_default() {
        val health = json.decodeFromString<HealthResponse>("""{"status":"ok"}""")
        assertEquals("ok", health.status)
        assertFalse(health.llmEnabled)
        assertNull(health.llmSource)
        assertNull(health.aiProvider)
        assertEquals(0, health.tools)
        assertTrue(health.agents.isEmpty())
        assertNull(health.devices)
        assertNull(health.companion)
        assertFalse(json.decodeFromString<HealthResponse>("""{"status":"degraded"}""").isOk)
    }

    // ------------------------------------------------------------------
    // Session — the gateway redeem shape (jarvis/companion/gateway.py):
    //   {"session_token", "surface", "expires_in"}
    // ------------------------------------------------------------------

    @Test
    fun session_response_decodes_redeem_shape() {
        val session = json.decodeFromString<SessionResponse>(
            """{"session_token":"CMP_a1b2c3d4e5","surface":"phone","expires_in":28800}"""
        )
        assertEquals("CMP_a1b2c3d4e5", session.sessionToken)
        assertEquals("phone", session.surface)
        assertEquals(28800.0, session.expiresIn)
    }

    @Test
    fun session_response_without_token_is_blank() {
        val session = json.decodeFromString<SessionResponse>("""{"surface":"phone"}""")
        assertNull(session.sessionToken)
    }

    // ------------------------------------------------------------------
    // Command — the real /command contract (jarvis/core/models.py).
    // The response is a pipeline trace: reply + actions[] + decision, NOT a
    // {result, status, verification} blob.
    // ------------------------------------------------------------------

    @Test
    fun command_request_encodes_field_names() {
        val req = CommandRequest(
            text = "summarize my day",
            deviceId = "phone_test",
            source = "mobile",
            sessionId = null,
            context = mapOf("surface" to "phone"),
        )
        val obj = json.parseToJsonElement(
            json.encodeToString(CommandRequest.serializer(), req)
        ) as kotlinx.serialization.json.JsonObject
        assertEquals("summarize my day", (obj["text"] as JsonPrimitive).content)
        assertEquals("phone_test", (obj["device_id"] as JsonPrimitive).content)
        assertEquals("mobile", (obj["source"] as JsonPrimitive).content)
        // null is accepted by the live backend (verified: 200), so it must
        // encode as null, not be omitted with a wrong value.
        assertTrue(obj["session_id"] is kotlinx.serialization.json.JsonNull)
    }

    @Test
    fun command_request_defaults_match_backend_defaults() {
        val obj = json.parseToJsonElement(
            json.encodeToString(CommandRequest.serializer(), CommandRequest(text = "hello"))
        ) as kotlinx.serialization.json.JsonObject
        assertEquals("phone", (obj["device_id"] as JsonPrimitive).content)
        assertEquals("mobile", (obj["source"] as JsonPrimitive).content)
    }

    @Test
    fun command_response_decodes_real_executed_shape() {
        val body = """
        {
          "reply": "It is 17:23 on Friday, 16 October 2026.",
          "request_id": "REQ_abc123",
          "session_id": "SESS_1",
          "intent": "time",
          "decision": "EXECUTE",
          "needs_confirmation": false,
          "confirmation_id": null,
          "blocked_reason": null,
          "actions": [
            {
              "ok": true,
              "tool": "system_status",
              "device": "laptop_main",
              "output": {"time": "17:23"},
              "message": "read clock",
              "error": null,
              "verified": true,
              "verification": "SUCCESS",
              "verification_note": "",
              "duration_ms": 3
            }
          ],
          "recovery_notes": []
        }
        """.trimIndent()
        val resp = json.decodeFromString<CommandResponse>(body)
        assertEquals("It is 17:23 on Friday, 16 October 2026.", resp.reply)
        assertEquals("It is 17:23 on Friday, 16 October 2026.", resp.displayText)
        assertEquals("EXECUTE", resp.decision)
        assertFalse(resp.isBlocked)
        assertFalse(resp.confirmationRequired)
        assertTrue(resp.hasVerifiedEffect)
        assertEquals(1, resp.actions.size)
        assertEquals("SUCCESS", resp.actions[0].verification)
        assertTrue(resp.actions[0].isVerified)
    }

    @Test
    fun command_response_decodes_confirmation_required() {
        val resp = json.decodeFromString<CommandResponse>(
            """{"reply":"I need your approval to send this email.","decision":"CONFIRM","needs_confirmation":true,"confirmation_id":"CONF_1"}"""
        )
        assertTrue(resp.confirmationRequired)
        assertEquals("CONF_1", resp.confirmationId)
        assertEquals("I need your approval to send this email.", resp.displayText)
    }

    @Test
    fun command_response_decodes_blocked() {
        val resp = json.decodeFromString<CommandResponse>(
            """{"reply":"I won't do that.","decision":"BLOCK","blocked_reason":"This would delete everything."}"""
        )
        assertTrue(resp.isBlocked)
        // displayText prefers the reply when present, else the block reason.
        assertEquals("I won't do that.", resp.displayText)
        val bare = json.decodeFromString<CommandResponse>(
            """{"decision":"BLOCK","blocked_reason":"deletion of all data is not a confirmable action"}"""
        )
        assertEquals("deletion of all data is not a confirmable action", bare.displayText)
    }

    @Test
    fun command_response_unverified_effect_is_not_claimed() {
        val resp = json.decodeFromString<CommandResponse>(
            """{"reply":"Done.","actions":[{"ok":true,"tool":"email_send","verification":"UNKNOWN","verified":false}]}"""
        )
        // UNKNOWN is the honest default: the action ran, the effect is not
        // confirmed. hasVerifiedEffect must be false so the UI doesn't show a
        // checkmark for an unverified world effect.
        assertFalse(resp.hasVerifiedEffect)
    }

    @Test
    fun command_response_empty_object_all_null() {
        val resp = json.decodeFromString<CommandResponse>("{}")
        assertNull(resp.reply)
        assertEquals("Done", resp.displayText)
        assertFalse(resp.confirmationRequired)
        assertNull(resp.decision)
        assertTrue(resp.actions.isEmpty())
    }

    // ------------------------------------------------------------------
    // Confirmation — a real row from GET /confirmations
    // (jarvis/security/policy.py `_row_to_confirmation`).
    // ------------------------------------------------------------------

    @Test
    fun confirmation_decodes_real_row() {
        val body = """
        {
          "confirmation_id": "CONF_abc",
          "request_id": "REQ_1",
          "session_id": "SESS_2",
          "tool": "email_send",
          "params": {"to": "boss@corp.com", "subject": "Q3 report"},
          "device": "phone_main",
          "risk": 3,
          "preview": "Send email to boss@corp.com (subject: Q3 report)",
          "state": "pending",
          "created_at": "2026-10-16T09:59:00",
          "resolved_at": null,
          "requested_by_session": "SESS_2",
          "requested_surface": "dashboard"
        }
        """.trimIndent()
        val c = json.decodeFromString<Confirmation>(body)
        assertEquals("CONF_abc", c.id)
        assertEquals("CONF_abc", c.confirmationId)
        assertEquals("email_send", c.action)
        assertEquals("phone_main", c.device)
        assertEquals("destructive", c.riskLevel)
        assertEquals("Send email to boss@corp.com (subject: Q3 report)", c.description)
        assertEquals("boss@corp.com", c.params?.get("to")?.let { it as JsonPrimitive }?.content)
        assertEquals("2026-10-16T09:59:00", c.createdAt)
    }

    @Test
    fun confirmation_risk_tier_maps_to_label() {
        fun level(risk: Int): String =
            json.decodeFromString<Confirmation>("""{"confirmation_id":"x","risk":$risk}""").riskLevel
        assertEquals("read", level(0))
        assertEquals("low", level(1))
        assertEquals("external", level(2))
        assertEquals("destructive", level(3))
        assertEquals("physical", level(4))
    }

    @Test
    fun confirmation_minimal_row() {
        val c = json.decodeFromString<Confirmation>("""{"confirmation_id":"x"}""")
        assertEquals("x", c.id)
        assertEquals("unknown", c.action)
        assertEquals("medium", c.riskLevel)
    }

    @Test
    fun confirmations_list_decodes() {
        val list = json.decodeFromString<List<Confirmation>>(
            """[{"confirmation_id":"a","risk":1},{"confirmation_id":"b","risk":3}]"""
        )
        assertEquals(2, list.size)
        assertEquals("low", list[0].riskLevel)
        assertEquals("destructive", list[1].riskLevel)
    }

    // ------------------------------------------------------------------
    // Device — a real row from GET /devices (the device registry model).
    // There is no `id`, `state`, `device_name` or `surface` on the wire; the
    // model exposes them as computed views over auth_state/type.
    // ------------------------------------------------------------------

    @Test
    fun device_decodes_real_row() {
        val body = """
        {
          "device_id": "phone_main",
          "type": "android",
          "owner": "user",
          "platform": "android",
          "capabilities": ["notifications", "file_transfer", "battery_status"],
          "trust_level": "medium",
          "online": false,
          "connection": ["local_transfer"],
          "metadata": {},
          "fingerprint": "",
          "auth_state": "paired",
          "paired_at": "2026-10-10T12:00:00",
          "last_seen": "2026-10-16T08:00:00",
          "key_ref": "abc",
          "is_host": false
        }
        """.trimIndent()
        val d = json.decodeFromString<DeviceInfo>(body)
        assertEquals("phone_main", d.id)
        assertEquals("phone_main", d.deviceId)
        assertEquals("phone_main", d.displayName)
        assertEquals("paired", d.state)
        assertTrue(d.isPaired)
        assertTrue(d.isPhone)
        assertFalse(d.isHost)
        assertEquals("android", d.type)
        assertEquals("2026-10-10T12:00:00", d.pairedAt)
    }

    @Test
    fun device_host_row() {
        val d = json.decodeFromString<DeviceInfo>(
            """{"device_id":"laptop_main","type":"computer","platform":"macos","online":true,"auth_state":"unpaired","is_host":true}"""
        )
        assertTrue(d.isHost)
        assertFalse(d.isPhone)
        assertEquals("unpaired", d.state)
    }

    // ------------------------------------------------------------------
    // Pairing — POST /devices/pair/request and the gateway redeem.
    // ------------------------------------------------------------------

    @Test
    fun pairing_request_encodes_field_names() {
        val obj = json.parseToJsonElement(
            json.encodeToString(
                PairingRequest.serializer(),
                PairingRequest(deviceId = "phone_z", deviceName = "Z Phone", publicKey = "PEM..."),
            )
        ) as kotlinx.serialization.json.JsonObject
        assertEquals("phone_z", (obj["device_id"] as JsonPrimitive).content)
        assertEquals("Z Phone", (obj["device_name"] as JsonPrimitive).content)
        assertEquals("PEM...", (obj["public_key"] as JsonPrimitive).content)
    }

    @Test
    fun pairing_response_decodes() {
        val r = json.decodeFromString<PairingResponse>(
            """{"pairing_id":"PR_1","device_id":"phone_z","state":"pending","message":"Awaiting approval"}"""
        )
        assertEquals("PR_1", r.pairingId)
        assertEquals("phone_z", r.deviceId)
        assertEquals("pending", r.state)
        assertEquals("Awaiting approval", r.message)
    }

    @Test
    fun pairing_code_response_decodes_gateway_shape() {
        val r = json.decodeFromString<PairingCodeResponse>(
            """{"code":"ab12","certificate_fingerprint":"3DF3 CB13","expires_in_seconds":120,"companion_port":8766}"""
        )
        assertEquals("ab12", r.code)
        assertEquals("3DF3 CB13", r.certificateFingerprint)
        assertEquals(120, r.expiresInSeconds)
        assertEquals(8766, r.companionPort)
    }

    // ------------------------------------------------------------------
    // Emergency stop — real POST /emergency_stop shape.
    // ------------------------------------------------------------------

    @Test
    fun emergency_stop_decodes_real_shape() {
        val body = """
        {
          "ok": true,
          "verified": true,
          "result": {
            "message": "No JARVIS job was in flight, so there was nothing here to cancel. Physical actuators were NOT stopped by this command.",
            "verified": true,
            "jobs_cancelled": 0,
            "cancelled_job_ids": [],
            "jobs_stuck": [],
            "physical_stop": "not_available"
          }
        }
        """.trimIndent()
        val r = json.decodeFromString<EmergencyStopResponse>(body)
        assertEquals(true, r.ok)
        assertEquals(0, r.result?.jobsCancelled)
        assertTrue(r.detail?.contains("No JARVIS job") == true)
        // The honest capability boundary must be visible to the UI.
        assertFalse(r.physicalStopAvailable)
    }
    // ------------------------------------------------------------------
    // Confirmation — the EXACT row a live GET /confirmations returned on
    // 2026-10-21, decoded with the model's own decoder.
    //
    // The macOS client's Confirmation model named `id`, `action`,
    // `description`, `device_id` and `expires_at` -- none of which the server
    // has ever sent -- so decoding a real approval failed and the Approvals
    // screen showed an error instead of the approvals. It sat there for
    // years, because the contract was described rather than exercised.
    //
    // This is the row, verbatim.
    // ------------------------------------------------------------------

    @Test
    fun confirmation_decodes_the_exact_live_row() {
        val body = """
        {
          "confirmation_id": "CONF-71242e4cf93bd579e32b646d9156eed7",
          "request_id": "",
          "session_id": "AND-CHK",
          "tool": "create_event",
          "params": {"title": "called Android Check", "start": "2026-12-08T12:00:00+05:30"},
          "device": "laptop_main",
          "risk": 2,
          "preview": "create_event on laptop_main | title=called Android Check | start=2026-12-08T12:00:00+05:30",
          "state": "pending",
          "created_at": "2026-10-21T18:50:02.884453+00:00",
          "resolved_at": null,
          "requested_by_session": "AND-CHK",
          "approved_by_session": "",
          "requested_surface": "live",
          "approved_surface": ""
        }
        """.trimIndent()
        val c = json.decodeFromString<Confirmation>(body)
        assertEquals("CONF-71242e4cf93bd579e32b646d9156eed7", c.id)
        assertEquals("create_event", c.action)
        assertEquals("laptop_main", c.device)
        assertEquals("external", c.riskLevel)
        assertEquals("pending", c.state)
        assertEquals("live", c.requestedSurface)
        assertEquals("2026-10-21T18:50:02.884453+00:00", c.createdAt)
        // The preview is the server's own one-liner, and it is what the card
        // shows the person.
        assertTrue(c.description.contains("create_event on laptop_main"))
        assertTrue(c.description.contains("Android Check"))
    }

    @Test
    fun confirmations_list_decodes_the_exact_live_response() {
        val body = """
        [
          {
            "confirmation_id": "CONF-71242e4cf93bd579e32b646d9156eed7",
            "request_id": "",
            "session_id": "AND-CHK",
            "tool": "create_event",
            "params": {"title": "called Android Check"},
            "device": "laptop_main",
            "risk": 2,
            "preview": "create_event on laptop_main | title=called Android Check",
            "state": "pending",
            "created_at": "2026-10-21T18:50:02.884453+00:00",
            "resolved_at": null,
            "requested_by_session": "AND-CHK",
            "approved_by_session": "",
            "requested_surface": "live",
            "approved_surface": ""
          }
        ]
        """.trimIndent()
        val list = json.decodeFromString<List<Confirmation>>(body)
        assertEquals(1, list.size)
        assertEquals("create_event", list[0].action)
    }

}
