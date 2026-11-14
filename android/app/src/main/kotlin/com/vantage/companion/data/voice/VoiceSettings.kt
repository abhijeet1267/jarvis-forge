package com.vantage.companion.data.voice

import android.content.Context
import android.content.SharedPreferences
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The persisted "AI Voice Responses" preference.
 *
 * A voice on/off choice is not a credential, so it lives in the PLAIN
 * preference store (the same `vantage_prefs` file [SessionStore] keeps its
 * non-secret state in) — never in the Keystore-backed encrypted store.
 *
 * The constructor takes a [SharedPreferences] so JVM unit tests can drive it
 * with an in-memory fake; [create] is the production path from a [Context].
 */
class VoiceSettings(private val prefs: SharedPreferences) {

    companion object {
        /** The plain preference file SessionStore also writes non-secrets to. */
        const val PLAIN_PREFS = "vantage_prefs"
        const val KEY_ENABLED = "ai_voice_enabled"

        fun create(context: Context): VoiceSettings =
            VoiceSettings(context.getSharedPreferences(PLAIN_PREFS, Context.MODE_PRIVATE))
    }

    // Default OFF: the app stays silent until the user explicitly opts in.
    private val _enabled = MutableStateFlow(prefs.getBoolean(KEY_ENABLED, false))
    val enabled: StateFlow<Boolean> = _enabled.asStateFlow()

    fun isEnabled(): Boolean = _enabled.value

    fun setEnabled(enabled: Boolean) {
        prefs.edit().putBoolean(KEY_ENABLED, enabled).apply()
        _enabled.value = enabled
    }

    fun toggle() = setEnabled(!_enabled.value)
}