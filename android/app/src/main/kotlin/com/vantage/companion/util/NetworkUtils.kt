package com.vantage.companion.util

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities

object NetworkUtils {
    fun isWifiConnected(context: Context): Boolean {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val network = cm.activeNetwork ?: return false
        val caps = cm.getNetworkCapabilities(network) ?: return false
        return caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
    }

    fun isValidHost(host: String): Boolean {
        val trimmed = host.trim()
        if (trimmed.isEmpty()) return false
        val hostnameRegex = Regex(
            """^([a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?\.)*[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?:\d{1,5}$"""
        )
        val ipRegex = Regex("""^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}$""")
        return hostnameRegex.matches(trimmed) || ipRegex.matches(trimmed)
    }
}