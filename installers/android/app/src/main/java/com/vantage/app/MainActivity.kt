package com.vantage.app

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.KeyEvent
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewAssetLoader.AssetsPathHandler

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var serverUrl: String = "http://192.168.1.1:8765"

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webview)

        // Try to auto-detect server from saved preferences, or use default
        val prefs = getSharedPreferences("vantage", MODE_PRIVATE)
        serverUrl = prefs.getString("server_url", "http://192.168.1.1:8765") ?: serverUrl

        setupWebView()
        loadVantage()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val settings: WebSettings = webView.settings
        settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            // Cache the PWA assets
            cacheMode = WebSettings.LOAD_DEFAULT
            setSupportZoom(true)
            builtInZoomControls = true
            displayZoomControls = false
            useWideViewPort = true
            loadWithOverviewMode = true
        }

        // Accept all cookies for session management
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true)

        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedError(
                view: WebView,
                errorCode: Int,
                description: String,
                failingUrl: String
            ) {
                if (errorCode == ERROR_HOST_LOOKUP || errorCode == ERROR_CONNECT) {
                    showConnectionError()
                }
            }

            override fun shouldOverrideUrlLoading(
                view: WebView,
                request: WebResourceRequest
            ): Boolean {
                // Keep all navigation in the WebView
                return false
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onReceivedTitle(view: WebView, title: String) {
                supportActionBar?.title = title
            }
        }
    }

    private fun loadVantage() {
        webView.loadUrl("$serverUrl/phone")
    }

    private fun showConnectionError() {
        runOnUiThread {
            Toast.makeText(
                this,
                "Cannot reach Vantage at $serverUrl\nMake sure Vantage is running on your computer and both devices are on the same Wi-Fi network.",
                Toast.LENGTH_LONG
            ).show()
        }
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView.canGoBack()) {
            webView.goBack()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }
}