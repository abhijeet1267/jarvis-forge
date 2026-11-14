"""Phase 36: two applications that can actually be installed and connected.

The ask was blunt: "make 1st two applications 1 for mobile and 1 for laptop so i can connect
and work". Measured against what this machine can build and verify:

```
app/launcher.py:46   project_path.txt holds an ABSOLUTE path  -> the .app breaks when moved;
                    build_app.py:125 copies no source          -> it was never an application,
                    only a shortcut to this checkout (the Phase 10 failure mode, still live)
app/launcher.py      no way to turn LAN on from the window     -> 35-D: connecting a phone
                    requires exporting a shell variable, which a double-clicked app never sees
dashboard/phone.html no manifest, no icons, no service worker  -> a browser tab, not an app
                    an Android APK                              -> no SDK, no adb, no device here
```

So the two applications delivered are: a Mac app that carries its own source and opens its own
door, and a phone app installed from the laptop through the browser's own install path ("Add to
Home Screen"), which is the only installable artifact that can be built *and verified* here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

node = shutil.which("node")

import pytest
from jarvis.config import Settings, get_settings


# ---------------------------------------------------------------------------
# 1. the laptop app's own switch: connect a phone without opening a terminal
# ---------------------------------------------------------------------------


def test_a_file_in_the_users_folder_turns_the_door_on(tmp_path, monkeypatch):
    """A double-clicked app does not inherit shell exports, so `JARVIS_LAN_MODE=1` is not
    an option for a user who never opens a terminal. The setting has to live in the
    laptop's own data directory, where the app can write it."""
    from jarvis.config import set_lan_mode

    monkeypatch.delenv("JARVIS_LAN_MODE", raising=False)
    set_lan_mode(tmp_path, True)
    assert Settings(home=tmp_path).lan_mode is True


def test_the_switch_can_be_turned_back_off(tmp_path, monkeypatch):
    from jarvis.config import lan_mode_state, set_lan_mode

    monkeypatch.delenv("JARVIS_LAN_MODE", raising=False)
    set_lan_mode(tmp_path, True)
    set_lan_mode(tmp_path, False)
    assert lan_mode_state(tmp_path) is False
    assert Settings(home=tmp_path).lan_mode is False


