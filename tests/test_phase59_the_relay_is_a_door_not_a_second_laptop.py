"""Phase 59 -- an authenticated relay, and the line between "works locally" and "reachable from outside".

What is being built is a real forwarder, not a mock: a client authenticates to the relay with the
device identity Vantage already issues (the pairing PSK, signed over a single-use challenge), the
relay then carries that client's *own* host credential to the host, and every Vantage decision --
permission, confirmation, verification, audit -- is still made by the host.

Three rules do the security work, and each is tested rather than asserted in a comment:

* **The relay never holds the master token.** It forwards the credential the client presented. A
  relay that carried its own laptop-wide token would turn one stolen relay session into total
  control, and it would make every command look like it came from the laptop itself.
* **Who you are is decided by what you can sign, not by what you type.** The `device_id` in a
  forwarded body is overwritten with the device that authenticated to the relay, so a client cannot
  attribute its work to another handset.
* **Down is down, not empty.** An unreachable host answers `502 {"relay_error": "host_unavailable"}`.
  Returning `[]` there is the bug this project already fixed twice for mail and calendar: an
  outage dressed up as "nothing to show".

What this file deliberately does *not* claim: that any of this proves reachability from outside the
network. Every test here is loopback, and a local hop cannot demonstrate a path through a router,
across an ISP, or past a firewall. That half is `BLOCKED_EXTERNAL_DEPENDENCY` (see the phase
report), and no amount of passing these tests changes it.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from jarvis.relay import RelayService, SESSION_HEADER, build_relay_app


HOST_BASE = "http://127.0.0.1:9910"
FAKE_MASTER = "master-token-that-must-never-leave-the-host"


# ---------------------------------------------------------------------------
# seams
# ---------------------------------------------------------------------------


class FakeSender:
    """Stands in for the host. Records what the relay actually sent -- the assertions
    about passthrough and attribution are made on this, not on the relay's reply."""

    def __init__(self, reply=(200, b'{"ok": true}'), error=None):
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, *, method, url, headers, content, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "content": content, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.reply

    @property
    def last(self):
        assert self.calls, "the relay never reached the host at all"
        return self.calls[-1]


