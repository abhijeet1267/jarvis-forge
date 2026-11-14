package com.vantage.companion.data.local

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKeys
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class SessionStore(
    private val encryptedPrefs: SharedPreferences,
    private val plainPrefs: SharedPreferences,
) {

    companion object {
        private const val ENC_PREFS = "vantage_session_encrypted"
        private const val PLAIN_PREFS = "vantage_prefs"

        private const val KEY_SESSION_TOKEN = "session_token"
        private const val KEY_SURFACE = "surface"
        private const val KEY_BACKEND_URL = "backend_url"
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_DEVICE_NAME = "device_name"
        private const val KEY_PAIRING_ID = "pairing_id"
        private const val KEY_CERT_FINGERPRINT = "cert_fingerprint"

        /**
         * Production path: build the two preference stores from a [Context].
         * The token goes into Keystore-backed EncryptedSharedPreferences;
         * everything non-sensitive goes into the plain store. Tests construct
         * the primary constructor directly with in-memory fakes (they have no
         * [Context] and no Android Keystore).
         */
        fun create(context: Context): SessionStore {
            val masterKey = MasterKeys.getOrCreate(MasterKeys.AES256_GCM_SPEC)
            val encrypted = EncryptedSharedPreferences.create(
                ENC_PREFS,
                masterKey,
                context,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
            )
            val plain = context.getSharedPreferences(PLAIN_PREFS, Context.MODE_PRIVATE)
            return SessionStore(encrypted, plain)
        }
    }

    // ── Observable state ──

    private val _hasSession = MutableStateFlow(false)
    val hasSession: StateFlow<Boolean> = _hasSession.asStateFlow()

    private val _backendUrl = MutableStateFlow("")
    val backendUrl: StateFlow<String> = _backendUrl.asStateFlow()

    private val _deviceId = MutableStateFlow("")
    val deviceId: StateFlow<String> = _deviceId.asStateFlow()

    init {
        reload()
    }

    // ── Token ──

    fun sessionToken(): String? = encryptedPrefs.getString(KEY_SESSION_TOKEN, null)

    fun saveSession(token: String, surface: String) {
        encryptedPrefs.edit().putString(KEY_SESSION_TOKEN, token).apply()
        plainPrefs.edit().putString(KEY_SURFACE, surface).apply()
        _hasSession.value = true
    }

    // ── Backend URL ──

    fun saveBackendURL(url: String) {
        plainPrefs.edit().putString(KEY_BACKEND_URL, url).apply()
        _backendUrl.value = url
    }

    // ── Pinned certificate fingerprint ──
    //
    // The gateway's X.509 SHA-256 fingerprint, entered out-of-band (QR/manual)
    // during pairing. Stored plain: it is an identity, not a secret — the
    // secret is the session token, which stays in the encrypted store.

    fun certFingerprint(): String? =
        plainPrefs.getString(KEY_CERT_FINGERPRINT, null)?.takeIf { it.isNotBlank() }

    fun saveCertFingerprint(fingerprint: String) {
        plainPrefs.edit().putString(KEY_CERT_FINGERPRINT, fingerprint).apply()
    }

    // ── Device identity ──

    fun saveDeviceIdentity(id: String, name: String) {
        plainPrefs.edit()
            .putString(KEY_DEVICE_ID, id)
            .putString(KEY_DEVICE_NAME, name)
            .apply()
        _deviceId.value = id
    }

    fun deviceName(): String =
        plainPrefs.getString(KEY_DEVICE_NAME, "") ?: ""

    /** The surface this session was issued on ("phone" via the companion gateway). */
    fun surface(): String =
        plainPrefs.getString(KEY_SURFACE, "") ?: ""

    fun pairingId(): String? =
        plainPrefs.getString(KEY_PAIRING_ID, null)

    fun savePairingId(id: String) {
        plainPrefs.edit().putString(KEY_PAIRING_ID, id).apply()
    }

    // ── Lifecycle ──

    fun signOut() {
        encryptedPrefs.edit().remove(KEY_SESSION_TOKEN).apply()
        plainPrefs.edit()
            .remove(KEY_SURFACE)
            .remove(KEY_PAIRING_ID)
            .apply()
        _hasSession.value = false
    }

    fun clearAll() {
        encryptedPrefs.edit().clear().apply()
        plainPrefs.edit().clear().apply()
        _hasSession.value = false
        _backendUrl.value = ""
        _deviceId.value = ""
    }

    private fun reload() {
        _hasSession.value = sessionToken() != null
        _backendUrl.value = plainPrefs.getString(KEY_BACKEND_URL, "") ?: ""
        _deviceId.value = plainPrefs.getString(KEY_DEVICE_ID, "") ?: ""
    }
}