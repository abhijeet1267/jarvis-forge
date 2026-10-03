"""Phase 48: a name that stays the same.

Measured premise, from the delivery README's own words: *"The address is your laptop's wifi
address, and the router hands that out again whenever the lease renews. It changed once today
(…169.34 became …169.185) without anything being touched."* Every phone bookmark made before a
renewal dies with the lease, and the project has carried that as 38-B / 39-E / 40-A.

The laptop already has a stable name -- `scutil --get LocalHostName` and `hostname` both say
``Abhijeets-MacBook-Air-2.local`` -- and the gate refuses it. Reading the reason (40-A) says why
it is worth being careful here::

    # LAN mode: accept exactly the addresses this machine owns. A hostname is never accepted
    # here -- that is the DNS-rebinding case the loopback rule was written for.

That rule is right about *hostnames*. It is over-broad about **mDNS names**: ``.local`` is
reserved by RFC 6762 and is never answerable through public DNS, so an attacker cannot register
``evil.local`` and point it at a victim's laptop the way ``evil.example.com`` can be pointed at
127.0.0.1. The narrowing this phase tests is therefore two conditions, not one: the name must be
in ``.local``, **and** it must resolve to an address this machine already owns.

Also pinned here: the advertised URL list and the accepting gate are one source (the drift
between them is how the first real-browser phone test failed in Phase 35), and a name check that
asks the resolver on every request is a denial-of-service handle on the laptop's own network.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from jarvis.config import Settings
from jarvis.security import auth as auth_module
from jarvis.security.auth import host_allowed, interface_addresses, mdns_name_is_ours


@pytest.fixture()
def lan():
    """A LAN-mode settings object that is not the cached session one."""
    return Settings(host="0.0.0.0", port=8765, lan_mode=True, enforce_host_header=True)


#: The address these tests pretend this machine owns. Not read from the host: the wifi
#: lease changed *during* this phase, which took seven of them down -- the exact failure
#: the phase exists to fix, arriving as a test bug. Ownership is asserted against a
#: fixture, so the file passes on a laptop with no network at all.
OURS = "10.0.0.5"
THEIRS = "10.99.1.7"


@pytest.fixture(autouse=True)
def _hermetic_network(monkeypatch):
    """No real resolver, no real interface list, no real lease.

    Every test injects its own answer: resolution needs an mDNS responder that a CI box
    or a laptop on a train does not have, and `interface_addresses()` returns whatever the
    router handed out this minute.
    """
    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: [])
    monkeypatch.setattr(auth_module, "_mdns_cache", {})
    monkeypatch.setattr(auth_module, "interface_addresses",
                        lambda: {"127.0.0.1", "::1", "localhost", OURS})


# ---------------------------------------------------------------------------
# 1. the gate: what a name may and may not be
# ---------------------------------------------------------------------------


def test_a_local_name_that_is_ours_is_accepted(lan, monkeypatch):
    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: [OURS])
    assert host_allowed("Abhijeets-MacBook-Air-2.local:8765", lan) is True, (
        "the laptop's own mDNS name is still refused, which is the whole defect")


def test_a_public_name_pointing_at_us_is_still_refused(lan, monkeypatch):
    """The rebinding case the rule was written for, kept intact.

    An attacker's page can resolve evil.example.com to this laptop's LAN address, and the
    victim's browser will send `Host: evil.example.com` from the same network. A name
    outside .local cannot be proven local, so it stays refused even when it resolves here.
    """
    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: [OURS])
    assert host_allowed("evil.example.com:8765", lan) is False
    assert host_allowed("attacker.local.test:8765", lan) is False


def test_a_local_name_pointing_at_someone_else_is_refused(lan, monkeypatch):
    """`.local` alone is not the credential; the address has to be ours."""
    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: [THEIRS])
    assert host_allowed("sometherouter.local:8765", lan) is False


def test_a_local_name_with_no_answer_is_refused_not_raising(lan):
    """No mDNS responder, or a name that does not exist: a refusal, never a 500."""
    assert host_allowed("nosuchhost.local:8765", lan) is False


def test_names_are_refused_while_lan_mode_is_off(monkeypatch):
    """Private posture is unchanged: a laptop that has not opted into the wifi is still
    loopback-only, and the name must not open it."""
    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: [OURS])
    private = Settings(host="127.0.0.1", port=8765, lan_mode=False)
    assert host_allowed("Abhijeets-MacBook-Air-2.local:8765", private) is False
    assert host_allowed("127.0.0.1:8765", private) is True


def test_the_check_is_cached_per_name(lan, monkeypatch):
    """A request-time DNS lookup is a slow path an adjacent device can drive. One answer
    per name per TTL, not one per request."""
    calls = []

    def counting(name):
        calls.append(name)
        return [OURS]

    monkeypatch.setattr(auth_module, "_resolve_mdns", counting)
    for _ in range(25):
        assert host_allowed("Abhijeets-MacBook-Air-2.local:8765", lan) is True
    assert len(calls) == 1, f"the resolver was asked {len(calls)} times for one name"


def test_a_cached_refusal_expires(monkeypatch, lan):
    """The lease and the responder both change, so a 'no' may not be forever."""
    answers = {"value": []}
    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: answers["value"])
    clock = {"now": 1000.0}
    monkeypatch.setattr(auth_module, "_now", lambda: clock["now"])
    assert host_allowed("laptop.local:8765", lan) is False

    answers["value"] = [OURS]
    assert host_allowed("laptop.local:8765", lan) is False, "still cached, as expected"
    clock["now"] += auth_module.MDNS_TTL_SECONDS + 1
    assert host_allowed("laptop.local:8765", lan) is True, "the answer never expires"

# ---------------------------------------------------------------------------
# 2. the name is what gets advertised, from the same source
# ---------------------------------------------------------------------------


def test_the_advertised_hosts_include_the_name(lan, monkeypatch):
    """A gate that accepts a name the panel never prints is half a feature; a printed name
    the gate refuses is a page that loads and then cannot be used. Both come from
    `client_hosts()`, which is why this test is about that list and not about strings."""
    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: [OURS])
    hosts = lan.client_hosts()
    assert socket.gethostname().lower() in [h.lower() for h in hosts], hosts


def test_the_advertised_urls_and_accepted_origins_agree(lan, monkeypatch):
    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: [OURS])
    hosts = lan.client_hosts()
    urls = lan.reachable_urls()
    origins = lan.allowed_origins()
    named = [h for h in hosts if h.endswith(".local")]
    assert named, "no name in the advertised list"
    for host in named:
        assert any(f"//{host}:" in u for u in urls), (host, urls)
        assert any(f"//{host}:" in o for o in origins), (host, origins)
        assert host_allowed(f"{host}:8765", lan) is True, (
            f"{host} is advertised but the gate refuses it -- the Phase 35 drift, again")


def test_no_name_is_advertised_while_private(monkeypatch):
    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: [OURS])
    private = Settings(host="127.0.0.1", lan_mode=False)
    assert private.client_hosts() == ["127.0.0.1"]


# ---------------------------------------------------------------------------
# 3. what the browser actually sends back
# ---------------------------------------------------------------------------


def test_the_advertised_name_is_the_form_a_browser_will_send(lan, monkeypatch):
    """Found in a real browser, not in a unit test: the panel printed
    ``Abhijeets-MacBook-Air-2.local``, Chrome lowered it to
    ``abhijeets-macbook-air-2.local``, and ``POST /auth/session`` answered **403** --
    the page loaded and every call after it was 401. A name advertised in a form the
    browser rewrites is a name that cannot be used.
    """
    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: [OURS])
    monkeypatch.setattr(socket, "gethostname", lambda: "Abhijeets-MacBook-Air-2.local")
    hosts = lan.client_hosts()
    named = [h for h in hosts if h.endswith(".local")]
    assert named, hosts
    assert named == [h.lower() for h in named], f"advertised in a form browsers rewrite: {named}"


def test_the_origin_gate_ignores_host_case(lan, monkeypatch):
    """The comparison itself, so a hand-typed mixed-case address still works and the rule
    does not depend on every client lowering the same way."""
    from jarvis.api.app import _refuse_unless_same_origin

    monkeypatch.setattr(auth_module, "_resolve_mdns", lambda name: [OURS])
    monkeypatch.setattr(socket, "gethostname", lambda: "Abhijeets-MacBook-Air-2.local")
    request = SimpleNamespace(headers={
        "origin": "http://Abhijeets-MacBook-Air-2.local:8765"})
    lowered = SimpleNamespace(headers={
        "origin": "http://abhijeets-macbook-air-2.local:8765"})
    assert lan.allowed_origins(), "nothing is allowed, so the test below proves nothing"
    _refuse_unless_same_origin(lowered, lan)          # the browser's form
    _refuse_unless_same_origin(request, lan)          # the form a person types


def test_a_foreign_origin_is_still_refused(lan):
    from fastapi import HTTPException

    from jarvis.api.app import _refuse_unless_same_origin

    request = SimpleNamespace(headers={"origin": "http://evil.example.com:8765"})
    with pytest.raises(HTTPException) as caught:
        _refuse_unless_same_origin(request, lan)
    assert caught.value.status_code == 403


# ---------------------------------------------------------------------------
# 4. what the panel prints and encodes
# ---------------------------------------------------------------------------

URLS = [
    "http://127.0.0.1:8765/phone",
    "http://172.20.169.185:8765/phone",
    "http://Abhijeets-MacBook-Air-2.local:8765/phone",
]


def test_the_panel_prefers_the_name_over_the_address():
    """The QR is the thing a person scans, and today it encodes the address the router
    takes back at every lease renewal -- which is the exact failure the delivery README
    tells the user to live with.
    """
    from app.launcher import lan_phone_url

    assert lan_phone_url(URLS).endswith("Abhijeets-MacBook-Air-2.local:8765/phone")


def test_the_panel_still_shows_an_address_for_browsers_without_mdns():
    """Android's Chrome does not reliably resolve `.local`, so hiding the number behind
    the name would trade one broken phone for another. The preferred line is the name;
    the alternative has to stay visible.
    """
    from app.launcher import lan_fallback_url

    assert lan_fallback_url(URLS) == "http://172.20.169.185:8765/phone"


def test_the_fallback_is_quiet_when_the_name_is_all_there_is():
    from app.launcher import lan_fallback_url

    assert lan_fallback_url(["http://127.0.0.1:8765/phone"]) == ""
    assert lan_fallback_url(
        ["http://127.0.0.1:8765/phone", "http://laptop.local:8765/phone"]) == ""


def test_no_name_no_preference_order_change():
    """On a machine whose hostname is not a `.local` name, nothing about the old choice
    changes -- the first address a phone can reach still wins."""
    from app.launcher import lan_phone_url

    assert lan_phone_url(URLS[:2]) == "http://172.20.169.185:8765/phone"
    assert lan_phone_url([]) == ""


# ---------------------------------------------------------------------------
# 5. the helper itself
# ---------------------------------------------------------------------------


def test_mdns_helper_ignores_names_outside_the_reserved_domain():
    """A lookup for a public name is both pointless and the rebinding vector; the helper
    should not make one at all."""
    assert mdns_name_is_ours("evil.example.com") is False
    assert mdns_name_is_ours("example.com") is False


def test_the_laptop_actually_has_a_local_name_on_this_machine():
    """The premise, not assumed: `hostname` must end in .local for any of this to matter.
    On a machine where it does not, the phase's own claim would be about a name that does
    not exist, so the test says so rather than passing vacuously."""
    name = socket.gethostname()
    if not name.lower().endswith(".local"):
        pytest.skip(f"this machine advertises no mDNS name (hostname is {name!r})")
    assert name.count(".") == 1, name