def test_no_switch_file_means_the_door_stays_shut(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_LAN_MODE", raising=False)
    assert Settings(home=tmp_path).lan_mode is False, "silence must not mean reachable"


def test_an_explicit_environment_value_outranks_the_app_switch(tmp_path, monkeypatch):
    """Someone debugging from a shell must be able to overrule what the window wrote."""
    from jarvis.config import set_lan_mode

    monkeypatch.setenv("JARVIS_LAN_MODE", "false")
    set_lan_mode(tmp_path, True)
    assert Settings(home=tmp_path).lan_mode is False


def test_the_app_switch_cannot_slip_past_the_startup_guard(tmp_path, monkeypatch):
    """Convenience must not become a way around the safety check: switching LAN on from
    the app still has to fail loudly when there is no token to gate it."""
    from jarvis.config import set_lan_mode
    from jarvis.security.auth import lan_startup_problems

    monkeypatch.delenv("JARVIS_LAN_MODE", raising=False)
    set_lan_mode(tmp_path, True)
    settings = Settings(home=tmp_path, auth_enabled=False, host="0.0.0.0")
    assert settings.lan_mode is True
    assert lan_startup_problems(settings), "the app's own switch must not bypass the guard"


def test_the_switch_file_is_not_a_place_for_shell(tmp_path):
    """It is read as a boolean, not evaluated: a file containing anything else is off,
    and a huge or binary file must not become a parser's problem."""
    from jarvis.config import lan_mode_state, set_lan_mode

    (tmp_path / "lan_mode").write_text("true; rm -rf /", encoding="utf-8")
    assert lan_mode_state(tmp_path) is None
    set_lan_mode(tmp_path, True)
    assert (tmp_path / "lan_mode").read_text(encoding="utf-8").strip() == "true"


def test_no_http_route_can_open_the_door(client_factory):
    """The phone is served the same dashboard as the laptop, so a settings endpoint that
    accepted `lan_mode` would let a connected device grant itself remote access to the
    laptop. The switch belongs to the desktop app, which has no network surface."""
    client = client_factory()
    body = {"lan_mode": True, "host": "0.0.0.0"}
    for path in ("/settings", "/config", "/admin/settings", "/system/lan"):
        response = client.post(path, json=body)
        assert response.status_code in (404, 405), f"{path} accepted a settings write"


# ---------------------------------------------------------------------------
# 2. the laptop app travels with its own code
# ---------------------------------------------------------------------------


def _fake_bundle(tmp_path, *, recorded: str, carries_source: bool):
    """Lay out `Something.app/Contents/{Resources,MacOS}` the way build_app.py does."""
    contents = tmp_path / "Test.app" / "Contents"
    resources = contents / "Resources"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    (resources / "project_path.txt").write_text(recorded, encoding="utf-8")
    if carries_source:
        staged = resources / "app"
        (staged / "jarvis").mkdir(parents=True)
        (staged / "run.py").write_text("# staged\n", encoding="utf-8")
    return contents


def test_a_moved_bundle_still_finds_its_own_code(tmp_path, monkeypatch):
    """The Phase 10 finding, still true in Phase 35: the recorded path is absolute, so
    copying the app anywhere -- or deleting the checkout it points at -- leaves an icon
    that exits 1. A bundle that carries its source must prefer it."""
    launcher = _launcher_module()
    monkeypatch.delenv("JARVIS_PROJECT_DIR", raising=False)

    other = tmp_path / "elsewhere"
    contents = _fake_bundle(tmp_path / "moved", recorded=str(other), carries_source=True)
    monkeypatch.setattr(launcher, "BUNDLE_ROOT", contents)
    assert launcher.resolve_project_dir() == (contents / "Resources" / "app").resolve()


def test_a_bundle_without_staged_source_still_uses_the_recorded_path(tmp_path, monkeypatch):
    """Development builds copy no source; they must keep working exactly as before."""
    launcher = _launcher_module()
    monkeypatch.delenv("JARVIS_PROJECT_DIR", raising=False)
    real = tmp_path / "checkout"
    (real / "jarvis").mkdir(parents=True)
    (real / "run.py").write_text("", encoding="utf-8")
    contents = _fake_bundle(tmp_path / "dev", recorded=str(real), carries_source=False)
    monkeypatch.setattr(launcher, "BUNDLE_ROOT", contents)
    assert launcher.resolve_project_dir() == real


def test_build_staging_copies_the_code_the_app_runs(tmp_path):
    """Not a grep of build_app.py: the staging step itself, called and inspected."""
    import importlib

    build_app = importlib.import_module("build_app")
    stage = getattr(build_app, "stage_project", None)
    assert stage is not None, "build_app must expose the copy step so it can be tested"
    target = tmp_path / "Resources"
    stage(target)
    assert (target / "app" / "run.py").exists()
    assert (target / "app" / "jarvis" / "config.py").exists()
    assert (target / "app" / "dashboard" / "phone.html").exists()


def test_the_staged_copy_excludes_the_venv_and_the_tests(tmp_path):
    """Copying the checkout wholesale would ship a 300 MB interpreter and every cache in
    the tree inside an application bundle."""
    import importlib

    build_app = importlib.import_module("build_app")
    target = tmp_path / "Resources"
    build_app.stage_project(target)
    staged = target / "app"
    assert not (staged / ".venv").exists()
    assert not (staged / "tests").exists()
    assert not (staged / "dist").exists()
    assert not list(staged.rglob("__pycache__"))


def _launcher_module():
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "app"))
    import launcher  # noqa: WPS433 - the bundle's own module, not a package member

    return launcher


# ---------------------------------------------------------------------------
# 3. the phone app: installed through the browser, because no APK can be built here
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_factory():
    def build():
        from fastapi.testclient import TestClient

        from jarvis.api.app import app

        return TestClient(app, base_url="http://127.0.0.1:8765",
                          headers={"X-JARVIS-Token": get_settings().api_token})

    return build


@pytest.fixture()
def client(client_factory):
    with client_factory() as test_client:
        yield test_client


def test_the_phone_page_tells_the_browser_it_is_an_app(client):
    body = client.get("/phone").text
    assert 'rel="manifest"' in body, "without a manifest the browser offers no install"
    assert "apple-touch-icon" in body, "iOS installs from that link, not the manifest"
    assert 'name="theme-color"' in body


