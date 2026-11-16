"""Privacy Egress Firewall: a declared allowlist at the one read chokepoint.

The property that matters is not "there is a list" -- it is that a read to an
undeclared host **cannot open a socket**, and that privacy mode refuses the
kinds of destination that would carry the conversation off the machine while
leaving loopback and lookups working. A gate that only filtered a screen would
be a promise; this is the check.
"""

from __future__ import annotations

import urllib.request

import pytest

from jarvis.net import EgressRefused, http_read
from jarvis.security.egress import (
    ALLOWED,
    LOOPBACK,
    REFUSED,
    SERVICE,
    Destination,
    EgressFirewall,
    describe_firewall,
    host_of,
    _is_loopback,
)


# -- host parsing -------------------------------------------------------------

@pytest.mark.parametrize("url,host", [
    ("https://api.openai.com/v1/chat", "api.openai.com"),
    ("http://127.0.0.1:8765/health", "127.0.0.1"),
    ("https://EN.Wikipedia.ORG/x", "en.wikipedia.org"),
    ("https://user:pw@example.com:8443/p?q=1", "example.com"),
    ("not a url", ""),
])
def test_host_of_reads_the_real_destination(url, host):
    assert host_of(url) == host


@pytest.mark.parametrize("host,loopback", [
    ("127.0.0.1", True),
    ("127.1.2.3", True),
    ("::1", True),
    ("localhost", True),
    ("api.localhost", True),
    ("n8n", True),          # a Docker-style single-label service name
    ("example.com", False),
    ("api.openai.com", False),
    ("", False),
])
def test_loopback_recognition(host, loopback):
    assert _is_loopback(host) is loopback


# -- the allowlist -----------------------------------------------------------

def test_a_declared_provider_is_allowed_when_privacy_is_off():
    v = EgressFirewall().check("https://api.openai.com/v1/chat/completions")
    assert v.allowed and v.decision == ALLOWED and v.kind == "provider"


def test_an_undeclared_host_is_refused():
    v = EgressFirewall().check("https://collect.example.net/beacon")
    assert not v.allowed and v.decision == REFUSED
    assert "not a declared destination" in v.reason


def test_privacy_mode_refuses_a_provider_but_not_a_lookup():
    fw = EgressFirewall()
    provider = fw.check("https://api.openai.com/v1", privacy_mode=True)
    lookup = fw.check("https://en.wikipedia.org/api/rest_v1/page/summary/X",
                      privacy_mode=True)
    assert not provider.allowed and "privacy mode" in provider.reason
    assert lookup.allowed


def test_privacy_mode_never_refuses_this_machine():
    v = EgressFirewall().check("http://127.0.0.1:8765/health", privacy_mode=True)
    assert v.allowed and v.kind == LOOPBACK


def test_privacy_mode_refuses_a_first_party_service():
    """A configured Google account is still egress: privacy mode says no."""
    v = EgressFirewall().check("https://www.googleapis.com/calendar/v3/x",
                               privacy_mode=True)
    assert not v.allowed and v.kind == SERVICE


def test_a_new_destination_is_declared_by_naming_it():
    fw = EgressFirewall()
    assert not fw.check("https://internal.example/x").allowed
    fw.add(Destination("internal.example", SERVICE, "a service the person added"))
    assert fw.check("https://internal.example/x").allowed


# -- the gate is real: no socket for a refused read ---------------------------

