"""API contract tests — the shape of the API is a promise to its consumers.

The dashboard, the native macOS app, the Android companion and the relay all send
request bodies to these routes. A refactor that changes a field name, drops a field, or
changes a required field breaks a *client*, not a test — so the contract is pinned here.

This file is the API layer's own contract test: it lives beside the code that owns the
contract (`jarvis/api/schemas.py` + `jarvis/api/app.py`) and states what a consumer may
rely on. It is deliberately about the *contract*, not about behaviour (behaviour is
covered by the per-feature test files).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jarvis.api import schemas
from jarvis.api.app import app

AUTH = {"X-JARVIS-Token": "test-token-do-not-use-in-production"}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8765")


# ---------------------------------------------------------------------------
# 1. the body models live in the API layer's own module
# ---------------------------------------------------------------------------

def test_the_request_bodies_are_owned_by_the_api_layer():
    """`jarvis.api.schemas` owns the request contract.

    The models used to live inline in the 4,000-line `app.py`; a route module could not
    import one without importing the whole application (a circular import). Asserted here
    so the ownership does not quietly drift back.
    """
    for name in ("CommandBody", "MemoryBody", "BootstrapBody", "VoiceTextBody"):
        assert hasattr(schemas, name), f"{name} left the API layer's schema module"


def test_app_re_exports_the_bodies_for_existing_importers():
    """`from jarvis.api.app import CommandBody` keeps working.

    The bodies moved, but they were re-exported from `app.py` on purpose: an existing
    importer (a plugin, a script, a future route module) must not break because a
    refactor moved a class.
    """
    # `jarvis.api.__init__` exports the FastAPI *instance* as `app`, so the module
    # itself has to be reached through importlib (the pattern the API-shape test uses).
    import importlib

    api_mod = importlib.import_module("jarvis.api.app")

    for name in ("CommandBody", "MemoryBody", "BootstrapBody", "VoiceTextBody"):
        assert getattr(api_mod, name) is getattr(schemas, name), (
            f"{name} is no longer the same class in both places")


# ---------------------------------------------------------------------------
# 2. the published shape of a request body
# ---------------------------------------------------------------------------

def test_the_command_body_publishes_the_fields_consumers_send():
    """The `/command` body shape is a promise; the fields below are what the dashboard,
    the native app and the relay send."""
    schema = app.openapi()["components"]["schemas"]["CommandBody"]
    assert sorted(schema["properties"]) == ["context", "device_id", "session_id", "source", "text"]
    assert schema.get("required") == ["text"]


def test_the_command_body_still_refuses_an_empty_sentence():
    """A blank command is refused at the boundary, not somewhere deeper."""
    with pytest.raises(Exception):
        schemas.CommandBody(text="   ")


def test_the_command_body_trims_the_sentence():
    """A trailing newline from a paste must not change the command."""
    assert schemas.CommandBody(text="  what time is it\n").text == "what time is it"


# ---------------------------------------------------------------------------
# 3. the routes that carry these bodies still exist and still require a credential
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,method,body,empty_field", [
    ("/command", "post", {"text": "what time is it"}, "text"),
    ("/memory", "post", {"content": "remember the milk"}, "content"),
    ("/voice/command", "post", {"text": "what time is it"}, "text"),
])
def test_a_body_route_rejects_a_malformed_body(client: TestClient, path, method, body, empty_field):
    """A body that does not match the published schema is refused with a 422 — the
    consumer gets a validation error, never a silent success."""
    bad = dict(body)
    bad[empty_field] = "   "
    resp = getattr(client, method)(path, json=bad, headers=AUTH)
    assert resp.status_code in (400, 422), f"{path} accepted a malformed body: {resp.status_code}"


@pytest.mark.parametrize("path,method,body", [
    ("/command", "post", {"text": "what time is it"}),
    ("/voice/command", "post", {"text": "what time is it"}),
])
def test_a_body_route_requires_a_credential(client: TestClient, path, method, body):
    """These routes are protected; the credential gate is unchanged by the refactor."""
    resp = getattr(client, method)(path, json=body)
    assert resp.status_code in (401, 403), f"{path} accepted an anonymous body: {resp.status_code}"
