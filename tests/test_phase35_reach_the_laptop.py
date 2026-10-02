"""Phase 35: reach the laptop from another device, and tell it what Qoder is doing.

The user asked for an Android app, a Windows app, and voice that answers to their own words,
so that a phone in their hand can pair with the laptop, see what JARVIS wants to do, and say
yes or no from there. What exists and what does not was measured before writing anything:

```
jarvis/security/auth.py:132   host must be in LOOPBACK_HOSTS      -> a phone cannot connect
jarvis/config.py:36           host = "127.0.0.1"                  -> nothing on the LAN reaches it
jarvis/devices/pairing.py     request / pending / approve / secret -> the pairing half exists
jarvis/api/app.py             /watch/collect, /watch/ack, /watch/reply,
                              /devices/{id}/endpoint               -> a device protocol exists
                                                          no client page speaks it
/Applications/Qoder.app, ~/.qoder/bin/qoder-computer-use/          -> present, nothing asks it
jarvis/voice/wakeword.py      configurable phrases, fuzzy matching -> today it reads transcripts
```

So the gap is not the pairing logic; it is that the door is shut and nobody on the other side
of it has a page to open. The door is closed for a reason this project states out loud: a
localhost API that executes real actions is reachable by any web page the user visits, which
is why `Host` is checked at all. Opening it therefore has to be opt-in, and it has to keep the
two protections that survive the change -- the token, and the anti-DNS-rebinding check, which
becomes "this request must name an address this machine actually owns" rather than "must name
loopback".
"""

from __future__ import annotations

import asyncio
import json

import pytest
from jarvis.config import Settings, get_settings
from jarvis.core.models import CommandRequest, PolicyDecision, RiskLevel
from jarvis.security.auth import host_allowed, lan_startup_problems


# ---------------------------------------------------------------------------
# 1. the door stays shut unless it is opened on purpose
# ---------------------------------------------------------------------------


def test_lan_mode_is_off_by_default():
    assert Settings().lan_mode is False, "a laptop must not expose itself by default"


def test_loopback_is_still_accepted_with_lan_mode_off():
    assert host_allowed("127.0.0.1:8765", Settings()) is True


def test_a_lan_address_is_refused_while_lan_mode_is_off(monkeypatch):
    settings = Settings()
    assert settings.lan_mode is False
    for address in ("192.168.1.40:8765", "10.0.0.5:8765"):
        assert host_allowed(address, settings) is False, address


def test_a_lan_address_this_machine_owns_is_accepted_in_lan_mode(monkeypatch):
    """The phone's `Host` header is the laptop's own LAN address, so accept exactly
    addresses this machine owns -- nothing broader."""
    monkeypatch.setattr("jarvis.security.auth.interface_addresses",
                        lambda: {"192.168.1.40", "fe80::1"})
    assert host_allowed("192.168.1.40:8765", Settings(lan_mode=True)) is True


def test_a_foreign_hostname_is_never_accepted_even_in_lan_mode(monkeypatch):
    """DNS rebinding is the reason this check exists. An attacker's domain resolving to
    a laptop's LAN address must not become a reason to trust the request."""
    monkeypatch.setattr("jarvis.security.auth.interface_addresses",
                        lambda: {"192.168.1.40"})
    settings = Settings(lan_mode=True)
    for host in ("evil.example.com:8765", "attacker.local", "localhost.evil.test"):
        assert host_allowed(host, settings) is False, host


def test_localhost_still_works_in_lan_mode():
    assert host_allowed("127.0.0.1:8765", Settings(lan_mode=True)) is True