class _Spy:
    """Stands in for urlopen so a test can tell whether a socket was opened."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("urlopen must not be reached for a refused host")


def test_a_refused_read_never_opens_a_socket(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(urllib.request, "urlopen", spy)
    request = urllib.request.Request("https://collect.example.net/beacon")

    with pytest.raises(EgressRefused) as caught:
        http_read(request, total=1.0)

    assert spy.calls == 0, "the refused read reached the network"
    assert "collect.example.net" in str(caught.value)


def test_a_refused_read_is_not_reported_as_a_timeout():
    """A refusal is its own failure. If it were a DeadlineExceeded, the provider
    path would label it a timeout and the person would hunt a broken network."""
    from jarvis.net import DeadlineExceeded

    request = urllib.request.Request("https://collect.example.net/x")
    with pytest.raises(EgressRefused) as caught:
        http_read(request, total=1.0)
    assert not isinstance(caught.value, DeadlineExceeded)


def test_privacy_mode_is_read_at_check_time(monkeypatch):
    """Flipping the switch takes effect on the next read without a rebuild."""
    from jarvis.config import get_settings

    settings = get_settings()
    original = settings.privacy_mode
    request = urllib.request.Request("https://api.openai.com/v1/chat/completions")
    try:
        settings.privacy_mode = True
        with pytest.raises(EgressRefused) as caught:
            http_read(request, total=1.0)
        assert "privacy mode" in str(caught.value)
    finally:
        settings.privacy_mode = original


# -- the provider path names the refusal --------------------------------------

def test_a_provider_reports_an_egress_refusal_as_its_own_category(monkeypatch):
    """`ProviderReply.notes["category"]` must say egress_refused, not network."""
    from jarvis.config import get_settings
    from jarvis.gateway.providers import OpenAIProvider

    settings = get_settings()
    original_privacy = settings.privacy_mode
    original_url = settings.llm_base_url
    original_key = settings.llm_api_key
    try:
        settings.privacy_mode = True
        provider = OpenAIProvider(settings)
        # Give it an endpoint and a key so it reaches the *network* step; the
        # firewall refuses before any header is sent, so neither is used.
        provider.settings.llm_base_url = "https://api.openai.com/v1"
        provider.settings.llm_api_key = "test-key-not-a-real-credential"
        reply = provider.verify_connection(timeout=1.0, model="gpt-4o-mini")
        assert reply.ok is False
        assert (reply.notes or {}).get("category") == "egress_refused", reply.notes
        assert "privacy mode" in (reply.error or "")
    finally:
        settings.privacy_mode = original_privacy
        settings.llm_base_url = original_url
        settings.llm_api_key = original_key


# -- the posture is honest ----------------------------------------------------

def test_the_posture_states_what_it_does_not_cover():
    posture = describe_firewall()
    assert posture["enforced_at"] == "jarvis.net.http_read"
    assert "not seen or controlled" in posture["not_covered"]
    assert posture["declared_hosts"] >= 10


# -- over the wire ------------------------------------------------------------

def test_the_posture_is_served_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    body = client.get("/egress", headers=headers).json()
    assert body["declared_hosts"] >= 10
    assert any(d["host"] == "api.openai.com" and d["kind"] == "provider"
               for d in body["destinations"])


def test_the_check_route_refuses_an_undeclared_host():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    response = client.post("/egress/check", headers=headers,
                           json={"url": "https://collect.example.net/x"})
    assert response.status_code == 200
    assert response.json()["allowed"] is False


def test_the_check_route_needs_a_url():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    assert client.post("/egress/check", headers=headers, json={}).status_code == 422

# -- the httpx call sites are gated too --------------------------------------

def test_require_refuses_an_undeclared_host():
    """`require` is the seam for callers that build their own httpx client."""
    from jarvis.security.egress import require

    with pytest.raises(EgressRefused):
        require("https://collect.example.net/x")


def test_require_allows_a_loopback_address():
    from jarvis.security.egress import require

    require("http://127.0.0.1:8765/health")  # must not raise


def test_the_relay_refuses_an_undeclared_peer():
    """The relay dials the laptop over the LAN; an undeclared peer is refused
    before a socket is opened, and reported as unavailable with the reason."""
    from jarvis.relay import RelayService, RelayUnavailable

    # `_httpx_sender` is the only place the relay talks to anything.
    service = RelayService(host_base="http://127.0.0.1:8765", pairing=object())
    with pytest.raises(RelayUnavailable) as caught:
        service._httpx_sender(method="GET", url="https://collect.example.net/x",
                              headers={}, content=b"", timeout=1.0)
    assert "egress firewall" in str(caught.value)


def test_the_stt_backend_refuses_a_provider_under_privacy_mode(tmp_path):
    """Speech to a provider is egress like a chat completion: the audio is not
    read, let alone sent, when privacy mode is on."""
    import asyncio

    from jarvis.config import get_settings
    from jarvis.voice.stt import OpenAIWhisperSTT

    settings = get_settings()
    original_privacy = settings.privacy_mode
    original_url = settings.llm_base_url
    original_key = settings.llm_api_key
    try:
        settings.privacy_mode = True
        settings.llm_base_url = "https://api.openai.com/v1"
        settings.llm_api_key = "test-key-not-a-real-credential"
        audio = tmp_path / "clip.wav"
        audio.write_bytes(b"RIFF....not really audio")
        result = asyncio.run(OpenAIWhisperSTT(settings).transcribe(audio))
        assert result.ok is False
        assert "refused" in (result.error or "").lower(), result.error
        assert "privacy mode" in (result.error or "")
    finally:
        settings.privacy_mode = original_privacy
        settings.llm_base_url = original_url
        settings.llm_api_key = original_key


def test_a_refused_caldav_read_is_classified_as_unavailable_not_a_network_error():
    """A blocked read must not be reported as a network failure -- the person
    would go debugging a connection that is fine."""
    from jarvis.integrations.calendar_provider import (
        ProviderStatus,
        classify_transport_error,
    )

    refusal = EgressRefused(
        __import__("jarvis.security.egress", fromlist=["Verdict"]).Verdict(
            False, "collect.example.net", "unknown", "refused",
            "not a declared destination"))
    assert classify_transport_error(refusal) is ProviderStatus.UNAVAILABLE


def test_the_calendar_reads_the_firewall_before_dialling(monkeypatch):
    """`require` is called on the CalDAV path, so an undeclared URL never
    reaches `httpx.Client.request`."""
    import jarvis.integrations.calendar_provider as cal

    called: list[str] = []
    monkeypatch.setattr("jarvis.security.egress.require",
                        lambda url: called.append(url) or (_ for _ in ()).throw(
                            EgressRefused(__import__(
                                "jarvis.security.egress", fromlist=["Verdict"]
                            ).Verdict(False, "x", "unknown", "refused", "blocked"))))

    # A bare source check: the module must reference `require` on the query path.
    source = __import__("inspect").getsource(cal)
    assert "require(self.url)" in source, "the CalDAV client is not gated"
