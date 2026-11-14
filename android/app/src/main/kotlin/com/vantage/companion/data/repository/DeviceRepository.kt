package com.vantage.companion.data.repository

import com.vantage.companion.data.api.DeviceInfo
import com.vantage.companion.data.api.PairingResponse
import com.vantage.companion.data.api.VantageApiClient

class DeviceRepository(private val api: VantageApiClient) {
    suspend fun fetchDevices(): List<DeviceInfo> = api.fetchDevices()
    suspend fun requestPairing(deviceId: String, deviceName: String): PairingResponse =
        api.requestPairing(deviceId, deviceName)
}