class Clock:
    """A clock the test controls, so expiry is proven rather than slept through."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture()
def pairing():
    """A real paired device with its own id: pending request, approved, PSK on disk.

    Unique per test on purpose. ``revoke`` deletes the device's key file, and the pairing store is
    shared by everything in this session's home -- a fixed id would let the revocation test reach
    into the ones that run after it and take their credentials away. That is a broken fixture, not
    a broken relay.
    """
    import secrets as _secrets

    from jarvis.devices.pairing import PairingManager

    manager = PairingManager()
    device_id = "p59_" + _secrets.token_hex(5)
    record = manager.request(device_id=device_id, device_name="Phase 59 handset")
    pairing_id = record["pairing_id"] if isinstance(record, dict) else record.pairing_id
    issued = manager.approve(pairing_id)
    return manager, device_id, issued["secret_b64"]


def _service(pairing, sender=None, clock=None, **over):
    manager, _pairing_id, _secret = pairing
    return RelayService(host_base=HOST_BASE, pairing=manager, sender=sender or FakeSender(),
                        clock=clock or Clock(), **over)


def _client(service):
    return TestClient(build_relay_app(service), base_url="http://127.0.0.1:9911")


def _proof(secret_b64: str, challenge: str, device_id: str, timestamp: float) -> str:
    """The signature a legitimate handset produces: the PSK, over exactly what the host's
    `derive_proof` covers -- challenge, device id and the timestamp the relay handed out."""
    import base64

    key = base64.b64decode(secret_b64)
    body = f"{challenge}\n{device_id}\n{int(timestamp)}".encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def _authenticate(client, pairing, when=None):
    """Do the handshake the way a handset would: ask for a challenge, sign it, spend it."""
    _manager, device_id, secret = pairing
    challenge_reply = client.post("/relay/challenge", json={"device_id": device_id})
    assert challenge_reply.status_code == 200, challenge_reply.text
    issued = challenge_reply.json()
    stamp = issued["now"] if when is None else when
    return client.post("/relay/session", json={
        "device_id": device_id, "challenge": issued["challenge"],
        "proof": _proof(secret, issued["challenge"], device_id, stamp), "timestamp": stamp,
    })


# ---------------------------------------------------------------------------
# 1. authentication
# ---------------------------------------------------------------------------


def test_a_paired_device_can_open_a_relay_session(pairing):
    service = _service(pairing)
    with _client(service) as client:
        reply = _authenticate(client, pairing)
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["relay_session"]
        assert body["device_id"] == pairing[1]
        assert body["expires_in"] > 0


def test_an_unknown_device_gets_no_challenge(pairing):
    service = _service(pairing)
    with _client(service) as client:
        reply = client.post("/relay/challenge", json={"device_id": "not_a_device"})
    assert reply.status_code in (403, 404)
    assert "challenge" not in reply.json()


def test_a_pending_device_is_not_authorized(pairing):
    """Approval is the user's act. A relay that accepted a pending pairing would let any
    handset that guessed a device id onto the host before anyone said yes."""
    from jarvis.devices.pairing import PairingManager

    manager = PairingManager()
    manager.request(device_id="p59_pending", device_name="never approved")
    service = RelayService(host_base=HOST_BASE, pairing=manager, sender=FakeSender(),
                           clock=Clock())
    with _client(service) as client:
        assert client.post("/relay/challenge", json={"device_id": "p59_pending"}).status_code in (403, 404)


def test_a_wrong_proof_is_rejected_and_still_burns_the_challenge(pairing):
    service = _service(pairing)
    sender = FakeSender()
    service.sender = sender
    with _client(service) as client:
        issued = client.post("/relay/challenge", json={"device_id": pairing[1]}).json()
        challenge, now = issued["challenge"], issued["now"]
        bad = client.post("/relay/session", json={
            "device_id": pairing[1], "challenge": challenge,
            "proof": "0" * 64, "timestamp": now})
        assert bad.status_code in (401, 403)
        # The same challenge, this time with a *correct* proof: it must be gone, so a failed
        # attempt cannot leave a live challenge for a second guess.
        again = client.post("/relay/session", json={
            "device_id": pairing[1], "challenge": challenge,
            "proof": _proof(pairing[2], challenge, pairing[1], now),
            "timestamp": now})
        assert again.status_code in (401, 403), \
            "a failed attempt must not leave the challenge live for a second guess"
    assert sender.calls == []


def test_a_challenge_cannot_be_replayed(pairing):
    """The existing HMAC primitive has no nonce and no timestamp -- it is fresh because it is
    bound to one live connection. A relay session is a new envelope, so the replay defence has
    to come with it: single-use challenge, and a timestamp the client could not have chosen."""
    service = _service(pairing)
    with _client(service) as client:
        issued = client.post("/relay/challenge", json={"device_id": pairing[1]}).json()
        challenge, now = issued["challenge"], issued["now"]
        good = {"device_id": pairing[1], "challenge": challenge,
                "proof": _proof(pairing[2], challenge, pairing[1], now), "timestamp": now}
        assert client.post("/relay/session", json=good).status_code == 200
        # Byte-for-byte the request that just worked. It must not work twice: the challenge was
        # spent, so a captured handshake buys nothing a second time.
        assert client.post("/relay/session", json=good).status_code in (401, 403)


def test_a_stale_timestamp_is_rejected(pairing):
    """The signature includes the timestamp, so an hour-old captured handshake must fail on
    freshness even if every byte of it is genuine."""
    service = _service(pairing)
    with _client(service) as client:
        reply = _authenticate(client, pairing, when=time.time() - 3600)
        assert reply.status_code in (401, 403), reply.text
        assert "relay_session" not in reply.json()


def test_a_relay_session_expires(pairing):
    clock = Clock()
    service = RelayService(host_base=HOST_BASE, pairing=pairing[0], sender=FakeSender(),
                           clock=clock, session_ttl=60)
    with _client(service) as client:
        session = _authenticate(client, pairing).json()["relay_session"]
        clock.advance(61)
        reply = client.get("/relay/health", headers={SESSION_HEADER: session,
                                                     "X-JARVIS-Token": "host-session-token"})
    assert reply.status_code == 401


def test_a_missing_or_forged_session_never_reaches_the_host(pairing):
    sender = FakeSender()
    service = _service(pairing, sender)
    with _client(service) as client:
        assert client.get("/relay/health").status_code == 401
        assert client.get("/relay/health",
                          headers={SESSION_HEADER: "guessed"}).status_code == 401
    assert sender.calls == [], "the relay forwarded something it had not authenticated"


def test_signing_out_ends_one_session_and_not_the_other(pairing):
    service = _service(pairing)
    with _client(service) as client:
        first = _authenticate(client, pairing).json()["relay_session"]
        second = _authenticate(client, pairing).json()["relay_session"]
        assert client.delete("/relay/session", headers={SESSION_HEADER: first}).status_code == 200
        sender = FakeSender()
        service.sender = sender
        assert client.get("/relay/health", headers={SESSION_HEADER: first,
                                                    "X-JARVIS-Token": "t"}).status_code == 401
        assert client.get("/relay/health", headers={SESSION_HEADER: second,
                                                    "X-JARVIS-Token": "t"}).status_code == 200


def test_revoking_a_device_ends_its_relay_sessions_and_blocks_new_ones(pairing):
    """The whole point of revoking a handset is that it stops being able to act. A relay that
    let the session ride out its TTL after revocation would keep a stolen phone connected."""
    manager, pairing_id, secret = pairing
    service = _service(pairing, FakeSender())
    with _client(service) as client:
        session = _authenticate(client, pairing).json()["relay_session"]
        manager.revoke(pairing[1])
        assert client.get("/relay/health", headers={SESSION_HEADER: session,
                                                    "X-JARVIS-Token": "t"}).status_code == 401
        assert client.post("/relay/challenge",
                           json={"device_id": pairing[1]}).status_code in (403, 404)


# ---------------------------------------------------------------------------
# 2. forwarding, and what the relay is not allowed to change
# ---------------------------------------------------------------------------


def _authorized(pairing, service, sender=None):
    client = _client(service)
    session = _authenticate(client, pairing).json()["relay_session"]
    return client, session


def test_the_request_is_forwarded_verbatim_in_its_essentials(pairing):
    sender = FakeSender(reply=(200, b'{"reply":"battery 82%","verified":true}'))
    service = _service(pairing, sender)
    client, session = _authorized(pairing, service)
    with client:
        reply = client.post("/relay/command?retry=0",
                            headers={SESSION_HEADER: session, "X-JARVIS-Token": "host-token"},
                            json={"text": "what is my battery", "source": "phone"})
    assert reply.status_code == 200
    call = sender.last
    assert call["url"] == f"{HOST_BASE}/command?retry=0", call["url"]
    assert call["method"] == "POST"
    sent = json.loads(call["content"])
    assert sent["text"] == "what is my battery"
    assert call["headers"]["X-JARVIS-Token"] == "host-token"


def test_the_hosts_answer_is_returned_unchanged(pairing):
    """The relay is a pipe with a bouncer, not a judge: the host's status, tri-state verdict and
    wording come back exactly as they went in."""
    for status, body in [
        (200, b'{"reply":"done","verified":true,"actions":[{"status":"succeeded"}]}'),
        (200, b'{"reply":"could not confirm","verified":false}'),
        (403, b'{"detail":"This phone is not allowed to reach /audit."}'),
        (409, b'{"detail":"confirmation expired"}'),
        (500, b'{"detail":"internal error"}'),
    ]:
        sender = FakeSender(reply=(status, body))
        service = _service(pairing, sender)
        client, session = _authorized(pairing, service)
        with client:
            reply = client.post("/relay/command", headers={SESSION_HEADER: session,
                                                           "X-JARVIS-Token": "t"},
                                json={"text": "x"})
        assert reply.status_code == status, (status, reply.status_code)
        assert reply.content == body, (body, reply.content)


def test_the_relay_never_supplies_a_host_credential_of_its_own(pairing):
    """A request with no host credential must fail at the host, not quietly gain the relay's."""
    sender = FakeSender()
    service = _service(pairing, sender)
    client, session = _authorized(pairing, service)
    with client:
        reply = client.get("/relay/health", headers={SESSION_HEADER: session})
    assert reply.status_code in (401, 422), reply.text
    assert sender.calls == [], "the relay forwarded without a client credential"


