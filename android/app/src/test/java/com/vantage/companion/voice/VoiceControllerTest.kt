package com.vantage.companion.voice

import com.vantage.companion.data.local.InMemorySharedPreferences
import com.vantage.companion.data.voice.VoiceSettings
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [VoiceController] against a fake engine. Proves what the user can rely on:
 * OFF speaks nothing, turning OFF mid-utterance stops it immediately, an
 * unavailable engine never speaks, the same reply is not spoken twice, and
 * release shuts the engine down.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class VoiceControllerTest {

    /** Fake [SpeechEngine]; no android.speech.tts anywhere in JVM tests. */
    private class FakeEngine : SpeechEngine {
        private val _availability = MutableStateFlow(VoiceAvailability.NOT_INITIALIZED)
        override val availability: StateFlow<VoiceAvailability> = _availability.asStateFlow()

        val spoken = mutableListOf<Pair<String, String>>()
        var stopCalls = 0
        var initCalls = 0
        var shutdownCalls = 0

        var failInit = false
        var acceptSpeech = true

        override fun initialize() {
            initCalls++
            _availability.value =
                if (failInit) VoiceAvailability.UNAVAILABLE else VoiceAvailability.READY
        }

        override fun speak(text: String, utteranceId: String): Boolean {
            if (_availability.value != VoiceAvailability.READY || !acceptSpeech) return false
            spoken += text to utteranceId
            return true
        }

        override fun stop() { stopCalls++ }
        override fun shutdown() {
            shutdownCalls++
            _availability.value = VoiceAvailability.NOT_INITIALIZED
        }
    }

    private val prefs = InMemorySharedPreferences()
    private val settings = VoiceSettings(prefs)
    private val engine = FakeEngine()

    private fun controller(scope: CoroutineScope?) = VoiceController(settings, engine, scope)

    private fun normalReply(id: String? = "REQ_1") = com.vantage.companion.data.api.CommandResponse(
        reply = "Your system is healthy.",
        decision = "ALLOW",
        requestId = id,
    )

    @Test
    fun does_not_speak_when_off() {
        val c = controller(null)
        assertNull(c.announce(normalReply()))
        assertTrue(engine.spoken.isEmpty())
    }

    @Test
    fun speaks_when_on_and_engine_ready() {
        val c = controller(null)
        engine.initialize()
        c.setEnabled(true)
        val spokenText = c.announce(normalReply())
        assertEquals("Your system is healthy.", spokenText)
        assertEquals(1, engine.spoken.size)
        assertEquals("REQ_1", engine.spoken[0].second)
    }

    @Test
    fun the_same_reply_is_not_spoken_twice() {
        val c = controller(null)
        engine.initialize()
        c.setEnabled(true)
        c.announce(normalReply("REQ_1"))
        c.announce(normalReply("REQ_1"))
        assertEquals(1, engine.spoken.size)
    }

    @Test
    fun a_new_reply_is_spoken() {
        val c = controller(null)
        engine.initialize()
        c.setEnabled(true)
        c.announce(normalReply("REQ_1"))
        c.announce(normalReply("REQ_2"))
        assertEquals(2, engine.spoken.size)
    }

    @Test
    fun turning_off_stops_speech_immediately() {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val c = controller(scope)
        c.setEnabled(true)
        engine.initialize()
        c.announce(normalReply())
        assertTrue(engine.spoken.isNotEmpty())

        val before = engine.stopCalls
        c.setEnabled(false)
        assertTrue("stop must be called when voice is disabled", engine.stopCalls > before)
    }

    @Test
    fun turning_off_resets_duplicate_guard() {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val c = controller(scope)
        c.setEnabled(true)
        engine.initialize()
        c.announce(normalReply("REQ_1"))
        c.setEnabled(false)
        c.setEnabled(true)
        c.announce(normalReply("REQ_1"))
        assertEquals(2, engine.spoken.size)
    }

    @Test
    fun does_not_speak_when_engine_is_unavailable() {
        engine.failInit = true
        val c = controller(null)
        c.setEnabled(true)
        assertNull(c.announce(normalReply()))
        assertTrue(engine.spoken.isEmpty())
        assertEquals(VoiceAvailability.UNAVAILABLE, c.availability.value)
    }

    @Test
    fun does_not_speak_blocked_or_confirmation_replies() {
        engine.initialize()
        val c = controller(null)
        c.setEnabled(true)
        assertNull(c.announce(
            com.vantage.companion.data.api.CommandResponse(
                reply = "Nope", decision = "BLOCK", requestId = "REQ_B",
            )
        ))
        assertNull(c.announce(
            com.vantage.companion.data.api.CommandResponse(
                reply = "Approve?",
                decision = "CONFIRM",
                needsConfirmation = true,
                requestId = "REQ_C",
            )
        ))
        assertTrue(engine.spoken.isEmpty())
    }

    @Test
    fun an_error_is_never_spoken() {
        engine.initialize()
        val c = controller(null)
        c.setEnabled(true)
        assertNull(c.announce(null))
        assertTrue(engine.spoken.isEmpty())
    }

    @Test
    fun enabling_initializes_the_engine_lazily() {
        val c = controller(null)
        assertEquals(0, engine.initCalls)
        c.setEnabled(true)
        c.rearm()
        assertEquals(1, engine.initCalls)
    }

    @Test
    fun pause_and_stop_silence_speech() {
        engine.initialize()
        val c = controller(null)
        c.setEnabled(true)
        c.onPause()
        c.onStop()
        assertEquals(2, engine.stopCalls)
    }

    @Test
    fun release_shuts_the_engine_down() {
        val c = controller(null)
        engine.initialize()
        c.setEnabled(true)
        c.release()
        assertEquals(1, engine.shutdownCalls)
        assertTrue(engine.spoken.isEmpty())
    }
}