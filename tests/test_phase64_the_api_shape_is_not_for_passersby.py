"""Phase 64: the API's shape is not free information.

Measured on the delivered app before this phase wrote a line of code, against the process the
user is actually running (pid 25197, bound to `0.0.0.0:8765`, `lan_mode` on):

    GET http://127.0.0.1:8765/docs           -> 200
    GET http://172.20.168.82:8765/docs        -> 200   (this laptop's own Wi-Fi address)
    GET http://172.20.168.82:8765/openapi.json-> 200   82,556 bytes
    GET http://127.0.0.1:8765/health          -> 401
    GET http://172.20.168.82:8765/tools       -> 401

The credential gate held everywhere this project wrote a route. The only thing that answered
without a token was the description of everything the API can be told to do -- and because
`lan_mode` (Phase 35) deliberately makes `host_allowed` accept the addresses this machine owns,
"without a token" also means "without being this laptop".

Phase 9 filed this as P2 at 71,047 bytes, listing `/command`, `/confirm/{id}`, `/privacy/delete`
and `/memory/export` among the paths it published. Phase 59 closed the same three routes on the
relay, with a test. The host was never closed, because nobody had measured the host's own route
list since.

No test here claims a documentation page is useless. They claim it is absent unless a person
asks for it, and that asking is one switch with a name.
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jarvis.api.app import doc_routes
from jarvis.config import Settings

REPO = Path(__file__).resolve().parents[1]
API = importlib.import_module("jarvis.api.app")

#: What FastAPI mounts on its own, none of which takes a credential.
SHAPE_PATHS = ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect")

# Set by tests/conftest.py before any jarvis import.
TEST_TOKEN = os.environ["JARVIS_API_TOKEN"]
AUTH_HEADERS = {"X-JARVIS-Token": TEST_TOKEN}


@pytest.fixture(scope="module")
def anon() -> TestClient:
    """A client with no token, which is what anything on the network is."""
    return TestClient(API.app, base_url="http://127.0.0.1:8765")


@pytest.fixture(scope="module")
def authed() -> TestClient:
    return TestClient(API.app, base_url="http://127.0.0.1:8765", headers=AUTH_HEADERS)


def _paths(app: FastAPI) -> set:
    return {getattr(route, "path", "") for route in app.routes}


# ---------------------------------------------------------------------------
# 1. the shipped default serves nothing
# ---------------------------------------------------------------------------
def test_the_host_mounts_no_route_that_publishes_its_own_shape() -> None:
    """Route objects, not HTTP codes: a 404 can also come from a handler that exists.

    The mount is the stronger claim. If a change brought these routes back and answered 401
    instead of 200, this still fails -- the surface is what is being measured here, and a
    credential-gated blueprint page would be a different defect wearing the same clothes.
    """
    mounted = _paths(API.app)
    for path in SHAPE_PATHS:
        assert path not in mounted, f"{path} is mounted on the host"


def test_an_anonymous_request_for_the_blueprints_is_a_404(anon: TestClient) -> None:
    for path in SHAPE_PATHS:
        assert anon.get(path).status_code == 404, path


def test_the_404_is_a_refusal_and_not_a_dead_app(anon: TestClient) -> None:
    """Without this, "all four paths 404" is equally satisfied by an app that never started.

    `/` answers 200 (public by design since Phase 9) and `/health` answers 401 (protected); the
    404s sit between those two facts, so the process is demonstrably alive and choosing.
    """
    assert anon.get("/").status_code == 200
    assert anon.get("/health").status_code == 401
    for path in SHAPE_PATHS:
        assert anon.get(path).status_code == 404


def test_the_public_index_does_not_advertise_a_page_it_does_not_serve(anon: TestClient) -> None:
    """`{"docs": "/docs"}` on an unauthenticated route is a signpost to the blueprints.

    Absent rather than null: an index that reports a closed door is still describing a door,
    and there is no door.
    """
    body = anon.get("/").json()
    assert "docs" not in body, body
    assert body["name"] == "Vantage Core"


def test_the_shape_stays_closed_to_a_credential_that_could_have_read_it(authed: TestClient) -> None:
    """The master token used to be enough to fetch the schema; the token still buys the API.

    `/tools` returning a list of specs (not a wrapper object) is what this phase's first
    assertion got wrong and the code got right.
    """
    assert authed.get("/health").status_code == 200
    assert authed.get("/dashboard").status_code == 200
    tools = authed.get("/tools").json()
    assert isinstance(tools, list) and len(tools) > 100, f"tool list went thin: {len(tools)}"
    assert authed.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------------------
# 2. the switch, and that it really is a switch
# ---------------------------------------------------------------------------
def test_the_documentation_switch_is_off_unless_a_person_turns_it_on() -> None:
    assert Settings.model_fields["api_docs"].default is False
    # `env_prefix="JARVIS_"` on Settings is what makes the env name JARVIS_API_DOCS.
    assert Settings(api_docs=True).api_docs is True


def test_the_env_var_actually_reaches_the_setting(monkeypatch) -> None:
    """Reading the field's default would also pass if the env prefix were misspelled."""
    monkeypatch.setenv("JARVIS_API_DOCS", "1")
    assert Settings().api_docs is True
    monkeypatch.delenv("JARVIS_API_DOCS", raising=False)
    assert Settings().api_docs is False