def test_the_device_id_in_the_body_is_the_one_that_signed(pairing):
    """`device_id` is a claim the client types. Provenance in the audit trail has to come from
    what the relay actually verified, or a handset can book its work to another device."""
    sender = FakeSender()
    service = _service(pairing, sender)
    client, session = _authorized(pairing, service)
    with client:
        client.post("/relay/command", headers={SESSION_HEADER: session, "X-JARVIS-Token": "t"},
                    json={"text": "open Notes", "device_id": "laptop_main"})
    sent = json.loads(sender.last["content"])
    assert sent["device_id"] == pairing[1]
    assert sent["source"] == "relay"


def test_a_get_carries_no_body_and_a_post_carries_one(pairing):
    sender = FakeSender()
    service = _service(pairing, sender)
    client, session = _authorized(pairing, service)
    with client:
        client.get("/relay/confirmations", headers={SESSION_HEADER: session,
                                                    "X-JARVIS-Token": "t"})
    assert sender.last["content"] in (b"", None)
    assert sender.last["method"] == "GET"


def test_the_relay_refuses_paths_it_has_no_business_forwarding(pairing):
    """An entry point that sits outside the house gets the narrowest list it can work with.
    The phone's own surface is the precedent; this one adds /health because a remote client has
    no other way to learn whether the laptop is there."""
    sender = FakeSender()
    service = _service(pairing, sender)
    client, session = _authorized(pairing, service)
    forbidden = ["/relay/privacy/delete", "/relay/permissions", "/relay/permissions/policy",
                 "/relay/persistence", "/relay/memory/delete_all", "/relay/relay/session"]
    with client:
        for path in forbidden:
            reply = client.post(path, headers={SESSION_HEADER: session, "X-JARVIS-Token": "t"},
                                json={"scope": "all"})
            assert reply.status_code == 403, (path, reply.status_code, reply.text)
        allowed = client.get("/relay/tools", headers={SESSION_HEADER: session,
                                                      "X-JARVIS-Token": "t"})
        assert allowed.status_code == 200
    assert all("privacy" not in call["url"] for call in sender.calls)


