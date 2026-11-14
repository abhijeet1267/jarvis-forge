package com.vantage.companion

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.compose.ui.Modifier
import androidx.lifecycle.ViewModelProvider
import com.vantage.companion.ui.navigation.AppNavigation
import com.vantage.companion.ui.setup.SetupScreen
import com.vantage.companion.ui.setup.SetupViewModel
import com.vantage.companion.ui.theme.Background
import com.vantage.companion.ui.theme.VantageTheme
import com.vantage.companion.voice.VoiceController

class MainActivity : ComponentActivity() {

    private lateinit var setupViewModel: SetupViewModel
    private lateinit var voiceController: VoiceController

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val app = application as VantageApplication
        val sessionStore = app.sessionStore
        val apiClient = app.apiClient
        voiceController = app.voiceController
        // A recreated activity (rotation) finds the shared controller whose
        // engine was released in onDestroy; re-arm it if voice is still on.
        voiceController.rearm()

        setupViewModel = ViewModelProvider(
            this,
            SetupViewModel.Factory(apiClient, sessionStore),
        )[SetupViewModel::class.java]

        handleIntent(intent)

        setContent {
            VantageTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = Background,
                ) {
                    val hasSession by sessionStore.hasSession
                        .collectAsStateWithLifecycle()

                    if (hasSession) {
                        AppNavigation(
                            apiClient = apiClient,
                            sessionStore = sessionStore,
                            voiceController = voiceController,
                        )
                    } else {
                        SetupScreen(viewModel = setupViewModel)
                    }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    // Speech must not keep playing behind a backgrounded app, and the
    // platform engine is released with the activity so it cannot leak.
    override fun onPause() {
        voiceController.onPause()
        super.onPause()
    }

    override fun onStop() {
        voiceController.onStop()
        super.onStop()
    }

    override fun onDestroy() {
        voiceController.release()
        super.onDestroy()
    }

    /**
     * Pairing deep link from the Mac's QR code:
     * `vantage://pair?host=192.168.1.50&port=8766&fingerprint=3DF3…&code=ab12…`
     * The VM pre-fills the fields and, when a code is present, starts the
     * pinned-TLS redeem automatically.
     */
    private fun handleIntent(intent: Intent?) {
        if (intent?.action != Intent.ACTION_VIEW) return
        val uri: Uri = intent.data ?: return
        if (uri.scheme != "vantage" || uri.host != "pair") return
        setupViewModel.applyDeepLink(
            host = uri.getQueryParameter("host") ?: "",
            port = uri.getQueryParameter("port")?.toIntOrNull(),
            fingerprint = uri.getQueryParameter("fingerprint"),
            code = uri.getQueryParameter("code"),
        )
    }
}