def test_the_off_values_are_the_names_fastapi_reads() -> None:
    """`{"docs_url": None, "redoc_url": None, "openapi_url": None}`, and nothing else.

    A misspelled keyword is not an error to FastAPI -- it is a default that still mounts the
    route -- so the exact key names are asserted rather than the behaviour they produce.
    """
    off = doc_routes(Settings(api_docs=False))
    assert off == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    assert doc_routes(Settings(api_docs=True)) == {}


def test_an_opted_in_host_serves_the_shape_it_declined_by_default() -> None:
    """Proves the helper can open the door, so the 404 above is a decision and not an accident.

    Built as a separate app rather than by reloading `jarvis.api.app`: reloading that module
    mid-session would swap the shared `app` object for every later test, which is the state-leak
    class this project has already been bitten by twice (Phase 33's stub handlers, Phase 63's
    shared OCR backend).
    """
    app = FastAPI(title="opt-in", **doc_routes(Settings(api_docs=True)))
    app.get("/x")(lambda: {"ok": True})  # a real route, so the document has something in it
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200


def test_a_fastapi_app_built_without_the_helper_still_leaks_its_shape() -> None:
    """Negative control on the library's own default.

    Without it, a `doc_routes` that returned nothing at all would look identical to one that
    returns the off-values, and the whole point of the change is to override a default FastAPI
    supplies for you.
    """
    control = FastAPI(title="control")
    control.get("/x")(lambda: {"ok": True})
    assert {"/openapi.json", "/docs", "/redoc"} <= _paths(control)


def test_the_relay_and_the_host_decline_the_same_three_paths() -> None:
    """Phase 59 closed the relay's shape; this makes the two agree by route object, not prose.

    The relay's answer stays narrower on purpose -- `build_relay_app` hardcodes the off-values,
    so `JARVIS_API_DOCS=1` opens the host and not the door outside the house.
    """
    from jarvis.relay import RelayService, build_relay_app

    relay = build_relay_app(RelayService(host_base="http://127.0.0.1:1", pairing=None))
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert path not in _paths(API.app)
        assert path not in _paths(relay)


def test_every_mounted_route_is_one_this_project_wrote() -> None:
    """The stronger claim behind the four names above: nothing is mounted by default.

    `app.routes` minus the paths written into `jarvis/api/app.py` is whatever the framework put
    there on its own. On the current tree that set is empty, and the same expression on a bare
    `FastAPI()` returns exactly the three blueprints this phase closed -- which is what keeps the
    assertion from passing vacuously.
    """
    source = (REPO / "jarvis" / "api" / "app.py").read_text(encoding="utf-8")
    authored = set(re.findall(r'@app\.(?:get|post|put|delete|websocket|api_route)\("([^"]+)"',
                              source))
    library_owned = {p for p in _paths(API.app) - authored if p}
    assert library_owned == set(), f"routes FastAPI mounted unasked: {sorted(library_owned)}"
    assert {"/openapi.json", "/docs", "/redoc"} <= _paths(FastAPI())