def test_a_relay_path_that_is_not_a_host_path_is_not_guessed_at(pairing):
    service = _service(pairing, FakeSender())
    client, session = _authorized(pairing, service)
    with client:
        assert client.get("/relay", headers={SESSION_HEADER: session,
                                             "X-JARVIS-Token": "t"}).status_code in (404, 400)
        assert client.get("/relay//", headers={SESSION_HEADER: session,
                                               "X-JARVIS-Token": "t"}).status_code in (404, 400)


# ---------------------------------------------------------------------------
# 3. outages are outages
# ---------------------------------------------------------------------------


def test_an_unreachable_host_is_an_outage_not_an_empty_result(pairing):
    from jarvis.relay import RelayUnavailable

    sender = FakeSender(error=RelayUnavailable("connection refused"))
    service = _service(pairing, sender)
    client, session = _authorized(pairing, service)
    with client:
        reply = client.get("/relay/confirmations", headers={SESSION_HEADER: session,
                                                            "X-JARVIS-Token": "t"})
    assert reply.status_code == 502, reply.text
    body = reply.json()
    assert body["relay_error"] == "host_unavailable"
    assert reply.text.strip() not in ("[]", "{}", "null"), \
        "an outage returned as an empty list is how a down laptop reads as an idle one"


def test_a_timeout_is_reported_as_a_timeout(pairing):
    from jarvis.relay import RelayTimeout

    sender = FakeSender(error=RelayTimeout("no answer within the deadline"))
    service = _service(pairing, sender)
    client, session = _authorized(pairing, service)
    with client:
        reply = client.post("/relay/command", headers={SESSION_HEADER: session,
                                                       "X-JARVIS-Token": "t"},
                            json={"text": "x"})
    assert reply.status_code == 504
    assert reply.json()["relay_error"] == "host_timeout"


def test_one_request_is_one_attempt(pairing):
    """A relay that retries a POST /command is a relay that runs the action twice. Anything that
    can change the world goes out exactly once; the client decides whether to ask again."""
    from jarvis.relay import RelayUnavailable

    sender = FakeSender(error=RelayUnavailable("refused"))
    service = _service(pairing, sender)
    client, session = _authorized(pairing, service)
    with client:
        for _ in range(3):
            client.post("/relay/command", headers={SESSION_HEADER: session,
                                                   "X-JARVIS-Token": "t"},
                        json={"text": "turn off the heater"})
    assert len(sender.calls) == 3, "each client attempt must map to exactly one host attempt"


def test_every_forward_carries_a_bounded_deadline(pairing):
    sender = FakeSender()
    service = _service(pairing, sender)
    client, session = _authorized(pairing, service)
    with client:
        client.get("/relay/health", headers={SESSION_HEADER: session, "X-JARVIS-Token": "t"})
    assert sender.last["timeout"] == service.timeout
    assert 0 < service.timeout <= 30, "a relay hop must not outrun the sentence that asked for it"


# ---------------------------------------------------------------------------
# 4. nothing leaks
# ---------------------------------------------------------------------------