def test_lan_mode_refuses_to_start_without_the_token():
    """LAN plus no authentication is a remote shell on someone's wifi. The setting that
    turns on reachability must not be able to turn off the thing that controls it."""
    problems = lan_startup_problems(Settings(lan_mode=True, auth_enabled=False, host="0.0.0.0"))
    assert problems, "lan mode without auth must refuse to start"
    assert any("token" in p.lower() or "auth" in p.lower() for p in problems), problems
    assert lan_startup_problems(
        Settings(lan_mode=True, auth_enabled=True, host="0.0.0.0",
                 api_token="a-real-token")) == []
    assert lan_startup_problems(Settings(lan_mode=False, auth_enabled=False)) == []


def test_lan_mode_asks_for_an_explicit_bind_or_keeps_loopback():
    """Binding to every interface should be a second, deliberate act."""
    problems = lan_startup_problems(Settings(lan_mode=True, host="127.0.0.1"))
    assert any("host" in p.lower() for p in problems), problems


def test_the_server_refuses_to_boot_in_an_unsafe_lan_config(monkeypatch):
    """A check nobody runs is a comment, not a guard.

    `lan_startup_problems` was written, tested, and wired into nothing: the settings a
    user puts in a file or an environment can only be caught when the process that will
    act on them starts up. So the app's own startup must consult it and refuse.
    """
    import importlib

    from fastapi.testclient import TestClient

    # `jarvis.api.app` is shadowed by the FastAPI instance the package re-exports,
    # so the module has to be reached through the import machinery.
    app_module = importlib.import_module("jarvis.api.app")

    bad = Settings(lan_mode=True, host="127.0.0.1", auth_enabled=True,
                   api_token="a-real-token-value")
    monkeypatch.setattr(app_module, "get_settings", lambda: bad)
    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(app_module.app, base_url="http://127.0.0.1:8765"):
            pass
    said = str(excinfo.value)
    assert "JARVIS_HOST" in said, said


def test_the_server_boots_when_lan_mode_is_configured_honestly(monkeypatch):
    """The refusal has to be specific or it becomes a way to turn the product off."""
    import importlib

    from fastapi.testclient import TestClient

    # `jarvis.api.app` is shadowed by the FastAPI instance the package re-exports,
    # so the module has to be reached through the import machinery.
    app_module = importlib.import_module("jarvis.api.app")

    good = Settings(lan_mode=True, host="0.0.0.0", auth_enabled=True,
                    api_token=get_settings().api_token)
    monkeypatch.setattr(app_module, "get_settings", lambda: good)
    with TestClient(app_module.app, base_url="http://127.0.0.1:8765",
                    headers={"X-JARVIS-Token": get_settings().api_token}) as test_client:
        assert test_client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# 1b. what to type on the phone
# ---------------------------------------------------------------------------


def test_the_addresses_to_try_are_the_ones_the_server_will_answer(monkeypatch):
    """`host = 0.0.0.0` is a bind address, not something you can put in a browser.

    Opening the door is only useful if the user can find it, so the laptop reports the
    addresses it both owns and accepts — the same set `host_allowed` will let through.
    """
    import jarvis.security.auth as auth_module

    monkeypatch.setattr(auth_module, "interface_addresses",
                        lambda: {"192.168.1.40", "127.0.0.1", "::1"})
    urls = Settings(lan_mode=True, host="0.0.0.0", port=8765).reachable_urls()
    assert "http://192.168.1.40:8765/phone" in urls, urls
    assert any("127.0.0.1" in u for u in urls), urls


def test_a_loopback_only_server_advertises_nothing_a_phone_could_use(monkeypatch):
    import jarvis.security.auth as auth_module

    monkeypatch.setattr(auth_module, "interface_addresses",
                        lambda: {"192.168.1.40", "127.0.0.1"})
    urls = Settings(lan_mode=False, host="127.0.0.1", port=8765).reachable_urls()
    assert urls == ["http://127.0.0.1:8765/phone"], urls
    assert not any("192.168" in u for u in urls), "must not invite a phone it would refuse"


