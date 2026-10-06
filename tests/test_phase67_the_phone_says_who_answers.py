"""Phase 67: the phone says who answers, and no gate moves to let it speak.

**63-A** — `grep -n "health|llm|model|provider" dashboard/phone.html` returns **nothing**: the page a
person holds in their hand has no idea whether rules or a vendor is answering. Phase 60 made the
vendor selectable and Phase 63 gave the laptop's panel a badge that names it; the phone got neither.

**63-B** — `/health` publishes `provider_paths` and, until this phase, the only reader in the
repository was a test. A field with no surface is a field nobody can be wrong about and nobody can
check.

The fix had to avoid the obvious bad shape. The phone session is deliberately narrow (Phase 58):
`/health`, `/audit` and `/privacy/delete` answer it **403**, and that is the property this phase
must not trade away for a status line. So no new endpoint is reachable and `PHONE_ALLOWED` is
asserted not to have grown. The value arrives the way the bootstrap code already does -- rendered
into the HTML by the route that serves the page, which is the same page the phone is already
allowed to load, and which the worker already refetches from the network first.

The second half of the phase is honesty in the refusal path: `provider_capabilities()` returned two
keys when the provider name was unusable and four when it was fine, so a reader that asked "what
about vision?" got silence rather than "not routed to this vendor".
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
API = importlib.import_module("jarvis.api.app")
from jarvis.config import Settings
from jarvis.gateway.providers import provider_capabilities

node = shutil.which("node")

# Set by tests/conftest.py before any jarvis import.
TEST_TOKEN = os.environ["JARVIS_API_TOKEN"]
AUTH_HEADERS = {"X-JARVIS-Token": TEST_TOKEN}

@pytest.fixture(scope="module")
def client() -> TestClient:
    """An authenticated client on a loopback Host, which is what the gate expects."""
    fresh = TestClient(API.app, base_url="http://127.0.0.1:8765", headers=AUTH_HEADERS)
    yield fresh


#: The surface Phase 58 fixed at code-minting time. This phase adds a status line, not a path.
PHONE_ALLOWED_AS_SHIPPED = {
    "/command", "/confirmations", "/devices", "/devices/pair/request",
    "/emergency_stop", "/tools", "/voice/status", "/voice/transcribe",
}


# ---------------------------------------------------------------------------
# 1. what the laptop says about who answers
# ---------------------------------------------------------------------------
def test_no_key_means_the_line_names_rules_and_no_vendor() -> None:
    """A vendor nobody will call is not a fact about the answer (Phase 63's badge rule)."""
    status = API.model_status(Settings(llm_api_key="", llm_base_url=""))
    assert status["llm_enabled"] is False
    assert status["answered_by"] == "rules (no key)", status
    assert "gemini" not in status["answered_by"] and "openai" not in status["answered_by"]


def test_a_key_makes_the_line_name_the_vendor_that_would_be_called() -> None:
    gemini = API.model_status(Settings(llm_api_key="k", llm_base_url="https://x/v1",
                                       ai_provider="gemini"))
    assert gemini["answered_by"] == "gemini", gemini
    openai = API.model_status(Settings(llm_api_key="k", llm_base_url="https://x/v1"))
    assert openai["answered_by"] == "openai", openai


def test_an_unusable_provider_name_is_reported_as_a_refusal_not_a_vendor() -> None:
    status = API.model_status(Settings(llm_api_key="k", llm_base_url="https://x/v1",
                                       ai_provider="mistral"))
    assert "refused" in status["answered_by"], status
    assert "mistral" in status["answered_by"], "the refusal has to name what it refused"


def test_provider_capabilities_describes_the_same_four_tasks_whatever_the_answer() -> None:
    """The gap this phase found: four keys when the vendor is fine, two when it is not.

    A reader that asks "and vision?" must get the same *kind* of answer in both cases -- either a
    vendor name or a sentence saying it is not routed -- rather than a missing key, which reads as
    "nothing to report" and not as "nobody has looked".
    """
    ok = provider_capabilities(Settings(llm_api_key="k", llm_base_url="https://x/v1"))
    assert set(ok) == {"chat", "embed", "vision", "stt"}, ok
    refused = provider_capabilities(Settings(llm_api_key="k", llm_base_url="https://x/v1",
                                             ai_provider="mistral"))
    assert set(refused) == set(ok), f"the refusal path dropped {set(ok) - set(refused)}"
    for key in ("vision", "stt"):
        assert refused[key], f"{key} was left blank instead of being described"
        assert "refused" not in refused[key] or "not provider-routed" in refused[key]


# ---------------------------------------------------------------------------
# 2. the page carries it, and the token does not travel with it
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def phone_html() -> str:
    client = TestClient(API.app, base_url="http://127.0.0.1:8765")
    response = client.get("/phone")
    assert response.status_code == 200
    return response.text


def test_the_served_page_carries_the_model_as_data_not_as_prose(phone_html: str) -> None:
    """Injected like the bootstrap code: the route decides, the page renders."""
    block = re.search(r'<script type="application/json" id="jarvis-model">(.*?)</script>',
                      phone_html, re.S)
    assert block, "the page ships no model block for the phone to read"
    data = json.loads(block.group(1))
    assert {"answered_by", "chat", "embed", "vision", "stt", "llm_enabled"} <= set(data), data


def test_the_placeholder_never_survives_to_the_phone(phone_html: str) -> None:
    assert "__JARVIS_MODEL_JSON__" not in phone_html, "the laptop served its own template hole"


def test_the_injected_model_carries_no_credential(phone_html: str) -> None:
    """This page is fetched by a device that has not been trusted yet (Phase 10's rule)."""
    assert TEST_TOKEN not in phone_html
    block = re.search(r'<script type="application/json" id="jarvis-model">(.*?)</script>',
                      phone_html, re.S)
    body = block.group(1)
    for leak in ("api_token", "secret", "Bearer"):
        assert leak not in body, f"{leak} travelled inside a status field"


# ---------------------------------------------------------------------------
# 3. the gate did not move
# ---------------------------------------------------------------------------
def test_the_phone_reaches_exactly_the_paths_it_reached_before_this_phase() -> None:
    """The temptation this phase had to refuse: add `/health` to the allowlist and be done.

    `/health` answers tools, agents, memory counts, security posture and peripherals -- a status
    line does not need any of it, and a phone that can read it can read more tomorrow. So the
    allowlist is pinned to the set Phase 58 shipped, and the assertion is an equality, not a subset.
    """
    assert set(API.PHONE_ALLOWED) == PHONE_ALLOWED_AS_SHIPPED, (
        f"+{sorted(set(API.PHONE_ALLOWED) - PHONE_ALLOWED_AS_SHIPPED)} "
        f"-{sorted(PHONE_ALLOWED_AS_SHIPPED - set(API.PHONE_ALLOWED))}")
    assert API.PHONE_ALLOWED_PREFIXES == ("/confirm/",)


def test_a_phone_session_still_cannot_read_the_laptop_wider_state(client) -> None:
    """Proved live, not asserted in a constant: the phone mints a code, buys a session, and is
    refused the three paths that would turn a status line into a reconnaissance surface."""
    minted = client.get("/phone")
    bootstrap = re.search(r'name="jarvis-bootstrap" content="([^"]+)"', minted.text)
    assert bootstrap, "no phone code to spend"
    session = client.post("/auth/session", json={"bootstrap": bootstrap.group(1)},
                          headers={"Origin": "http://127.0.0.1:8765"})
    assert session.status_code == 200, session.text
    token = session.json()["session_token"]
    phone = {"X-JARVIS-Token": token}

    assert client.get("/tools", headers=phone).status_code == 200
    for path in ("/health", "/audit", "/privacy/delete"):
        refused = client.post(path, headers=phone) if path.endswith("delete") else \
            client.get(path, headers=phone)
        assert refused.status_code == 403, f"{path} answered {refused.status_code} to a phone"


# ---------------------------------------------------------------------------
# 4. the shipped renderer, run under node
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def render(phone_html: str):
    if not node:
        pytest.skip("node not available to run the page's own code")
    body = re.search(r"<script>(.*?)</script>", phone_html, re.S).group(1)
    start = body.index("function modelLine")
    source = body[start:body.index("\n}", start) + 2]
    assert "answered_by" in source, "the extracted function is not the model line"

    def _run(model, root=REPO):
        script = Path(root) / "p67_model_line.js"
        script.write_text(source + "\nconsole.log(modelLine(%s));" % json.dumps(model),
                          encoding="utf-8")
        done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
        script.unlink(missing_ok=True)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    return _run


def test_the_phone_line_names_rules_when_nobody_is_configured(render) -> None:
    text = render({"answered_by": "rules (no key)", "chat": "refused: no key",
                   "embed": "refused: no key", "vision": "openai-shaped, not provider-routed",
                   "stt": "openai-shaped, not provider-routed", "llm_enabled": False})
    assert text.startswith("answers: rules"), text
    assert "no vendor" in text, text


def test_the_phone_line_names_the_vendor_and_what_it_does_not_reach(render) -> None:
    text = render({"answered_by": "gemini", "chat": "gemini", "embed": "gemini",
                   "vision": "openai-shaped, not provider-routed",
                   "stt": "openai-shaped, not provider-routed", "llm_enabled": True})
    assert "gemini" in text, text
    assert "screen text" in text and "voice" in text, f"the boundary is missing: {text}"
    assert "not provider-routed" in text, text


def test_the_refusal_is_printed_as_a_refusal(render) -> None:
    """A line that quietly fell back to a vendor name would be the Phase 60 bug on a new surface."""
    text = render({"answered_by": "refused: unknown provider mistral (known: gemini, openai)",
                   "chat": "refused: unknown provider mistral (known: gemini, openai)",
                   "embed": "refused: unknown provider mistral (known: gemini, openai)",
                   "vision": "not asked -- the vendor could not be chosen",
                   "stt": "not asked -- the vendor could not be chosen",
                   "llm_enabled": True})
    assert "refused" in text.lower(), text
    assert "mistral" in text, text


def test_a_missing_value_renders_as_unknown_rather_than_blank(render) -> None:
    text = render({})
    assert "unknown" in text, text
    assert text.strip(), "an empty status line is worse than no line"


# ---------------------------------------------------------------------------
# 5. the shell version has to advance when the shell changes
# ---------------------------------------------------------------------------
def test_the_offline_shell_a_phone_already_installed_carries_the_new_line(client) -> None:
    """The page gained an element and a renderer, so a phone holding the old cache would render the
    line from a shell that has no such element. The worker's cache name is the fix, and it is only a
    fix if it moves: `activate()` deletes every cache that is not the current name.

    Phase 58 pinned the version as the literal `2`, which means the *next* shell change would fail
    a test that is actually a rule about advancing. The invariant here is the same one Phase 57
    applied to frozen gate counts: assert that it moves forward, not that it equals today.
    """
    body = client.get("/sw.js").text
    names = {int(n) for n in re.findall(r"phone-shell-v(\d+)", body)}
    assert names, "no versioned shell cache"
    assert max(names) >= 3, f"the shell changed and the cache name stayed at v{max(names)}"
    assert "caches.delete" in body, "old caches are never removed, so phones keep the old shell"
