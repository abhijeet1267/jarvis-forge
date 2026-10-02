"""Application registry (section 11).

Maps human phrases ("my coding environment", "chrome") to a launchable
application on a given platform. Keeping this declarative means the planner
never has to invent a binary path.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..core.models import AppSpec, RiskLevel

DEFAULT_APPS: List[AppSpec] = [
    AppSpec(
        key="vscode",
        display_name="Visual Studio Code",
        platform=["macos", "windows", "linux"],
        launch={"macos": "Visual Studio Code", "linux": "code", "windows": "code"},
        # The macOS `code` shim is not on PATH unless the user installs it from the
        # command palette, so the bundle's own CLI is named relative to the app:
        # `open -a` cannot take a folder, and that is exactly the complaint.
        cli={"macos": "Contents/Resources/app/bin/code", "linux": "code", "windows": "code"},
        aliases=["vs code", "vscode", "code", "coding environment", "ide", "editor"],
        category="development",
    ),
    AppSpec(
        key="chrome",
        display_name="Google Chrome",
        platform=["macos", "windows", "linux"],
        launch={"macos": "Google Chrome", "linux": "google-chrome", "windows": "chrome"},
        aliases=["chrome", "google chrome", "browser", "web browser"],
        category="browser",
    ),
    AppSpec(
        key="safari",
        display_name="Safari",
        platform=["macos"],
        launch={"macos": "Safari"},
        aliases=["safari"],
        category="browser",
    ),
    AppSpec(
        key="terminal",
        display_name="Terminal",
        platform=["macos"],
        launch={"macos": "Terminal"},
        aliases=["terminal", "console", "shell"],
        category="development",
    ),
    AppSpec(
        key="finder",
        display_name="Finder",
        platform=["macos"],
        launch={"macos": "Finder"},
        aliases=["finder", "files", "file manager"],
        category="system",
    ),
    AppSpec(
        key="notes",
        display_name="Notes",
        platform=["macos"],
        launch={"macos": "Notes"},
        aliases=["notes", "notepad"],
        category="productivity",
    ),
    AppSpec(
        key="music",
        display_name="Music",
        platform=["macos"],
        launch={"macos": "Music"},
        aliases=["music", "spotify"],
        category="media",
    ),
    AppSpec(
        key="calculator",
        display_name="Calculator",
        platform=["macos"],
        launch={"macos": "Calculator"},
        aliases=["calculator", "calc"],
        category="utility",
    ),
]


class AppRegistry:
    """Look up applications by key, alias or fuzzy phrase."""

    def __init__(self, apps: Optional[List[AppSpec]] = None) -> None:
        self._apps: Dict[str, AppSpec] = {}
        self._alias_index: Dict[str, str] = {}
        for app in apps or DEFAULT_APPS:
            self.register(app)

    def register(self, app: AppSpec) -> None:
        self._apps[app.key] = app
        self._alias_index[app.key] = app.key
        self._alias_index[app.display_name.lower()] = app.key
        for alias in app.aliases:
            self._alias_index[alias.lower()] = app.key

    def get(self, key: str) -> Optional[AppSpec]:
        return self._apps.get(self._alias_index.get(key.lower(), key))

    def resolve(self, phrase: str, platform: str = "macos") -> Optional[AppSpec]:
        """Resolve a natural phrase to an app usable on ``platform``."""
        if not phrase:
            return None
        text = phrase.strip().lower()

        # Exact alias hit first.
        key = self._alias_index.get(text)
        if key:
            return self._filter_platform(self._apps[key], platform)

        # Then substring / token overlap, longest alias wins.
        best: Optional[AppSpec] = None
        best_len = 0
        for alias, candidate_key in self._alias_index.items():
            if alias in text and len(alias) > best_len:
                best = self._apps[candidate_key]
                best_len = len(alias)
        return self._filter_platform(best, platform)

    def all(self) -> List[AppSpec]:
        return list(self._apps.values())

    @staticmethod
    def _filter_platform(app: Optional[AppSpec], platform: str) -> Optional[AppSpec]:
        if app is None:
            return None
        if platform and platform not in app.platform:
            return None
        return app

    def launch_target(self, app: AppSpec, platform: str = "macos") -> Optional[str]:
        return app.launch.get(platform) or app.launch.get("macos")


__all__ = ["AppRegistry", "DEFAULT_APPS", "RiskLevel"]