def test_advertised_addresses_are_ones_a_phone_can_actually_type(monkeypatch):
    """A link-local IPv6 without a zone id is unroutable, and an IPv6 literal needs
    brackets to be a URL host at all. `http://fe80::1:8791/phone` is neither."""
    import jarvis.security.auth as auth_module

    monkeypatch.setattr(auth_module, "interface_addresses", lambda: {
        "192.168.1.40", "127.0.0.1", "::1", "localhost",
        "fe80::834:22c2:9066:2805", "fd00::21"})
    urls = Settings(lan_mode=True, host="0.0.0.0", port=8765).reachable_urls()
    assert "http://192.168.1.40:8765/phone" in urls, urls
    from urllib.parse import urlparse

    for url in urls:
        assert "fe80" not in url, f"link-local is not reachable from another device: {url}"
        # A browser must be able to read a port off it; an unbracketed IPv6 literal
        # parses as host "fd00" port "21", which is what makes the URL unusable.
        assert urlparse(url).port == 8765, f"not a well-formed URL: {url}"


def test_starting_the_server_prints_the_address_to_type(monkeypatch, capsys):
    """The one thing a user on a phone needs and cannot guess: the URL of the page."""
    import importlib

    from fastapi.testclient import TestClient

    import jarvis.security.auth as auth_module

    app_module = importlib.import_module("jarvis.api.app")
    monkeypatch.setattr(auth_module, "interface_addresses",
                        lambda: {"192.168.1.40", "127.0.0.1"})
    settings = Settings(lan_mode=True, host="0.0.0.0", auth_enabled=True,
                        api_token=get_settings().api_token)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    with TestClient(app_module.app, base_url="http://127.0.0.1:8765"):
        out = capsys.readouterr().out
    assert "http://192.168.1.40:8765/phone" in out, out


# ---------------------------------------------------------------------------
# 2. the phone's side of the door
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """The same app instance the desktop uses, addressed as if from the laptop itself."""
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    with TestClient(app, base_url="http://127.0.0.1:8765",
                    headers={"X-JARVIS-Token": get_settings().api_token}) as test_client:
        yield test_client


def test_the_phone_page_is_served_and_asks_for_no_secrets(client):
    body = client.get("/phone").text
    assert "Pair with this laptop" in body
    assert "/devices/pair/request" in body
    assert "Approve" in body and "Deny" in body


def test_the_phone_page_never_carries_the_laptop_token(client):
    """The page is served before the phone is trusted, so anything in it is readable by
    whoever can reach the port. A token in the HTML would make the page the key."""
    import re

    body = client.get("/phone").text
    token = get_settings().api_token or ""
    assert token, "the test server should have a token to keep out of the page"
    assert token not in body
    assert not re.search(r"api_token\s*[:=]\s*['\"][^'\"]{8,}", body), body


def test_the_phone_page_reads_the_credential_the_login_actually_returns(client):
    """Page JS is untested code until something treats its field names as a contract.

    Opened in a real browser, this page logged in successfully and then saw 401 on
    every later call: it read `.token` while the API hands out `session_token`, so the
    credential was silently dropped.
    """
    import re

    body = client.get("/phone").text
    bootstrap = re.search(r'name="jarvis-bootstrap" content="([^"]+)"', body).group(1)
    issued = client.post("/auth/session", json={"bootstrap": bootstrap},
                         headers={"Sec-Fetch-Site": "same-origin"}).json()
    key = next((k for k, v in issued.items()
                if isinstance(v, str) and v and k.endswith("token")), None)
    assert key, issued
    assert f").{key}" in body, f"the page never reads the {key!r} it was handed"


def test_a_phone_can_ask_to_be_paired_and_the_laptop_sees_it(client):
    requested = client.post("/devices/pair/request",
                            json={"device_id": "phone_test", "device_name": "Pixel"}).json()
    pairing_id = requested.get("pairing_id") or (requested.get("request") or {}).get("pairing_id")
    assert pairing_id, requested
    pending = client.get("/devices/pair/pending").json()
    rows = pending if isinstance(pending, list) else (pending.get("pairs") or pending.get("pending"))
    assert any(r.get("pairing_id") == pairing_id for r in rows), pending


