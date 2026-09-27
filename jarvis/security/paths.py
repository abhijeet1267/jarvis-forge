"""Filesystem safety policy (single source of truth).

Both the permission engine and the file tools consult this module, so a path
can never be allowed by one layer and refused by another.

On macOS several friendly paths are symlinks into ``/private`` (``/tmp`` ->
``/private/tmp``), so a naive prefix check on ``/private`` would block
legitimate work. Safe prefixes are therefore evaluated *before* protected
roots.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

# Roots that must never be touched.
PROTECTED_ROOTS = [
    "/",
    "/System",
    "/Library",
    "/bin",
    "/sbin",
    "/usr",
    "/etc",
    "/var",
    "/private",
    "/Applications",
]

# Locations under the user's home that are private by nature.
#
# This list is a *security boundary*, not a convenience. Anything that grants
# persistence (shell startup files, launch agents), holds credentials, or can
# influence other programs belongs here. Writing to a shell rc file is
# equivalent to arbitrary code execution on next login.
PROTECTED_SUBPATHS = [
    # --- credentials & secrets ---------------------------------------------
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".netrc",
    ".git-credentials",
    ".docker",
    ".config/gh",
    ".config/gcloud",
    ".npmrc",
    ".pypirc",
    "Library/Keychains",
    "Library/Cookies",
    # --- persistence: code that runs without the user present ---------------
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".zlogin",
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".profile",
    ".cshrc",
    ".tcshrc",
    ".config/fish",
    "Library/LaunchAgents",
    "Library/LaunchDaemons",
    "Library/StartupItems",
    # --- system-level influence --------------------------------------------
    "Library/Preferences",
    "Library/Application Support/com.apple",
]

# Explicitly safe areas. Checked before PROTECTED_ROOTS so that /tmp works.
SAFE_PREFIXES = [
    "/tmp",
    "/private/tmp",
    "/var/folders",
    "/private/var/folders",
    "/Volumes",
]


def _safe_prefixes() -> list[str]:
    return [*SAFE_PREFIXES, str(Path.home())]


def _parts(path: str) -> list[str]:
    """Split into comparable path components, ignoring separators and empties."""
    return [part for part in path.split(os.sep) if part]


def _contains(haystack: list[str], needle: list[str]) -> bool:
    """True when ``needle`` appears as a run of whole path components.

    Matching components rather than substrings is what keeps
    ``~/Documents/LaunchAgents_backup.txt`` from being refused because a
    protected directory happens to share its prefix. A name that contains a
    protected name is not inside it.
    """
    n, m = len(haystack), len(needle)
    if m == 0 or n < m:
        return False
    return any(haystack[i : i + m] == needle for i in range(n - m + 1))


def iter_string_values(value: Any) -> list[str]:
    """Every string reachable inside a parameter structure.

    The guard needs to see *values*, not the names the model happened to give
    its arguments: ``directory``, ``args[0]`` and a nested dict entry are all
    places a path can hide.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            for key, item in node.items():
                if isinstance(key, str):
                    found.append(key)
                walk(item)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)

    walk(value)
    return found


def resolve_path(value: str) -> Optional[str]:
    """Expand and resolve a user-supplied path. Returns None if unusable."""
    if not value or not isinstance(value, str):
        return None
    try:
        return str(Path(os.path.expanduser(value)).resolve())
    except (OSError, RuntimeError):
        return None


def protection_reason(path: str) -> Optional[str]:
    """Return a human-readable refusal reason if ``path`` is protected."""
    resolved = resolve_path(path)
    if resolved is None:
        return None

    home = str(Path.home())
    # Report the path the user actually typed, not the resolved symlink target
    # (on macOS /etc -> /private/etc, which reads confusingly).
    display = os.path.expanduser(path)

    # 1. Private subpaths win over everything, including the home prefix.
    parts = _parts(resolved)
    for sub in PROTECTED_SUBPATHS:
        if _contains(parts, _parts(sub)):
            return f"{sub} is a protected private location."

    # 2. The home directory itself.
    if resolved == home:
        return "Your home directory is protected."

    # 3. Exact protected roots.
    for root in PROTECTED_ROOTS:
        if resolved == root:
            return f"{display} is a protected system location."

    # 4. Explicitly safe areas.
    for safe in _safe_prefixes():
        if resolved == safe or resolved.startswith(safe.rstrip("/") + "/"):
            return None

    # 5. Anything else inside a protected root.
    for root in PROTECTED_ROOTS:
        if root != "/" and resolved.startswith(root.rstrip("/") + "/"):
            return f"{root} is a protected system location."

    return None


def is_protected(path: str) -> bool:
    return protection_reason(path) is not None
