"""Tests for filesystem safety policy."""

from __future__ import annotations

import pytest

from jarvis.security.paths import is_protected, iter_string_values, protection_reason


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/etc/hosts",
        "/usr/bin/python3",
        "/System/Library",
        "/Applications",
        "~",
        "~/.ssh/id_rsa",
        "~/.aws/credentials",
        "~/.gnupg/secring.gpg",
    ],
)
def test_protected_paths_are_refused(path: str) -> None:
    assert is_protected(path), f"{path} should be protected"
    assert protection_reason(path)


@pytest.mark.parametrize(
    "path",
    ["/tmp", "/tmp/scratch.txt", "~/Desktop/report.pdf", "~/Documents", "/Volumes/USB/file.txt"],
)
def test_safe_paths_are_allowed(path: str) -> None:
    assert not is_protected(path), f"{path} should be allowed"


def test_tmp_symlink_is_not_treated_as_private() -> None:
    """On macOS /tmp resolves to /private/tmp and must still be writable."""
    assert not is_protected("/tmp/jarvis-forge-check")


@pytest.mark.parametrize(
    "path",
    [
        "~/Documents/LaunchAgents_backup.txt",
        "~/notes/ssh-config-notes.md",
        "~/projects/github-token-tutorial.txt",
        "~/my.sshrc",
        "~/config/ghb/hosts.yml",
    ],
)
def test_lookalike_names_are_not_protected(path: str) -> None:
    """A name that merely contains a protected name is not inside it.

    Matching protected entries as substrings refused ordinary files whose names
    happened to begin like a guarded directory, which pushed users toward
    disabling the guard rather than trusting it.
    """
    assert not is_protected(path), f"{path} should be allowed"


@pytest.mark.parametrize(
    "path",
    [
        "~/Library/LaunchAgents/com.example.agent.plist",
        "~/.config/gh/hosts.yml",
        "~/.ssh/keys/id_ed25519",
    ],
)
def test_protected_subpath_matching_is_component_wise(path: str) -> None:
    assert is_protected(path), f"{path} should be protected"


def test_iter_string_values_reaches_every_nested_string() -> None:
    """The guard sees values, so the walker must not miss a depth."""
    found = iter_string_values(
        {
            "command": "find",
            "args": ["~/Library/LaunchAgents", "*.plist", {"nested": "~/.ssh/id"}],
            "meta": {"keys": ["/etc/hosts"]},
        }
    )

    assert "~/Library/LaunchAgents" in found
    assert "~/.ssh/id" in found
    assert "/etc/hosts" in found
    assert "*.plist" in found
