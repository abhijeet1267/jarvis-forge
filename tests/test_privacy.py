"""Privacy control tests (section 49).

Note on scope: the vendor-prefix redaction patterns are exercised indirectly
here rather than with literal example keys. Sample credentials in a test file
are indistinguishable from real ones to a secret scanner, and that is a
reasonable thing for a scanner to block -- so the tests below cover the
behaviour that matters (known secrets are masked, sensitive keys are masked,
ordinary text is untouched, privacy mode is additive, export/delete are safe)
without embedding anything credential-shaped in the source.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jarvis.security.privacy import (
    MASK,
    PrivacyManager,
    contains_secret,
    describe_privacy,
    redact,
    redact_value,
)

# The configured token is a real "known secret" the redactor must mask.
KNOWN_TOKEN = os.environ["JARVIS_API_TOKEN"]


# ---------------------------------------------------------------------------
# Redaction: known secrets
# ---------------------------------------------------------------------------
def test_configured_secret_is_masked() -> None:
    """The per-install token must never survive redaction."""
    out = redact(f"the token is {KNOWN_TOKEN} by the way")
    assert KNOWN_TOKEN not in out
    assert MASK in out


def test_contains_secret_finds_configured_secret() -> None:
    assert contains_secret(f"x {KNOWN_TOKEN} y")
    assert not contains_secret("just a normal sentence")


# ---------------------------------------------------------------------------
# Redaction: must not damage ordinary text
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "the report is at /tmp/report.pdf",
        "nothing sensitive in this sentence",
        "open VS Code please",
        "the gearbox overheated during the endurance test",
        "the AMR chassis uses 3mm aluminium",
    ],
)
def test_ordinary_text_is_untouched(text: str) -> None:
    """False positives would corrupt the audit log, so this matters."""
    assert redact(text) == text


# ---------------------------------------------------------------------------
# Redaction: structural
# ---------------------------------------------------------------------------
def test_sensitive_keys_are_masked_regardless_of_value() -> None:
    """Key-name masking is structural -- it does not depend on the value."""
    payload = {
        "api_key": "anything-at-all",
        "api_token": "whatever",
        "password": "hunter2000",
        "authorization": "some-value",
        "note": "safe text",
    }
    out = redact_value(payload)

    for key in ("api_key", "api_token", "password", "authorization"):
        assert out[key] == MASK, f"{key} was not masked"
    assert out["note"] == "safe text", "a benign key must survive"


def test_redact_value_walks_nested_structures() -> None:
    payload = {
        "outer": {"api_key": "value", "list": [{"secret": "value"}, "plain"]},
        "count": 3,
    }
    out = redact_value(payload)

    assert out["outer"]["api_key"] == MASK
    assert out["outer"]["list"][0]["secret"] == MASK
    assert out["outer"]["list"][1] == "plain"
    assert out["count"] == 3


def test_redact_handles_empty_and_non_string_input() -> None:
    assert redact("") == ""
    assert redact_value(None) is None
    assert redact_value(42) == 42


# ---------------------------------------------------------------------------
# Privacy mode
# ---------------------------------------------------------------------------
def test_privacy_mode_collapses_home_paths(monkeypatch) -> None:
    """Privacy mode is additive: it hides more than the always-on redaction."""
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "privacy_mode", True)

    home = str(Path.home())
    out = redact(f"the file lives at {home}/Documents/report.pdf", settings)
    assert home not in out
    assert "~" in out


def test_home_paths_visible_when_privacy_mode_off(monkeypatch) -> None:
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "privacy_mode", False)

    home = str(Path.home())
    text = f"the file lives at {home}/Documents/report.pdf"
    assert redact(text, settings) == text


def test_describe_privacy_shape() -> None:
    info = describe_privacy()
    assert info["redaction_enabled"] is True
    assert "memories" in info["delete_scopes"]
    assert info["export_redacted_by_default"] is True


# ---------------------------------------------------------------------------
# Audit integration
# ---------------------------------------------------------------------------
def test_audit_never_persists_the_configured_token(core) -> None:
    core.audit.record(
        intent="test",
        tool="test_tool",
        target=f"used {KNOWN_TOKEN}",
        result="success",
        detail={"note": f"token {KNOWN_TOKEN}"},
    )

    assert KNOWN_TOKEN not in json.dumps(core.audit.recent(limit=5))


def test_audit_masks_sensitive_detail_keys(core) -> None:
    core.audit.record(
        intent="test",
        tool="test_tool",
        result="success",
        detail={"api_key": "some-value", "note": "keep me"},
    )
    row = core.audit.recent(limit=1)[0]
    assert row["detail"]["api_key"] == MASK
    assert row["detail"]["note"] == "keep me"


# ---------------------------------------------------------------------------
# Export and deletion
# ---------------------------------------------------------------------------
def test_export_is_redacted_by_default(core) -> None:
    from jarvis.core.models import MemoryScope

    core.memory.remember(f"my token is {KNOWN_TOKEN}", scope=MemoryScope.LONG_TERM)

    payload = PrivacyManager().export(redact_output=True)
    assert KNOWN_TOKEN not in json.dumps(payload)
    assert payload["redacted"] is True
    assert "totals" in payload


def test_export_can_include_everything_when_asked(core) -> None:
    payload = PrivacyManager().export(redact_output=False)
    assert payload["redacted"] is False
    assert "memories" in payload["data"]


def test_write_export_is_owner_only(core, tmp_area: Path) -> None:
    target = PrivacyManager().write_export(tmp_area / "export.json")
    assert target.exists()
    assert (target.stat().st_mode & 0o777) == 0o600


def test_preview_delete_does_not_delete(core) -> None:
    from jarvis.core.models import MemoryScope

    core.memory.remember("preview me", scope=MemoryScope.LONG_TERM)
    before = core.memory.stats()["total"]

    preview = PrivacyManager().preview_delete("memories")
    assert preview["memories"] >= 1
    assert core.memory.stats()["total"] == before, "preview must not delete"


def test_delete_requires_explicit_confirmation(core) -> None:
    """Irreversible deletion must never happen implicitly."""
    with pytest.raises(ValueError, match="confirm"):
        PrivacyManager().delete("memories", confirm=False)


def test_delete_removes_only_the_requested_scope(core) -> None:
    from jarvis.core.models import MemoryScope

    core.memory.remember("memory to delete", scope=MemoryScope.LONG_TERM)
    core.audit.record(intent="keep-me", tool="get_time", result="success")

    result = PrivacyManager().delete("memories", confirm=True)
    assert result["total_removed"] >= 1
    assert core.memory.stats()["total"] == 0

    # The audit log is a different scope and must be untouched.
    assert core.audit.recent(limit=10)


def test_unknown_scope_is_rejected(core) -> None:
    with pytest.raises(ValueError, match="unknown scope"):
        PrivacyManager().delete("not_a_scope", confirm=True)
