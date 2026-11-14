package com.vantage.companion.data.voice

import com.vantage.companion.data.local.InMemorySharedPreferences
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The persisted AI Voice preference. It lives in the plain preference store
 * (it is not a credential) and must default to OFF, and a toggle must survive
 * a fresh instance over the same storage (process restart).
 */
class VoiceSettingsTest {

    private val prefs = InMemorySharedPreferences()

    @Test
    fun defaults_to_off() {
        val settings = VoiceSettings(prefs)
        assertFalse(settings.isEnabled())
        assertFalse(settings.enabled.value)
    }

    @Test
    fun enabling_persists_across_instances() {
        VoiceSettings(prefs).setEnabled(true)
        assertTrue(VoiceSettings(prefs).isEnabled())
    }

    @Test
    fun disabling_persists_across_instances() {
        val settings = VoiceSettings(prefs)
        settings.setEnabled(true)
        settings.setEnabled(false)
        assertFalse(settings.isEnabled())
        assertFalse(VoiceSettings(prefs).isEnabled())
    }

    @Test
    fun toggle_flips_the_state_flow() {
        val settings = VoiceSettings(prefs)
        settings.toggle()
        assertTrue(settings.enabled.value)
        settings.toggle()
        assertFalse(settings.enabled.value)
    }

    @Test
    fun stored_under_the_plain_preference_key() {
        VoiceSettings(prefs).setEnabled(true)
        assertTrue(prefs.getBoolean(VoiceSettings.KEY_ENABLED, false))
        assertTrue(VoiceSettings.PLAIN_PREFS == "vantage_prefs")
    }
}