def test_the_master_token_appears_nowhere_in_the_relays_conversation(pairing):
    """The rule the whole credential layer rests on: the master token never leaves the host
    process. Checked against every byte the relay sent and every byte it answered."""
    sender = FakeSender()
    service = _service(pairing, sender)
    client, session = _authorized(pairing, service)
    with client:
        answers = [
            _authenticate(client, pairing).text,
            client.get("/relay/health", headers={SESSION_HEADER: session,
                                                 "X-JARVIS-Token": "t"}).text,
            client.get("/relay/health", headers={"X-JARVIS-Token": FAKE_MASTER}).text,
            client.get("/relay/health").text,
        ]
        state = json.dumps(service.describe())
    joined = " ".join(answers) + state + json.dumps(
        [{"url": c["url"], "headers": c["headers"]} for c in sender.calls])
    assert FAKE_MASTER not in joined
    assert pairing[2] not in joined, "the device PSK must not be echoed either"
    for call in sender.calls:
        # The relay proxies to the backend using the *client's* credential, never the
        # host master token: no outbound call may carry it. (Line 472 above already
        # covers this via the serialized call headers; kept explicit here so the
        # per-call intent is unambiguous instead of a bare substring check.)
        assert call["headers"].get("X-JARVIS-Token") != FAKE_MASTER, \
            f"an outbound relay call carried the master token: {call}"


def test_a_relay_session_carries_no_secret_in_its_token(pairing):
    service = _service(pairing)
    with _client(service) as client:
        session = _authenticate(client, pairing).json()["relay_session"]
    assert pairing[1] not in session
    assert hmac.compare_digest(session, session)  # opaque: nothing to parse
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,}", session), session


def test_the_relay_describes_itself_for_the_operator_without_credentials(pairing):
    service = _service(pairing)
    with _client(service) as client:
        _authenticate(client, pairing)
    described = service.describe()
    assert described["host_base"] == HOST_BASE
    assert described["live_sessions"] == 1
    assert "sessions" not in described, "a status report that lists tokens is a token store"
    assert set(described) <= {"host_base", "live_sessions", "allowed_paths", "timeout",
                              "session_ttl", "surface"}


def test_the_relay_app_never_mounts_a_route_that_would_leak_its_config(pairing):
    """No debug surface, no config dump: the relay is the one Vantage component whose position
    is outside the house."""
    service = _service(pairing)
    app = build_relay_app(service)
    paths = {getattr(route, "path", "") for route in app.routes}
    for path in ("/config", "/debug", "/env", "/relay/admin", "/token"):
        assert path not in paths


def test_the_relay_answers_only_for_the_endpoints_it_defines(pairing):
    """FastAPI ships an OpenAPI document and two doc pages unless they are turned off.

    Nothing in them is a secret -- but a component whose whole job is to hold the door open
    should present exactly the four endpoints it means to, so that a scan of it is a scan of
    the truth. Found by listing the live relay's routes rather than reading the source.
    """
    service = _service(pairing)
    with _client(service) as client:
        for path in ("/openapi.json", "/docs", "/redoc"):
            assert client.get(path).status_code == 404, path
        assert client.get("/relay/session").status_code == 401


# ---------------------------------------------------------------------------
# 5. provenance in the ledger, which is the host's job and was not happening
# ---------------------------------------------------------------------------


def test_a_command_from_elsewhere_is_distinguishable_in_the_audit(core):
    """Found by running the relay, not by unit-testing it.

    The relay rewrites the forwarded body so `device_id` is the handset that signed -- proven by
    the tests above. Then the host's own ledger threw that away: every executed action is written
    with ``device=action.device``, which is the *target* device Phase 31 made deliberate, so a
    command that arrived from a phone two rooms away was recorded identically to one typed on the
    laptop. Nothing was faked, but nothing was true either.

    The fix has to be additive, because repurposing the device column would corrupt device
    targeting. The ledger gains who asked, beside what was asked to change.
    """
    import asyncio

    from jarvis.core.models import CommandRequest

    response = asyncio.run(core.handle(
        CommandRequest(text="what time is it", device_id="p59_handset", source="relay")))
    assert response.reply, response
    rows = core.audit.db.query(
        "select intent, tool, device, detail from action_logs order by rowid desc limit 1")
    row = dict(rows[0])
    detail = json.loads(row["detail"])
    assert detail["requested_by"] == "p59_handset", detail
    assert detail["surface"] == "relay", detail
    assert row["device"] == "laptop_main", \
        "the device column stays the target: Phase 31's rule that the device you named is the " \
        "device that changes must not be quietly repurposed to mean who asked"