def test_the_phone_page_uses_the_protocol_that_already_exists(client):
    """The client must speak the endpoints the laptop already exposes, not a parallel
    invented one -- that is how a second, ungated path gets created by accident."""
    body = client.get("/phone").text
    for path in ("/devices/pair/request", "/devices/pair/pending", "/confirm/", "/command"):
        assert path in body, path


def test_the_laptop_answers_the_phone_address_it_just_advertised(monkeypatch):
    """Found in a real browser, not from the tests.

    The page loaded on a phone, its first call went to `Origin: http://172.20.169.34:8791`
    -- the exact address the server printed -- and the API answered 403 "Origin not
    allowed.". Advertising an address and then refusing requests from it is worse than
    not opening the door, so the accepted origins and the advertised URLs are derived
    from one list.
    """
    import importlib

    from fastapi.testclient import TestClient

    import jarvis.security.auth as auth_module

    app_module = importlib.import_module("jarvis.api.app")

    monkeypatch.setattr(auth_module, "interface_addresses",
                        lambda: {"192.168.1.40", "127.0.0.1"})
    settings = Settings(lan_mode=True, host="0.0.0.0", port=8791, auth_enabled=True,
                        api_token=get_settings().api_token)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)

    origin = "http://192.168.1.40:8791"
    assert origin in settings.allowed_origins(), settings.allowed_origins()
    assert any(origin in url for url in settings.reachable_urls()), \
        f"advertised {settings.reachable_urls()} but would not serve it"

    with TestClient(app_module.app, base_url=origin,
                    headers={"Origin": origin,
                             "X-JARVIS-Token": get_settings().api_token}) as probe:
        refused = probe.post("/auth/session", json={"bootstrap": "not-a-real-credential"})
        assert "Origin not allowed" not in refused.text, refused.text


# ---------------------------------------------------------------------------
# 3. "check Qoder is working"
# ---------------------------------------------------------------------------


def test_the_agent_probe_is_read_only_and_unasked(core):
    spec = core.tools.get("check_agent_status")
    assert spec is not None, "the probe must exist"
    assert spec.risk is RiskLevel.READ
    action = core.tools.get("check_agent_status")
    from jarvis.core.models import PlannedAction
    verdict = core.policy.evaluate(PlannedAction(tool="check_agent_status",
                                                 params={"app": "Qoder"},
                                                 device="laptop_main"))
    assert verdict.decision is PolicyDecision.EXECUTE, verdict.reason


@pytest.fixture()
def bundle_root(tmp_path, monkeypatch):
    """A stand-in for /Applications.

    Without it these tests report what this particular machine has installed: the CI
    runner has no `Qoder.app` and honestly answered `found: false`, which was the test's
    fault, not the probe's.
    """
    from jarvis.tools import agents as agents_module

    root = tmp_path / "Applications"
    root.mkdir()
    monkeypatch.setattr(agents_module, "BUNDLE_ROOTS", (str(root),))
    return root


def test_the_agent_probe_reports_the_three_answers_that_are_asked(core, bundle_root,
                                                                  monkeypatch):
    """Installed? which version? running now? Anything vaguer is a shrug."""
    async def fake_run(argv, **kw):
        if argv[:2] == ["mdls", "-name"]:
            return 0, "Qoder 1.4.2 (build 4210)\n", ""
        if argv[0] == "ps":
            return 0, "  4242 ??  3:11.02 /Applications/Qoder.app/Contents/MacOS/Electron\n", ""
        return 1, "", "not found"

    from jarvis.tools import agents as agents_module

    (bundle_root / "Qoder.app").mkdir()
    monkeypatch.setattr(agents_module, "run_command", fake_run)
    result = asyncio.run(core.tools.handler("check_agent_status")(
        _ctx(core), app="Qoder"))
    assert result.ok is True, result.error
    assert result.output["found"] is True, result.output
    assert "1.4.2" in str(result.output.get("version")), result.output
    assert result.output["running"] is True, result.output
    assert "Qoder" in result.message