def test_the_manifest_promises_a_standalone_app_not_a_browser_tab(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200, response.status_code
    assert "manifest" in response.headers["content-type"], response.headers["content-type"]
    manifest = response.json()
    assert manifest["display"] == "standalone", manifest["display"]
    assert manifest["start_url"], "an app with nowhere to launch is not installable"
    assert manifest["icons"], "no icon, no home-screen tile"


def test_every_icon_the_manifest_promises_is_served_as_an_image(client):
    manifest = client.get("/manifest.webmanifest").json()
    for icon in manifest["icons"]:
        response = client.get(icon["src"])
        assert response.status_code == 200, icon
        assert response.headers["content-type"].startswith("image/png"), icon
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        assert len(response.content) > 1000, f"{icon['src']} is a stub"


def test_the_app_launches_onto_a_page_that_answers(client):
    manifest = client.get("/manifest.webmanifest").json()
    start = manifest["start_url"]
    assert client.get(start).status_code == 200, start


def test_the_offline_shell_caches_the_app_not_the_laptops_answers(client):
    """A service worker that cached API responses would show a phone a stale battery
    level or, worse, a confirmation that has already been answered."""
    response = client.get("/sw.js")
    assert response.status_code == 200
    script = response.text
    assert "javascript" in response.headers["content-type"], response.headers["content-type"]
    body = script.split("const PRECACHE", 1)[1].split("[", 1)[1].split("]")[0]
    # The real `sw.js` is valid JavaScript: individual entries are quoted strings and
    # the last may carry a trailing comma. `json.loads` rejects that trailing comma, so
    # strip it before treating the list as a JSON array. The assertion below — that no
    # entry is an API *answer* path — is the contract; how the shell spells its list is
    # not.
    body_clean = body.rstrip().rstrip(",")
    precached = json.loads("[" + body_clean + "]")
    for path in precached:
        assert not path.startswith(("/command", "/devices", "/confirmations", "/confirm")), (
            f"the shell cache would serve a stored answer from {path}")
    assert any("/phone" in path for path in precached), precached


def test_the_page_registers_the_worker_it_ships(client):
    body = client.get("/phone").text
    assert "serviceWorker" in body and "/sw.js" in body


def test_the_phone_app_still_refuses_a_forged_host(client):
    """Making it installable must not widen what the laptop answers."""
    import importlib

    from fastapi.testclient import TestClient

    app_module = importlib.import_module("jarvis.api.app")
    settings = Settings(lan_mode=True, host="0.0.0.0", auth_enabled=True,
                        api_token=get_settings().api_token)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(app_module, "get_settings", lambda: settings)
    try:
        with TestClient(app_module.app, base_url="http://127.0.0.1:8765",
                        headers={"Host": "evil.example.com"}) as probe:
            assert probe.get("/manifest.webmanifest").status_code == 400
    finally:
        monkey.undo()


# ---------------------------------------------------------------------------
# 4. the window's switch and the server's reader must be the same fact
# ---------------------------------------------------------------------------


def test_the_apps_switch_is_the_same_file_the_server_reads(tmp_path, monkeypatch):
    """The control panel deliberately does not import `jarvis.config` -- it has to work
    when the thing it manages is broken. That only holds if both sides agree on the
    file, which is a contract and therefore a test."""
    launcher = _launcher_module()
    from jarvis.config import lan_mode_state

    monkeypatch.setattr(launcher, "data_home", lambda: tmp_path)
    launcher.set_lan_switch(True)
    assert lan_mode_state(tmp_path) is True
    monkeypatch.delenv("JARVIS_LAN_MODE", raising=False)
    assert Settings(home=tmp_path).lan_mode is True


def _controller(launcher, tmp_path, host="127.0.0.1"):
    return launcher.ServerController(project_dir=tmp_path, python="python", host=host,
                                     port=8765, log_path=tmp_path / "app.log")


def test_the_switch_moves_the_bind_and_not_the_link(tmp_path, monkeypatch):
    """A laptop that binds loopback cannot be reached by a phone however the user has
    configured the app, and a panel that opens `http://0.0.0.0:8765` is a broken link."""
    launcher = _launcher_module()
    monkeypatch.delenv("JARVIS_HOST", raising=False)
    monkeypatch.setattr(launcher, "lan_switch_state", lambda: True)
    controller = _controller(launcher, tmp_path)
    assert controller.bind_host == "0.0.0.0"
    assert controller.host == "127.0.0.1", "the address shown to the user stays usable"


def test_a_laptop_that_was_never_touched_still_binds_loopback_only(tmp_path, monkeypatch):
    launcher = _launcher_module()
    monkeypatch.delenv("JARVIS_HOST", raising=False)
    monkeypatch.setattr(launcher, "lan_switch_state", lambda: False)
    assert _controller(launcher, tmp_path).bind_host == "127.0.0.1"


def test_a_bind_chosen_from_the_shell_is_not_overwritten(tmp_path, monkeypatch):
    launcher = _launcher_module()
    monkeypatch.setenv("JARVIS_HOST", "192.168.5.5")
    monkeypatch.setattr(launcher, "lan_switch_state", lambda: False)
    assert _controller(launcher, tmp_path).bind_host == "192.168.5.5"


def test_a_token_startup_has_not_written_yet_is_not_a_reason_to_refuse(tmp_path, monkeypatch):
    """Found by running the built app, not by reading the code.

    The guard demanded a non-empty `api_token`, but on a first start the token is
    generated *during* startup -- so a fresh install that switched wifi access on was
    refused by a check for a file it was about to create. The guard was refusing the
    one configuration it exists to protect.
    """
    from jarvis.security.auth import lan_startup_problems

    monkeypatch.delenv("JARVIS_LAN_MODE", raising=False)
    settings = Settings(home=tmp_path, lan_mode=True, auth_enabled=True, host="0.0.0.0")
    assert not (tmp_path / "api_token").exists(), "the token must not exist yet"
    assert lan_startup_problems(settings) == [], lan_startup_problems(settings)


# ---------------------------------------------------------------------------
# 5. the scheme has to follow the server, and the page must survive a browser
#    that refuses the worker outright (measured: over plain http on the LAN,
#    Chrome has no `navigator.serviceWorker` at all)
# ---------------------------------------------------------------------------


def test_an_encrypted_laptop_advertises_encrypted_addresses():
    settings = Settings(https_enabled=True, lan_mode=True, host="0.0.0.0", port=8798)
    assert all(url.startswith("https://") for url in settings.reachable_urls()), \
        settings.reachable_urls()
    assert any(origin.startswith("https://127.0.0.1") for origin in settings.allowed_origins()), \
        settings.allowed_origins()


def test_a_plain_laptop_still_advertises_http():
    settings = Settings(host="127.0.0.1", port=8765)
    assert settings.reachable_urls() == ["http://127.0.0.1:8765/phone"], \
        settings.reachable_urls()


def test_an_iphone_is_told_to_launch_standalone(client):
    """iOS honours these and, unlike Chrome, does not demand a secure context -- so the
    home-screen install works over plain http on the LAN, and the page must say so."""
    body = client.get("/phone").text
    assert 'apple-mobile-web-app-capable" content="yes' in body
    assert 'apple-mobile-web-app-title' in body


@pytest.mark.skipif(not node, reason="node is not available to run the page's own code")
def test_the_page_does_not_break_where_the_browser_has_no_worker(client, tmp_path):
    """The exact state Chrome is in on http://<lan-ip>: `navigator.serviceWorker` is
    undefined, so an unguarded register() call would throw and take the page's own
    startup down with it."""
    body = client.get("/phone").text
    start = body.index('if ("serviceWorker" in navigator)')
    # Extract the `if (...) { ... }` block. The register line itself contains
    # `.catch(() => {})` (an inner `}` on the same line), so the block's own closing
    # brace is the first *line* that, stripped, is exactly `}` — which also tolerates
    # the page's real indentation (an earlier `body.index("\n}", start)` assumed a
    # column-0 brace and broke when the page was legitimately re-indented).
    lines = body[start:].split("\n")
    closing = next(i for i, ln in enumerate(lines) if ln.strip() == "}")
    block = "\n".join(lines[: closing + 1])
    script = ("var navigator = {};\n" + block +
              "\nconsole.log('survived');\n")
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:300]
    assert "survived" in proc.stdout, proc.stdout


