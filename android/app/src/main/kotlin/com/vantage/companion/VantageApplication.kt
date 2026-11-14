package com.vantage.companion

import android.app.Application
import com.vantage.companion.data.api.VantageApiClient
import com.vantage.companion.data.local.SessionStore
import com.vantage.companion.data.voice.VoiceSettings
import com.vantage.companion.voice.AndroidTextToSpeechEngine
import com.vantage.companion.voice.VoiceController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

class VantageApplication : Application() {
    lateinit var sessionStore: SessionStore
        private set
    lateinit var apiClient: VantageApiClient
        private set

    // App-scoped so the voice preference (and its reaction to being toggled
    // off, which must stop speech immediately) survives screen changes.
    val appScope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    lateinit var voiceSettings: VoiceSettings
        private set
    lateinit var voiceController: VoiceController
        private set

    override fun onCreate() {
        super.onCreate()
        instance = this
        sessionStore = SessionStore.create(this)
        apiClient = VantageApiClient(sessionStore)
        voiceSettings = VoiceSettings.create(this)
        voiceController = VoiceController(
            settings = voiceSettings,
            engine = AndroidTextToSpeechEngine(this),
            scope = appScope,
        )
    }

    companion object {
        lateinit var instance: VantageApplication
            private set
    }
}