def test_a_name_ending_in_a_keeps_its_own_identity_when_matching_processes(
        core, bundle_root, monkeypatch):
    """`rstrip(".app")` strips *characters*, not the suffix, so "Opera.app" became the
    needle "oper" and "Arena" became "aren" -- a running app reported as not running."""
    from jarvis.tools import agents as agents_module

    (bundle_root / "Arena.app").mkdir()

    async def fake_run(argv, **kw):
        if argv[:2] == ["mdls", "-name"]:
            return 0, "Version 2.0.1\n", ""
        return 0, "  77 /Applications/Arena.app/Contents/MacOS/Arena\n", ""

    monkeypatch.setattr(agents_module, "run_command", fake_run)
    result = asyncio.run(core.tools.handler("check_agent_status")(
        _ctx(core), app="Arena"))
    assert result.output["running"] is True, result.output


def test_the_agent_probe_says_no_instead_of_guessing(core, bundle_root, monkeypatch):
    from jarvis.tools import agents as agents_module

    async def nothing(argv, **kw):
        return 1, "", "no such file"

    monkeypatch.setattr(agents_module, "run_command", nothing)
    result = asyncio.run(core.tools.handler("check_agent_status")(
        _ctx(core), app="Figma"))
    assert result.output["found"] is False, result.output
    assert "Figma" in result.message and "not" in result.message.lower(), result.message
    assert "1." not in result.message, "no version may be invented for an absent app"


def test_the_probe_refuses_a_name_that_could_escape_its_own_bounds(core):
    """The argument becomes part of a `ps` match, not a shell command -- but a value
    carrying a newline would still corrupt the report, so it is refused plainly."""
    result = asyncio.run(core.tools.handler("check_agent_status")(
        _ctx(core), app="Qoder\n--flags"))
    assert result.ok is False, result.error or result.message
    said = (result.error or result.message).lower()
    assert "app name" in said or "cannot look up" in said, said


def test_a_sentence_asks_the_probe_in_plain_words(core, monkeypatch):
    from jarvis.tools import agents as agents_module

    async def fake_run(argv, **kw):
        if argv[:2] == ["mdls", "-name"]:
            return 0, "Version 1.4.2\n", ""
        return 0, "", ""

    monkeypatch.setattr(agents_module, "run_command", fake_run)
    response = asyncio.run(core.handle(CommandRequest(
        text="is qoder working", source="phase35")))
    assert [a.tool for a in response.actions] == ["check_agent_status"], response.reply


def _ctx(core):
    from jarvis.tools.base import ToolContext
    return ToolContext(device_id="laptop_main", session_id="p35")


# ---------------------------------------------------------------------------
# 4. the address must survive being piped to a log
# ---------------------------------------------------------------------------

# GitHub's macOS runners were measured ~7x slower than this machine at the same
# import workload (257 s suite vs 38 s), so a boot deadline sized for the laptop
# reads as "it refused without saying why" on a runner that is still importing.
_SERVER_TIMEOUT = 180.0


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _server_env(home, **overrides):
    import os

    env = dict(os.environ)
    env["JARVIS_HOME"] = str(home)
    env["JARVIS_LOG_LEVEL"] = "WARNING"
    env["JARVIS_API_TOKEN"] = "phase35-subprocess-token"
    env.update({f"JARVIS_{k.upper()}": str(v) for k, v in overrides.items()})
    return env


