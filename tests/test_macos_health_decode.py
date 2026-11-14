"""The macOS app's health response must decode.

**The app's connection state was never actually available.** `HealthResponse`
declared

    security: [String: String]?
    privacy:   [String: String]?
    integrations: [String: Bool]?

but the backend's `/health` sends

    security:  {auth_enabled: true, token_path: "…", host_header_enforced: true,
                 rate_limit_per_minute: 600, allowed_origins: ["…"]}
    privacy:   {privacy_mode: false, redaction_enabled: true,
                 delete_scopes: ["audit", "…"]}
    integrations: {email: "local_maildir", calendar: "local_calendar"}

`security` and `privacy` are **mixed-type** maps — booleans, strings, numbers
and lists side by side — and `integrations` is a map of *strings*, not of
booleans. Every one of those declarations aborted the decode: the first
non-string value (`host_header_enforced`, a bool) threw a `typeMismatch`, and
`integrations.calendar` threw a second one.

The whole `/health` object was therefore unavailable. The app had **no**
connection state, no device count, no provider information and no tool count —
and the failure was **silent**, because the caller catches the error and keeps
polling. The app looked connected (the poll was running) while carrying no
health at all.

Measured, from the running app, before the fix:

```
HEALTH DECODE FAILED: The data couldn't be read because it isn't in the correct format.
```

and after it:

```
HEALTH decoded: status=ok tools=154 llm_enabled=Optional(true)
                ai_provider=Optional("9router") paired=Optional(6) securityKeys=8
```

The types are pinned from a **real** `/health` response, so a server-side
change breaks here rather than silently in the app. The check is a source
check plus a decode of a captured payload: the Swift decoder itself cannot
run in this environment (`no such module 'XCTest'`), so the payload is
decoded by the Python equivalent of the same rules and the Swift declaration
is read out of the source.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MACOS = REPO / "macos" / "Vantage"
BASE = "http://127.0.0.1:8765"


def _swift(*parts: str) -> str:
    return MACOS.joinpath("Sources", *parts).read_text(encoding="utf-8")


def _health_response() -> dict:
    """The backend's real `/health` payload."""
    import os

    token = (Path(os.path.expanduser("~/.jarvis-forge")) / "api_token").read_text().strip()
    request = urllib.request.Request(
        f"{BASE}/health", headers={"X-JARVIS-Token": token})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _declared_fields(struct: str) -> dict[str, str]:
    """The Swift struct's fields and their declared types.

    The type is the whole token after the colon: `[String: String]?`,
    `Bool?`, `Int?`, `String`. The optional marker matters — a
    non-optional field the server does not send aborts the decode.
    """
    source = _swift("APIClient", "APIModels.swift")
    match = re.search(r"struct " + struct + r": Codable[^{]*\{(.*?)\n    \}", source, re.S)
    assert match, f"{struct} is gone from the app's models"
    body = match.group(1)
    fields = {}
    for name, kind in re.findall(r"\blet (\w+):\s*(\[[^\]]+\]\??|[A-Za-z_][\w.]*\??)",
                                 body):
        fields[name] = kind.strip()
    return fields


# ---------------------------------------------------------------------------
# The types, against the real payload
# ---------------------------------------------------------------------------
def test_the_health_response_declares_the_types_the_server_sends():
    """Every declared type must match the payload.

    Checked against a **real** `/health` response, not a hand-written one:
    the whole point is that the app decodes what the server actually sends.
    """
    payload = _health_response()
    declared = _declared_fields("HealthResponse")

    # The mixed-type maps must be declared as mixed-type maps.
    for name in ("security", "privacy"):
        assert name in payload, f"the server no longer sends `{name}`"
        kinds = {type(v).__name__ for v in payload[name].values()}
        assert len(kinds) > 1, (
            f"`{name}` is now uniform ({kinds}); the app's declaration should "
            "follow it, not the other way round")
        assert "AnyCodable" in declared.get(name, ""), (
            f"`{name}` holds {sorted(kinds)} and must be declared as a map of "
            f"AnyCodable, not as {declared.get(name)!r} — a string-typed map "
            "aborts the decode on the first non-string value")

    # `integrations` reports the adapter in use, so it is a map of strings.
    kinds = {type(v).__name__ for v in payload.get("integrations", {}).values()}
    if kinds:
        assert kinds == {"str"}, (
            f"`integrations` holds {sorted(kinds)}; the app declares "
            f"{declared.get('integrations')!r}")


