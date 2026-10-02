"""Phase 38: connecting the phone should be a scan, not a typing exercise.

The root cause of "not working" was operational -- nothing was listening on the port, because
the app had never been started -- and that is fixed by running it. What remains is the friction
the address itself creates: an IPv4 with a port, typed on a phone keyboard, on a network where
the address changes when the router feels like it.

`qrcode` is deliberately NOT a dependency. The project runs stdlib-first, so the code is drawn
when the library happens to be installed and the plain address is shown when it is not -- and
the fallback is tested, because an untested fallback is a crash waiting for someone with a
clean install.
"""

from __future__ import annotations

import base64

import pytest


def _launcher():
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "app"))
    import launcher

    return launcher


# ---------------------------------------------------------------------------
# 1. the encoder, and the case where the library is not installed
# ---------------------------------------------------------------------------


def test_the_pairing_code_is_a_real_png():
    launcher = _launcher()
    png = launcher.pairing_qr_png("http://172.20.169.34:8765/phone")
    if png is None:
        pytest.skip("qrcode is not installed here; the fallback is the tested path below")
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert len(png) > 200, "a stub image would not scan"


def test_tk_can_actually_load_the_image_it_is_given():
    """The claim worth proving is not "a PNG exists" but "this toolkit can display it"."""
    tk = pytest.importorskip("tkinter")
    launcher = _launcher()
    png = launcher.pairing_qr_png("http://172.20.169.34:8765/phone")
    if png is None:
        pytest.skip("qrcode is not installed here")
    root = tk.Tk()
    root.withdraw()
    image = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
    assert image.width() > 80 and image.height() > 80, image


def test_a_machine_without_the_library_still_shows_the_address(tmp_path, monkeypatch):
    """No hard dependency means the missing-library path is the common path, not an
    edge case -- so it must return "no image" rather than raising."""
    import builtins

    launcher = _launcher()
    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name == "qrcode":
            raise ImportError("no module named qrcode")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", blocked)
    assert launcher.pairing_qr_png("http://192.168.1.5:8765/phone") is None


# ---------------------------------------------------------------------------
# 2. the panel must carry the code and the address together
# ---------------------------------------------------------------------------


def _walk(widget, want, found):
    try:
        if widget.winfo_class() == want:
            found.append(widget)
    except Exception:
        return
    for child in widget.winfo_children():
        _walk(child, want, found)


def _build_panel(launcher, monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")
    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setattr(launcher, "data_home", lambda: home)
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: None)
    health = {"tools": 128, "agents": ["laptop"], "pending_confirmations": 0,
              "llm_enabled": False,
              "phone_urls": ["http://127.0.0.1:8765/phone",
                             "http://172.20.169.34:8765/phone"]}
    monkeypatch.setattr(launcher, "api_get",
                        lambda host, port, path: health if path.startswith("/health")
                        else ([] if path.startswith(("/devices", "/audit")) else {}))
    monkeypatch.setattr(launcher, "api_post", lambda *a, **k: {})
    monkeypatch.setattr(launcher, "port_in_use", lambda *a: True)
    # boot() opens the dashboard when the server answers; a test must not be able to
    # raise a browser tab on someone's machine.
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)

    class Stub:
        host, port, tail, project_dir_error, attached, process = \
            "127.0.0.1", 8765, [], "", False, None
        bind_host = "0.0.0.0"

        def restart(self_inner): pass

        def wait_ready(self_inner, timeout=45.0): return True

        def stop(self_inner): pass

        def running(self_inner): return True

    # tkinter only assigns `_default_root` to the FIRST Tk() in a process, so reading it
    # here would hand back an earlier test's empty window -- which is exactly how this
    # file passed alone and failed in a full run. Capture the root actually built.
    created = []
    real_tk = tk.Tk

    class RecordedTk(real_tk):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created.append(self)

    monkeypatch.setattr(tk, "Tk", RecordedTk)
    launcher.build_ui(Stub(), lambda: None)
    assert created, "build_ui did not create a window"
    root = created[-1]

    # The panel's first refresh is on a 200 ms timer and mainloop is stubbed out, so the
    # timers have to be pumped by hand.
    import time

    for _ in range(80):
        root.update()
        time.sleep(0.02)
    return root


def test_the_panel_offers_a_scan_code_when_one_can_be_drawn(monkeypatch, tmp_path):
    launcher = _launcher()
    if launcher.pairing_qr_png("http://x/phone") is None:
        pytest.skip("qrcode is not installed on this machine")
    root = _build_panel(launcher, monkeypatch, tmp_path)
    labels = []
    _walk(root, "Label", labels)
    with_image = [l for l in labels if str(l.cget("image"))]
    assert with_image, "the panel drew no pairing image"


def test_the_address_is_readable_alongside_any_code(monkeypatch, tmp_path):
    """A QR is a convenience, not the only way in: someone on a laptop with a laptop
    still needs the text."""
    launcher = _launcher()
    root = _build_panel(launcher, monkeypatch, tmp_path)
    labels = []
    _walk(root, "Label", labels)
    texts = [str(l.cget("text")) for l in labels]
    assert any("http://172.20.169.34:8765/phone" in t for t in texts), texts
    assert not any("127.0.0.1:8765/phone" in t for t in texts), \
        "loopback is not an address a phone can use"


# ---------------------------------------------------------------------------
# 3. the server is the single source for "what to type"
# ---------------------------------------------------------------------------


def test_the_server_answers_with_the_addresses_it_would_accept(client):
    """The panel asks the server instead of deriving the address itself, so the URL it
    shows and the Host check that lets the request through cannot drift apart."""
    import asyncio

    from jarvis.config import get_settings

    health = client.get("/health").json()
    assert "phone_urls" in health, sorted(health)
    assert health["phone_urls"] == get_settings().reachable_urls()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from jarvis.api.app import app

    with TestClient(app, base_url="http://127.0.0.1:8765",
                    headers={"X-JARVIS-Token": __import__("jarvis.config",
                                                          fromlist=["get_settings"]
                                                          ).get_settings().api_token}) as c:
        yield c
