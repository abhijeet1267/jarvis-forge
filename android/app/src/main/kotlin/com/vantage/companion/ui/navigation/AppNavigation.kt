package com.vantage.companion.ui.navigation

import androidx.compose.animation.*
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.vantage.companion.data.api.VantageApiClient
import com.vantage.companion.data.local.SessionStore
import com.vantage.companion.ui.approvals.ApprovalsScreen
import com.vantage.companion.ui.approvals.ApprovalsViewModel
import com.vantage.companion.ui.command.CommandScreen
import com.vantage.companion.ui.command.CommandViewModel
import com.vantage.companion.ui.devices.DevicesScreen
import com.vantage.companion.ui.devices.DevicesViewModel
import com.vantage.companion.ui.google.GoogleScreen
import com.vantage.companion.ui.google.GoogleViewModel
import com.vantage.companion.ui.home.HomeScreen
import com.vantage.companion.ui.home.HomeViewModel
import com.vantage.companion.ui.settings.SettingsScreen
import com.vantage.companion.ui.settings.SettingsViewModel
import com.vantage.companion.ui.components.VantageTopBar
import com.vantage.companion.ui.theme.*
import com.vantage.companion.voice.VoiceController

enum class NavTab(val label: String, val icon: ImageVector) {
    HOME("Home", Icons.Filled.Home),
    COMMAND("Command", Icons.Filled.Terminal),
    GOOGLE("Google", Icons.Filled.CalendarMonth),
    APPROVALS("Approvals", Icons.Filled.Shield),
    DEVICES("Devices", Icons.Filled.DevicesOther),
    SETTINGS("Settings", Icons.Filled.Settings),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppNavigation(
    apiClient: VantageApiClient,
    sessionStore: SessionStore,
    voiceController: VoiceController,
) {
    var selectedTab by remember { mutableStateOf(NavTab.HOME) }
    val pendingCount by apiClient.pendingApprovalCount.collectAsStateWithLifecycle()

    val homeViewModel: HomeViewModel = viewModel(
        factory = HomeViewModel.Factory(apiClient, sessionStore)
    )
    val commandViewModel: CommandViewModel = viewModel(
        factory = CommandViewModel.Factory(apiClient, sessionStore, voiceController)
    )
    val approvalsViewModel: ApprovalsViewModel = viewModel(
        factory = ApprovalsViewModel.Factory(apiClient, sessionStore)
    )
    val devicesViewModel: DevicesViewModel = viewModel(
        factory = DevicesViewModel.Factory(apiClient, sessionStore)
    )
    val googleViewModel: GoogleViewModel = viewModel(
        factory = GoogleViewModel.Factory(apiClient, sessionStore)
    )
    val settingsViewModel: SettingsViewModel = viewModel(
        factory = SettingsViewModel.Factory(apiClient, sessionStore, voiceController)
    )

    Scaffold(
        containerColor = Background,
        bottomBar = {
            NavigationBar(
                containerColor = Panel,
                contentColor = Ink,
            ) {
                NavTab.entries.forEach { tab ->
                    val count = if (tab == NavTab.APPROVALS) pendingCount else 0
                    NavigationBarItem(
                        selected = selectedTab == tab,
                        onClick = { selectedTab = tab },
                        icon = {
                            if (count > 0) {
                                BadgedBox(badge = {
                                    Badge(containerColor = Bad) {
                                        Text(count.toString())
                                    }
                                }) {
                                    Icon(tab.icon, contentDescription = tab.label)
                                }
                            } else {
                                Icon(tab.icon, contentDescription = tab.label)
                            }
                        },
                        label = { Text(tab.label) },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = Accent,
                            selectedTextColor = Accent,
                            unselectedIconColor = Muted,
                            unselectedTextColor = Muted,
                            indicatorColor = AccentSoft,
                        ),
                    )
                }
            }
        },
    ) { padding ->
        Column(modifier = Modifier.padding(padding)) {
        // One brand bar for every screen: the app had no identity above the
        // content after Setup. The title follows the selected tab, so it says
        // where you are without each screen drawing its own.
        VantageTopBar(title = selectedTab.label)
        AnimatedContent(
            targetState = selectedTab,
            modifier = Modifier.weight(1f),
            transitionSpec = {
                fadeIn() togetherWith fadeOut()
            },
        ) { tab ->
            when (tab) {
                NavTab.HOME -> HomeScreen(
                    viewModel = homeViewModel,
                    onOpenGoogle = { selectedTab = NavTab.GOOGLE },
                )
                NavTab.COMMAND -> CommandScreen(viewModel = commandViewModel)
                NavTab.APPROVALS -> ApprovalsScreen(viewModel = approvalsViewModel)
                NavTab.GOOGLE -> GoogleScreen(viewModel = googleViewModel)
                NavTab.DEVICES -> DevicesScreen(viewModel = devicesViewModel)
                NavTab.SETTINGS -> SettingsScreen(viewModel = settingsViewModel)
            }
        }
        }
    }
}