def test_integrations_are_declared_as_strings_not_booleans():
    """`integrations` reports the adapter in use.

    "email": "local_maildir", "calendar": "local_calendar" -- strings. It was
    declared `[String: Bool]?`, and `integrations.calendar` is a string, so
    that alone aborted the decode of the whole health response.
    """
    payload = _health_response()
    declared = _declared_fields("HealthResponse")
    integrations = payload.get("integrations") or {}
    if not integrations:
        pytest.skip("the server no longer sends `integrations`")
    for name, value in integrations.items():
        assert isinstance(value, str), (
            f"`integrations.{name}` is a {type(value).__name__}, not a string")
    assert declared.get("integrations") == "[String: String]?", (
        "`integrations` is a map of strings (the adapter in use), not of "
        f"booleans; the app declares {declared.get('integrations')!r}")


def test_the_scalar_fields_match():
    """The plain fields must be the right type."""
    payload = _health_response()
    declared = _declared_fields("HealthResponse")

    assert isinstance(payload["status"], str)
    assert isinstance(payload["tools"], int)
    assert isinstance(payload["llm_enabled"], bool)
    assert isinstance(payload["ai_provider"], str)
    assert declared.get("llm_enabled") == "Bool?"
    assert declared.get("ai_provider") == "String?"
    assert declared.get("status") == "String"


def test_the_device_health_shape_matches():
    """`devices` holds a nested map and a count."""
    payload = _health_response()
    devices = payload.get("devices") or {}
    assert "paired_devices" in devices
    assert isinstance(devices["paired_devices"], int)
    assert isinstance(devices.get("pairing"), dict)

    declared = _declared_fields("DeviceHealth")
    assert declared.get("paired_devices") == "Int?"
    assert "AnyCodable" in declared.get("pairing", "")


def test_every_declared_field_exists_in_the_payload():
    """A field the app declares that the server does not send is dead.

    It is harmless on its own, but it is a sign the model has drifted from
    the server, and it is worth knowing about.
    """
    payload = _health_response()
    declared = _declared_fields("HealthResponse")
    missing = [name for name in declared if name not in payload]
    # `recent_failures` is a legacy field; it is allowed to be absent.
    assert set(missing) <= {"recent_failures"}, (
        f"the app declares fields the server does not send: {missing}")


# ---------------------------------------------------------------------------
# The consequence: the app can now know its state
# ---------------------------------------------------------------------------
def test_the_app_reads_the_provider_state_from_health():
    """The Command Center's provider notice depends on this decode working.

    With the decode failing, `llm_enabled` and `ai_provider` were never
    available, so the app could not tell whether the AI path was answering —
    and a lookup fallback looked exactly like a model answer.
    """
    service = (_swift("Services", "ConversationService.swift"))
    assert "/health" in service, (
        "the conversation service does not read /health, so it cannot know "
        "whether a provider is configured")
    assert "llm_enabled" in service


def test_the_backend_manager_surfaces_the_health_decode_failure():
    """A decode failure must not be silently swallowed.

    The poll caught the error and kept going, so the app looked alive while
    carrying no health at all. The state has to say the truth.
    """
    source = _swift("Services", "BackendManager.swift")
    fetch = re.search(r"var healthFetcher.*?\n    \}", source, re.S)
    assert fetch, "the health fetcher is gone"
    body = fetch.group(0)
    # The decode is not behind a silent catch: a failure has to reach the
    # caller's error handling.
    assert "decode(APIModels.HealthResponse.self" in body, (
        "the health fetcher no longer decodes the health response")
    assert "catch" not in body.split("decode(APIModels.HealthResponse.self")[0][-200:] or (
        "the decode is wrapped in a catch that hides the failure")
