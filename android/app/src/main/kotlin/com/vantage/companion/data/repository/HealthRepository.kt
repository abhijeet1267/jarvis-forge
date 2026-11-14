package com.vantage.companion.data.repository

import com.vantage.companion.data.api.HealthResponse
import com.vantage.companion.data.api.VantageApiClient

class HealthRepository(private val api: VantageApiClient) {
    suspend fun fetchHealth(): HealthResponse = api.fetchHealth()
}