# ---------------------------------------------------------------------------
# 6. the rebrand boundary (Phase 37): the name people see changed; the names
#    that carry state did not
# ---------------------------------------------------------------------------

BRAND = "Vantage"

WIRING = {
    "jarvis/api/app.py": ["__JARVIS_BOOTSTRAP__"],
    "jarvis/security/auth.py": ["X-JARVIS-Token"],
    "jarvis/config.py": ["JARVIS_HOME", ".jarvis-forge"],
    "dashboard/phone.html": ["jarvis-bootstrap", "X-JARVIS-Token", "jarvis.device"],
    "dashboard/index.html": ["jarvis-bootstrap", "X-JARVIS-Token"],
    "build_app.py": ["com.jarvisforge.app"],
}


def test_the_product_is_called_vantage_everywhere_a_person_looks():
    root = Path(__file__).resolve().parents[1]
    for rel in ("dashboard/index.html", "dashboard/phone.html",
                "dashboard/manifest.webmanifest"):
        text = (root / rel).read_text(encoding="utf-8")
        assert BRAND in text, rel
        assert "JARVIS Forge" not in text and "Jarvis Forge" not in text, rel


@pytest.mark.parametrize("rel,needles", sorted(WIRING.items()))
def test_the_names_that_carry_state_kept_their_spelling(rel, needles):
    """A brand rename that touched these would break a working install silently.

    `X-JARVIS-Token` is what every authenticated call sends; `jarvis.device` is the key
    a phone stores its pairing identity under, so renaming it un-pairs every handset;
    `.jarvis-forge` is where the memory database, audit chain and token live;
    `com.jarvisforge.app` is the bundle identity macOS grants microphone and speech
    permissions to. The visible name changed. These did not, on purpose.
    """
    root = Path(__file__).resolve().parents[1]
    text = (root / rel).read_text(encoding="utf-8")
    for needle in needles:
        assert needle in text, f"{rel} lost {needle!r} -- that is wiring, not branding"


