package com.vantage.companion.voice

import com.vantage.companion.data.api.CommandResponse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * "Should this reply be spoken?" — pure policy, no Android types. Speaks only
 * a real assistant answer with voice ON and a READY engine; never an error
 * (null), a blocked decision, or a confirmation request.
 */
class VoiceSpeechPolicyTest {

    private fun answer(
        reply: String? = "Your system is healthy.",
        decision: String? = "ALLOW",
        needsConfirmation: Boolean = false,
        blockedReason: String? = null,
    ) = CommandResponse(
        reply = reply,
        decision = decision,
        needsConfirmation = needsConfirmation,
        blockedReason = blockedReason,
    )

    @Test
    fun speaks_a_normal_assistant_reply() {
        val text = VoiceSpeechPolicy.utteranceFor(
            enabled = true,
            availability = VoiceAvailability.READY,
            response = answer(),
        )
        assertEquals("Your system is healthy.", text)
    }

    @Test
    fun silent_when_voice_is_off() {
        assertNull(
            VoiceSpeechPolicy.utteranceFor(
                enabled = false,
                availability = VoiceAvailability.READY,
                response = answer(),
            )
        )
    }

    @Test
    fun silent_when_engine_is_unavailable() {
        assertNull(
            VoiceSpeechPolicy.utteranceFor(
                enabled = true,
                availability = VoiceAvailability.UNAVAILABLE,
                response = answer(),
            )
        )
    }

    @Test
    fun silent_before_the_engine_is_ready() {
        assertNull(
            VoiceSpeechPolicy.utteranceFor(
                enabled = true,
                availability = VoiceAvailability.NOT_INITIALIZED,
                response = answer(),
            )
        )
    }

    @Test
    fun silent_for_an_error() {
        assertNull(
            VoiceSpeechPolicy.utteranceFor(
                enabled = true,
                availability = VoiceAvailability.READY,
                response = null,
            )
        )
    }

    @Test
    fun silent_for_a_blocked_decision() {
        assertNull(
            VoiceSpeechPolicy.utteranceFor(
                enabled = true,
                availability = VoiceAvailability.READY,
                response = answer(decision = "BLOCK", blockedReason = "Not allowed"),
            )
        )
    }

    @Test
    fun silent_for_a_confirmation_request() {
        assertNull(
            VoiceSpeechPolicy.utteranceFor(
                enabled = true,
                availability = VoiceAvailability.READY,
                response = answer(
                    reply = "Approve sending that email?",
                    decision = "CONFIRM",
                    needsConfirmation = true,
                ),
            )
        )
    }

    @Test
    fun silent_when_the_reply_is_blank() {
        assertNull(
            VoiceSpeechPolicy.utteranceFor(
                enabled = true,
                availability = VoiceAvailability.READY,
                response = answer(reply = "   "),
            )
        )
    }

    @Test
    fun the_spoken_text_is_trimmed() {
        assertEquals(
            "Done.",
            VoiceSpeechPolicy.utteranceFor(
                enabled = true,
                availability = VoiceAvailability.READY,
                response = answer(reply = "  Done.  "),
            ),
        )
    }
}