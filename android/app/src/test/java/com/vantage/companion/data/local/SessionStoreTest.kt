package com.vantage.companion.data.local

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * SessionStore behavior on the JVM (in-memory preference backends). Pins the
 * security-relevant behavior: the session token is the credential and must be
 * cleared on sign-out / 401; the fingerprint is identity, not secret.
 */
class SessionStoreTest {

    private fun store() = SessionStore(InMemorySharedPreferences(), InMemorySharedPreferences())

    @Test
    fun starts_with_no_session() {
        val store = store()
        assertNull(store.sessionToken())
        assertFalse(store.hasSession.value)
    }

    @Test
    fun save_session_makes_has_session_true() {
        val store = store()
        store.saveSession("CMP_abc", "phone")
        assertEquals("CMP_abc", store.sessionToken())
        assertTrue(store.hasSession.value)
        assertEquals("phone", store.surface())
    }

    @Test
    fun sign_out_clears_the_token_and_surface() {
        val store = store()
        store.saveSession("CMP_abc", "phone")
        store.saveCertFingerprint("3DF3 CB13")
        store.signOut()
        assertNull(store.sessionToken())
        assertFalse(store.hasSession.value)
        assertEquals("", store.surface())
    }

    @Test
    fun clear_all_wipes_everything() {
        val store = store()
        store.saveSession("CMP_abc", "phone")
        store.saveBackendURL("https://192.168.1.50:8766")
        store.saveDeviceIdentity("phone_1", "Test")
        store.saveCertFingerprint("3DF3 CB13")
        store.clearAll()
        assertNull(store.sessionToken())
        assertEquals("", store.backendUrl.value)
        assertEquals("", store.deviceId.value)
        assertNull(store.certFingerprint())
    }

    @Test
    fun fingerprint_round_trips_and_blank_is_absent() {
        val store = store()
        assertNull(store.certFingerprint())
        store.saveCertFingerprint("3DF3 CB13 C0F3")
        assertEquals("3DF3 CB13 C0F3", store.certFingerprint())
    }

    @Test
    fun device_identity_is_stable_until_saved_again() {
        val store = store()
        store.saveDeviceIdentity("phone_1", "Pixel")
        assertEquals("phone_1", store.deviceId.value)
        assertEquals("Pixel", store.deviceName())
    }
}
