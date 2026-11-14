package com.vantage.companion.voice

import com.vantage.companion.data.api.CommandResponse
import com.vantage.companion.data.voice.VoiceSettings
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * Binds the persisted preference to the speech engine (R: Android AI Voice).
 *
 * The decisions themselves — whether an utterance is spoken at all — live in
 * [VoiceSpeechPolicy], which is pure. This class only enforces the lifecycle:
 *
 *  - enabling the preference initializes the engine (lazy: no TTS object is
 *    created until the user opts in),
 *  - disabling it stops any utterance immediately,
 *  - lifecycle pause/stop stop speech,
 *  - [release] shuts the engine down.
 *
 * Duplicate suppression: an utterance id (a response's request id when the
 * backend supplies one) is spoken at most once until voice is toggled off, so
 * a repeated callback or a retried request that returns the same id does not
 * speak twice. The engine itself also flushes the queue, so two different
 * replies cannot overlap.
 *
 * @param scope application-scoped coroutine context that observes the
 * preference; tests pass a test scope, ViewModels pass none and rely on the
 * app singleton's scope.
 */
class VoiceController(
    private val settings: VoiceSettings,
    private val engine: SpeechEngine,
    scope: CoroutineScope? = null,
) {

    val enabled: StateFlow<Boolean> get() = settings.enabled
    val availability: StateFlow<VoiceAvailability> get() = engine.availability

    /** The last utterance id actually spoken; reset whenever voice turns off. */
    private var lastUtteranceId: String? = null

    init {
        if (scope != null) {
            scope.launch {
                settings.enabled.collect { enabled -> applyEnabled(enabled) }
            }
        }
    }

    fun setEnabled(enabled: Boolean) = settings.setEnabled(enabled)

    fun toggle() = setEnabled(!settings.isEnabled())

    /** Speak [response]'s reply when the policy allows it. Returns what was spoken, if anything. */
    fun announce(response: CommandResponse?, utteranceId: String? = null): String? {
        val enabledNow = settings.isEnabled()
        if (enabledNow && engine.availability.value == VoiceAvailability.NOT_INITIALIZED) {
            // A reply arrived before the engine had been asked for: start it
            // now so the next reply can be spoken, but never block on it.
            engine.initialize()
        }
        val text = VoiceSpeechPolicy.utteranceFor(
            enabled = enabledNow,
            availability = engine.availability.value,
            response = response,
        ) ?: return null
        val id = utteranceId?.takeIf { it.isNotBlank() }
            ?: response?.requestId?.takeIf { it.isNotBlank() }
            ?: text
        if (id == lastUtteranceId) return null
        return if (engine.speak(text, id)) {
            lastUtteranceId = id
            text
        } else {
            null
        }
    }

    /** Lifecycle pause: speech must not keep playing behind a hidden screen. */
    fun onPause() {
        engine.stop()
    }

    fun onStop() {
        engine.stop()
    }

    /** Re-apply the persisted preference (e.g. after the activity was recreated). */
    fun rearm() = applyEnabled(settings.isEnabled())

    /** Release the platform engine (activity destroyed / VM cleared). */
    fun release() {
        engine.shutdown()
    }

    private fun applyEnabled(enabled: Boolean) {
        if (enabled) {
            engine.initialize()
        } else {
            engine.stop()
            lastUtteranceId = null
        }
    }
}