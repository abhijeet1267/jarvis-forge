package com.vantage.companion.voice

import kotlinx.coroutines.flow.StateFlow

/**
 * How usable the speech engine is right now.
 *
 * [NOT_INITIALIZED] is the honest starting state: nothing has been asked of
 * the engine yet, so neither "ready" nor "unavailable" is true. The UI only
 * claims a device problem on [UNAVAILABLE], which is set after a real init
 * failure.
 */
enum class VoiceAvailability { NOT_INITIALIZED, READY, UNAVAILABLE }

/**
 * The thin seam between the speech policy and `android.speech.tts`.
 *
 * Unit tests implement this with a fake — `TextToSpeech` is a framework class
 * that cannot be instantiated on a plain JVM.
 */
interface SpeechEngine {

    val availability: StateFlow<VoiceAvailability>

    /** Start the platform engine if it has not been started before. */
    fun initialize()

    /**
     * Speak [text], replacing anything currently playing (`QUEUE_FLUSH`
     * semantics in the real engine), tagged with the stable [utteranceId].
     * Returns true when the utterance was accepted.
     */
    fun speak(text: String, utteranceId: String): Boolean

    /** Stop any utterance immediately; the engine stays initialized. */
    fun stop()

    /** Release the platform engine. A later [speak] may initialize it again. */
    fun shutdown()
}