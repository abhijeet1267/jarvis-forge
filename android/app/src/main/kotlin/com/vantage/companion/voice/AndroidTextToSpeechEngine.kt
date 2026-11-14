package com.vantage.companion.voice

import android.content.Context
import android.speech.tts.TextToSpeech
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.Locale

/**
 * [SpeechEngine] over `android.speech.tts.TextToSpeech`.
 *
 * Deliberately lazy: the platform engine is only created on [initialize],
 * which the controller calls when the user turns voice on (or when a reply is
 * about to be spoken). Speaking text needs no microphone permission, so this
 * class requests none.
 *
 * Failure is terminal for the process: if the engine fails to initialize (or
 * has no usable language), the state becomes [VoiceAvailability.UNAVAILABLE]
 * and further [initialize] calls are no-ops — the app reports the problem
 * instead of retrying in a loop or pretending it works.
 */
class AndroidTextToSpeechEngine(context: Context) : SpeechEngine {

    private val appContext = context.applicationContext

    private val _availability = MutableStateFlow(VoiceAvailability.NOT_INITIALIZED)
    override val availability: StateFlow<VoiceAvailability> = _availability.asStateFlow()

    private var tts: TextToSpeech? = null

    /** One attempt per process unless a working engine was explicitly shut down. */
    private var initAttempted = false

    @Synchronized
    override fun initialize() {
        if (initAttempted) return
        initAttempted = true
        try {
            tts = TextToSpeech(appContext) { status ->
                if (status != TextToSpeech.SUCCESS) {
                    _availability.value = VoiceAvailability.UNAVAILABLE
                } else {
                    val language = tts?.setLanguage(Locale.getDefault())
                    _availability.value =
                        if (language == TextToSpeech.LANG_MISSING_DATA ||
                            language == TextToSpeech.LANG_NOT_SUPPORTED
                        ) {
                            VoiceAvailability.UNAVAILABLE
                        } else {
                            VoiceAvailability.READY
                        }
                }
            }
        } catch (_: Exception) {
            // Some devices throw from the constructor when no TTS service is
            // installed. That is "unavailable", not a crash.
            tts = null
            _availability.value = VoiceAvailability.UNAVAILABLE
        }
    }

    override fun speak(text: String, utteranceId: String): Boolean {
        if (_availability.value != VoiceAvailability.READY) {
            // First use before an explicit initialize: start the engine, but
            // this utterance is not spoken (the engine is not ready yet).
            initialize()
            return false
        }
        val engine = tts ?: return false
        // QUEUE_FLUSH: a newer reply replaces the one still playing, so two
        // rapid responses never overlap.
        return engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, utteranceId) ==
            TextToSpeech.SUCCESS
    }

    override fun stop() {
        tts?.stop()
    }

    @Synchronized
    override fun shutdown() {
        val engine = tts
        if (engine != null) {
            runCatching { engine.stop() }
            runCatching { engine.shutdown() }
            tts = null
        }
        // Only a working engine is released; a failed init stays failed so a
        // later call cannot retry it.
        if (_availability.value == VoiceAvailability.READY) {
            _availability.value = VoiceAvailability.NOT_INITIALIZED
            initAttempted = false
        }
    }
}