# ---------------------------------------------------------------------------
# 7. the window's checkbox, actually clicked
# ---------------------------------------------------------------------------


def _walk(widget, want, found):
    try:
        if widget.winfo_class() == want:
            found.append(widget)
    except Exception:
        return
    for child in widget.winfo_children():
        _walk(child, want, found)


def test_ticking_the_wifi_box_writes_the_switch_and_rebinds(tmp_path, monkeypatch):
    """The one link in "connect my phone" that no test had reached.

    The self-test counts four *probe* widgets, so it never proved the real panel had a
    checkbox, that clicking it writes the switch, or that it rebinds the server. Here the
    shipped `build_ui` is constructed, the checkbox is found by class and invoked.
    """
    launcher = _launcher_module()
    tk = pytest.importorskip("tkinter")
    messagebox = pytest.importorskip("tkinter.messagebox")

    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setattr(launcher, "data_home", lambda: home)
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: None)   # build it, never block on it
    shown = []
    monkeypatch.setattr(messagebox, "showinfo", lambda t, m: shown.append((t, m)))
    monkeypatch.setattr(messagebox, "showerror", lambda t, m: shown.append((t, m)))

    health = {"tools": 128, "agents": ["laptop"], "pending_confirmations": 0,
              "llm_enabled": False}
    monkeypatch.setattr(launcher, "api_get",
                        lambda host, port, path: health if path.startswith("/health")
                        else ([] if path.startswith(("/devices", "/audit")) else {}))
    monkeypatch.setattr(launcher, "api_post", lambda *a, **k: {})
    monkeypatch.setattr(launcher, "port_in_use", lambda *a: True)

    restarted = []

    class Stub:
        host, port, tail, project_dir_error, attached, process = \
            "127.0.0.1", 8765, [], "", False, None
        bind_host = "127.0.0.1"

        def restart(self_inner):
            restarted.append(self_inner.bind_host)

        def wait_ready(self_inner, timeout=45.0):
            return True

        def stop(self_inner): pass

        def running(self_inner):
            return True

    controller = Stub()
    launcher.build_ui(controller, lambda: None)

    boxes = []
    _walk(tk._default_root, "Checkbutton", boxes)
    assert len(boxes) == 1, f"expected one wifi checkbox, found {len(boxes)}"
    assert "wifi" in boxes[0].cget("text").lower()

    boxes[0].invoke()                                  # the user's click
    assert (home / "lan_mode").read_text().strip() == "true", "the click wrote nothing"
    assert restarted == ["0.0.0.0"], f"it did not rebind: {restarted}"
    assert shown and "http" in shown[0][1], "the user was never told what to type"

    boxes[0].invoke()                                  # and clicking again turns it off
    assert (home / "lan_mode").read_text().strip() == "false"
    assert restarted == ["0.0.0.0", "127.0.0.1"], restarted
