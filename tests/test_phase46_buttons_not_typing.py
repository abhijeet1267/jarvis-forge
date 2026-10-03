"""Phase 46: buttons, not typing.

Both clients were screenshotted before anything changed, and the screenshots said the same
thing about each: one text box, and a wall of empty space under it. Everything the laptop can
do -- 134 tools -- was reachable only by typing a sentence a person has to invent, and the
phone page, the one device someone is actually holding, had three buttons on it.

Two other things were visible in the same captures and are treated as defects, not taste:

* The device list showed **thirteen** entries, eight of them named `phone_pair_a`,
  `phone_twice`, `phone_perm`, `phone_gone` -- residue of my own live test drives against
  this machine, sitting in the user's real data directory with no way to dismiss them.
* The phone page hard-codes its colours (`#0d1117`, `#161b22`, `#238636`) while the laptop
  panel moved every colour into named tokens two phases ago. Two spellings of one palette is
  how a theme ends up half-applied.

The rule the quick actions are built to: a button may only exist for a capability the registry
actually has, so the interface cannot advertise something the system has since lost.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parents[1] / "dashboard"
node = shutil.which("node")


def _registry(client):
    body = client.get("/tools").json()
    return {t["name"] for t in body}


@pytest.fixture()
def client(core):
    import importlib

    from fastapi.testclient import TestClient

    from jarvis.config import get_settings

    app_module = importlib.import_module("jarvis.api.app")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8765",
                    headers={"X-JARVIS-Token": get_settings().api_token}) as test_client:
        yield test_client


def _quick_actions(source: str) -> list[dict]:
    """The QUICK array a page declares, parsed out of its own source."""
    block = re.search(r"const QUICK = \[(.*?)\n\];", source, re.S)
    assert block, "the page declares no quick actions"
    return [{"label": m.group(1), "tool": m.group(2), "say": m.group(3)} for m in
            re.finditer(r'\{ label: "([^"]+)", tool: "([^"]+)", say: "([^"]*)" \}',
                        block.group(1))]


# ---------------------------------------------------------------------------
# 1. a button only exists for a capability that does
# ---------------------------------------------------------------------------


#: What a `{slot}` in a button's sentence gets filled with when this test has to
#: choose, so the routing below is a fact about the shipped page and not about luck.
FILL = {"site": "github", "name": "report", "subject": "a firewall"}


def _filled(say: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: FILL[m.group(1)], say)


@pytest.mark.parametrize("page", ["index.html", "phone.html"])
def test_every_quick_action_names_a_real_tool(client, page):
    source = (DASH / page).read_text()
    actions = _quick_actions(source)
    assert actions, f"{page} offers nothing to tap"
    names = _registry(client)
    missing = [a["tool"] for a in actions if a["tool"] not in names]
    assert not missing, f"{page} advertises tools the registry does not have: {missing}"


@pytest.mark.parametrize("page", ["index.html", "phone.html"])
def test_every_button_says_a_sentence_the_planner_routes(page):
    """The registry check is not enough: a button sends a *sentence*, and a sentence
    that routes to another tool is a button that does the wrong thing.

    Measured before this existed: nothing connected a QUICK label to a route, so the
    first draft of the Ask button said "ask about {subject}" -- which parses to
    `unknown`. Two candidates were then compared against the real planner on this build:
    "what is {subject}" routes, but only takes a short noun phrase, while
    "explain {subject}" routes and keeps a whole clause as the topic. The shipped
    sentence is the one that was measured to survive a person's wording.
    """
    from jarvis.core.planner import RulePlanner
    from jarvis.tools import build_default_registry

    planner = RulePlanner(build_default_registry())
    for action in _quick_actions((DASH / page).read_text()):
        text = _filled(action["say"])
        plan = planner.parse(text)
        got = [a.tool for a in plan.actions]
        assert action["tool"] in got, (
            f'{page}: the "{action["label"]}" button sends {text!r}, which routes to '
            f"{got or 'nothing'} ({plan.reply_kind or 'answered'}), not to {action['tool']}")


@pytest.mark.parametrize("page", ["index.html", "phone.html"])
def test_a_button_with_a_blank_asks_instead_of_guessing(page):
    """`open {site}` is not a command; the page has to ask what site.

    A literal `{site}` reaching /command would open a tab on a domain made of
    punctuation, which is the kind of thing the honest refusal path exists for.
    """
    source = (DASH / page).read_text()
    assert re.search(r"\{[a-z]+\}", source), f"{page} declares no argument slot"
    assert 'replace(/\\{(\\w+)\\}/g' in source, (
        f"{page} sends a slot without filling it from something the person typed")


@pytest.mark.parametrize("page", ["index.html", "phone.html"])
def test_the_page_hides_an_action_whose_tool_is_gone(page):
    """The guard has to be in the page, not only in this test.

    A list rendered unconditionally is a UI that can lie the day a tool is removed, and
    the registry is the thing that changes.
    """
    source = (DASH / page).read_text()
    assert re.search(r"QUICK\.filter\(", source), (
        "the page renders QUICK without checking it against /tools")
    assert re.search(r"indexOf\(a\.tool\)|includes\(a\.tool\)|names\.has\(a\.tool\)", source), (
        "the check is not against the tool names the server returned")


#: The four things they asked for in their own words, and the sentence each one turned
#: into. Checked per page: the phone is the device a person is actually holding, so it
#: gets no exemption from having a button for the thing they asked for.
WANTED = {
    "check_storage": "if i say check storege",
    "ask_question": "when i ask qns it give ans",
    "open_project_folder": "open my project",
    "find_and_open": "serch my .... file and open it",
    "battery_status": "what is my battery",
}


@pytest.mark.parametrize("page", ["index.html", "phone.html"])
def test_the_actions_cover_what_the_reader_asks_for(page):
    tools = {a["tool"] for a in _quick_actions((DASH / page).read_text())}
    missing = [f"{tool} ({WANTED[tool]})" for tool in WANTED if tool not in tools]
    assert not missing, f"{page} has no button for: {', '.join(missing)}"


# ---------------------------------------------------------------------------
# 2. the microphone button is honest about having no microphone
# ---------------------------------------------------------------------------


def test_the_phone_page_has_a_mic_button():
    source = (DASH / "phone.html").read_text()
    assert 'id="mic"' in source, "there is no microphone control on the phone"


def test_the_laptop_says_why_it_cannot_hear(client):
    """A disabled button needs a reason, and only the laptop knows what it is.

    Measured on this machine: `can_listen` is False because no speech backend works
    here. The page could say "unavailable", which is a shrug, or read the sentence the
    voice layer already writes for the failed-transcription path -- which names the three
    remedies in order. That sentence was reachability-only-on-error until this check.
    """
    stt = client.get("/voice/status").json()["stt"]
    assert "can_listen" in stt, stt.keys()
    if not stt["can_listen"]:
        assert stt["advice"], "the laptop reports it cannot listen and will not say why"
        assert "Speech Recognition" in stt["advice"], stt["advice"]


def test_the_mic_is_disabled_until_the_laptop_says_it_can_listen():
    """Phase 43 made `can_listen` truthful; this is the control that has to read it."""
    source = (DASH / "phone.html").read_text()
    assert "/voice/status" in source, "the page never asks whether there are ears"
    assert "stt.can_listen" in source, "the page reads the voice state but not the ear flag"
    assert re.search(r"mic\.disabled\s*=\s*!", source), (
        "the microphone button is never gated on anything")
    assert "micline" in source, "a disabled button with no reason is a broken button"
    assert "browserHasMic" in source, (
        "the page assumes the laptop is the only thing standing between a person and a mic")


def test_the_disabled_mic_states_the_reason_without_dumping_the_remedy():
    """Measured in a real browser at 375px: the server's advice is one 530-character
    sentence, and the first version of this control printed all of it under a disabled
    button -- 101px of text wall, which is the page making a reader scroll past a
    paragraph to reach the box they were trying to use.

    The line that stays visible is the reason. The remedy opens on demand.
    """
    source = (DASH / "phone.html").read_text()
    assert 'createElement("details")' in source, "the remedy has nowhere to go"
    assert 'createElement("summary")' in source
    assert '<p class="sub" id="micline">' not in source, (
        "a block element cannot live inside a <p>; the container has to be a div")
    assert 'micNote("the laptop has no working microphone.", earReason, "warn")' in source, (
        "the visible line is not the short reason")


def test_long_server_text_cannot_widen_the_phone_page():
    """Measured in the same browser pass: opening the remedy put 627px of page inside a
    375px viewport, because the advice names a marker path and a path is one word.

    A phone page that scrolls sideways loses whatever the reader was reaching for.
    """
    source = (DASH / "phone.html").read_text()
    assert re.search(r"#micline \{[^}]*overflow-wrap:\s*anywhere", source, re.S), (
        "server text is not breakable, so one long path widens the whole page")
    assert re.search(r"#micline \{[^}]*min-width:\s*0", source, re.S), (
        "a flex item refuses to shrink below its content")


@pytest.mark.skipif(not node, reason="node is needed to parse the shipped script")
def test_the_phone_script_still_parses():
    source = (DASH / "phone.html").read_text()
    script = re.search(r"<script>(.*?)</script>", source, re.S).group(1)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        probe = handle.name
    try:
        result = subprocess.run([node, "--check", probe], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    finally:
        Path(probe).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. the devices I created can be dismissed
# ---------------------------------------------------------------------------


def test_the_dashboard_can_forget_a_device(client):
    """Eight of thirteen devices on this machine are mine, from live test drives.

    They are offline forever, and the page had no way to remove them, which turns the
    first card a user sees into a list of somebody else's leftovers.
    """
    source = (DASH / "index.html").read_text()
    assert "/revoke" in source, "the dashboard cannot forget a device"
    assert "is_host" in source, (
        "nothing stops the page offering to forget the laptop itself")


def test_the_device_list_says_which_device_is_the_host(client):
    """The page must not hard-code a device id to know what it is standing on.

    Measured: 2026-10-03, `POST /devices/laptop_main/revoke` returned 200 against the
    live app and set the host's own record to `revoked`. A page that decides which row
    is "this machine" from a string it invented cannot be trusted to get that right, and
    the server is the only component that knows.
    """
    rows = client.get("/devices").json()
    hosts = [d["device_id"] for d in rows if d["is_host"]]
    assert hosts == ["laptop_main"], hosts


def test_the_revoke_route_refuses_the_laptop_and_changes_nothing(client):
    """The refusal has to be a refusal, and it has to happen before the write.

    A 200 that leaves the record alone would still be a lie about what happened, and a
    404 would say "no such device" about a device the same page lists -- which is how the
    route was written the first time: every `ValueError` from the pairing layer became 404.
    """
    names = {r.path for r in client.app.routes if hasattr(r, "path")}
    assert "/devices/{device_id}/revoke" in names

    before = {d["device_id"]: d for d in client.get("/devices").json()}["laptop_main"]
    response = client.post("/devices/laptop_main/revoke")
    assert response.status_code == 409, response.text

    after = {d["device_id"]: d for d in client.get("/devices").json()}["laptop_main"]
    assert after["auth_state"] == before["auth_state"], "the host's trust state changed"
    assert after["auth_state"] != "revoked", "the laptop's own device record was revoked"
    assert after["online"] == before["online"], "the host was marked offline by a refusal"


def test_pairing_refuses_to_revoke_the_host_directly():
    """The guard belongs under the route, because the route is not the only caller.

    `remove_phone` calls the same layer, and today it escapes the hole by accident: it
    refuses any device that is not paired, and the laptop never was. A guard that only
    lives in the HTTP layer is one tool call away from being the bug again.
    """
    from jarvis.devices.pairing import HostDeviceError, PairingManager

    with pytest.raises(HostDeviceError):
        PairingManager().revoke("laptop_main")


def test_an_unknown_device_is_still_404(client):
    """The refusal must not swallow the case that used to share its exception."""
    response = client.post("/devices/not_a_device_here/revoke")
    assert response.status_code == 404, response.text


def test_a_paired_device_can_still_be_revoked(client, core):
    """The guard is about the host, not about revoke itself.

    Forgetting a phone has to keep working, or the eight leftover rows get worse, not better.
    """
    from jarvis.core.models import DeviceSpec
    from jarvis.devices.pairing import PairingManager
    from jarvis.registry.devices import get_device_registry

    registry = get_device_registry()
    manager = PairingManager()
    registry.register(DeviceSpec(device_id="phone_forget_me", type="android",
                                 platform="android", capabilities=["file_receive"]))
    secret_path = manager.store_secret("phone_forget_me", b"x" * 32)
    registry.set_auth_state("phone_forget_me", "paired", key_ref=str(secret_path))
    try:
        result = manager.revoke("phone_forget_me")
        assert result["state"] == "revoked"
        assert not secret_path.exists(), "the key survived the revoke"
        row = {d["device_id"]: d for d in client.get("/devices").json()}["phone_forget_me"]
        assert row["auth_state"] == "revoked", row
    finally:
        core.db.execute("DELETE FROM devices WHERE device_id = ?", ("phone_forget_me",))
        # The registry has no delete; a row left in its cache would show up as a phantom
        # device for every later test in the session.
        registry._cache.pop("phone_forget_me", None)
        registry.refresh()


# ---------------------------------------------------------------------------
# 4. one palette, two devices
# ---------------------------------------------------------------------------


def test_the_tool_registry_can_be_filtered():
    """134 rows in a table is a wall, and the wall was the only way to see the tools.

    The filter box has to sit in the markup rather than be written by the render pass:
    repainting the card on every keystroke drops the caret, which is the same
    detach-the-node bug the phone page already records for its Approve button.
    """
    source = (DASH / "index.html").read_text()
    assert re.search(r'<div class="toolbar">\s*<input id="toolFilter"', source), (
        "the filter box is not static markup")
    assert 'id="toolCount"' in source, "the page never says how many tools matched"
    assert re.search(r'getElementById\("toolFilter"\)\.addEventListener\(\s*"input"', source), (
        "typing in the filter does nothing")


def test_a_forgotten_device_reads_as_forgotten():
    """Measured in the browser: after the Forget button did its job, the row still said
    OFFLINE. That word means "not here right now", which is exactly what a revoked
    device is not -- it is untrusted until it pairs again.

    The check is on order, not on the word existing: the revoked branch has to be
    reached before the online/offline one, or the row keeps falling through to a lie.
    """
    source = (DASH / "index.html").read_text()
    revoked = re.search(r'auth_state === "revoked" \? "revoked"', source)
    online = re.search(r'd\.online \? "online"', source)
    assert revoked and online, "the devices card has no revoked branch at all"
    assert revoked.start() < online.start(), "a revoked device falls through to offline"


def test_a_filter_that_matches_nothing_says_so():
    """An empty table reads as a broken registry rather than as a search that missed."""
    source = (DASH / "index.html").read_text()
    assert re.search(r"no tool matches", source), "a missed filter renders blank silence"


def test_the_phone_page_uses_named_tokens_like_the_laptop_page():
    """The laptop panel moved every colour into `:root` in Phase 39; the phone did not.

    Checked on the rule text, not the value list: after the token block is removed from
    the stylesheet, no colour literal may survive anywhere else in it.
    """
    phone = (DASH / "phone.html").read_text()
    style = re.search(r"<style>(.*?)</style>", phone, re.S)
    assert style, "the phone page has no stylesheet to inspect"
    root = re.search(r":root \{(.*?)\}", style.group(1), re.S)
    assert root, "the phone page declares no token block"
    tokens = re.findall(r"--([\w-]+):", root.group(1))
    assert tokens, "the token block is empty"
    rest = style.group(1).replace(root.group(0), "")
    stray = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rest)
    assert not stray, f"colours outside the token block: {stray[:6]}"


def test_the_two_pages_spell_the_palette_the_same_way():
    """Two spellings of one palette is how a theme ends up half-applied."""
    names = []
    for page in ("index.html", "phone.html"):
        block = re.search(r":root \{(.*?)\}", (DASH / page).read_text(), re.S)
        assert block, f"{page} declares no token block"
        names.append(set(re.findall(r"--([\w-]+):", block.group(1))))
    shared = {"bg", "panel", "ink", "muted", "line", "accent", "ok", "warn", "bad", "radius"}
    for page, token_set in zip(("index.html", "phone.html"), names):
        missing = shared - token_set
        assert not missing, f"{page} is missing token names the other page uses: {sorted(missing)}"


def test_the_phone_page_handles_the_states_a_button_can_be_in():
    """Hover, focus, disabled and reduced motion -- the panel has them, the phone didn't."""
    source = (DASH / "phone.html").read_text()
    for state in (":hover", ":focus-visible", ":disabled", "prefers-reduced-motion",
                  "aria-label"):
        assert state in source, f"the phone page never handles {state}"