def _read_until(process, needle, timeout=_SERVER_TIMEOUT):
    """Collect stdout lines until `needle` appears, or give up with what was seen.

    A daemon thread does the blocking read: `readline()` on a pipe ignores any
    deadline the caller sets, so a child that buffers its stdout would hang the
    suite instead of failing this test.
    """
    import threading
    import time

    lines: list = []
    closed = []

    def drain():
        for line in process.stdout:
            lines.append(line.rstrip())
        closed.append(True)

    threading.Thread(target=drain, daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = "\n".join(lines)
        if needle in text:
            return True, text
        if closed:
            return False, text  # the child exited without ever saying it
        time.sleep(0.05)
    return False, "\n".join(lines)


def test_the_real_server_refuses_an_unsafe_lan_start(tmp_path):
    """run.py is the entry point the desktop app uses; a guard that only TestClient
    consults would let the shipped product boot anyway."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=str(root),
        env=_server_env(tmp_path, lan_mode=1, host="127.0.0.1", port=_free_port()),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Drained concurrently: waiting for exit first would deadlock if the child
        # filled the pipe while nobody was reading it.
        found, seen = _read_until(process, "JARVIS_HOST")
        code = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        pytest.fail(f"the server neither refused nor booted within "
                    f"{_SERVER_TIMEOUT:.0f} s; output so far:\n{seen[-800:]}")
    finally:
        process.kill()
    assert code != 0, f"an unsafe LAN start exited successfully:\n{seen[-1500:]}"
    assert found, f"it refused without saying why:\n{seen[-1500:]}"


def test_the_phone_address_reaches_a_piped_log(tmp_path):
    """The user reads this line to know what to type; block-buffered stdout hides it."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=str(root),
        env=_server_env(tmp_path, lan_mode=1, host="0.0.0.0", port=port),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        found, seen = _read_until(process, "/phone")
        assert found, f"no phone address reached the pipe:\n{seen[-1500:]}"
        assert f":{port}/phone" in seen, seen
        assert "0.0.0.0:" not in seen, f"an untypeable address was advertised:\n{seen}"
    finally:
        process.kill()
        process.wait(timeout=10)


# ---------------------------------------------------------------------------
# 5. the page's own code, run against the payloads the API really returns
# ---------------------------------------------------------------------------

import re
import shutil
import subprocess
from pathlib import Path

node = shutil.which("node")
PAGE = Path(__file__).resolve().parents[1] / "dashboard" / "phone.html"


def _run_page_helper(payload):
    """Feed a real API response through the shipped `rowsOf` and see what the phone gets."""
    source = re.search(r"function rowsOf\(data, key\) \{.*?\n\}",
                      PAGE.read_text(), re.S).group(0)
    script = (source + "\nconsole.log(JSON.stringify(rowsOf("
              + json.dumps(payload) + ", 'x')));\n")
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:400]
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not node, reason="node is not available to run the page's own code")
def test_the_page_sees_the_confirmation_the_laptop_is_actually_holding(client):
    """An object-of-lists was assumed; both endpoints answer with a bare list.

    `pending.confirmations` on a list is `undefined`, so the phone rendered "nothing
    pending" beside a confirmation that was really waiting -- the approve-from-the-phone
    feature was silently absent rather than broken loudly.
    """
    sent = client.post("/command", json={"text": "turn on the living room lights",
                                         "source": "phase35_page"}).json()
    assert sent.get("reply_kind") == "confirm" or "confirm" in json.dumps(sent).lower(), sent
    pending = client.get("/confirmations").json()
    assert isinstance(pending, list) and pending, pending
    rows = _run_page_helper(pending)
    assert any(r.get("tool") == "control_home_device" for r in rows), rows


@pytest.mark.skipif(not node, reason="node is not available to run the page's own code")
def test_the_page_can_find_its_own_device_among_the_laptops_rows(client):
    """The pairing line used `devices.devices`, so a phone could never read its own
    state no matter what the laptop knew about it."""
    devices = client.get("/devices").json()
    assert isinstance(devices, list), type(devices)
    rows = _run_page_helper(devices)
    assert any(r.get("device_id") == "laptop_main" for r in rows), rows
    assert all("auth_state" in r for r in rows), "the page reads this to say paired"
