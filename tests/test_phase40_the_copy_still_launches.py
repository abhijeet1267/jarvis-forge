"""Phase 40: the copy you hand someone must still launch.

Asked for the application as files in a folder rather than as a commit, the delivered
`Vantage.app` was copied with `ditto`, ran, served the phone page -- and `codesign
--verify` on it said *"a sealed resource is missing or invalid"*. The bundle had been
signed with 145 sealed files and then, simply by being used, grown 127 `.pyc` files
inside itself, because the server it spawns writes bytecode next to the source it is
reading. A seal that the application breaks by running is not a seal; and on another
machine, with the quarantine flag set, an invalid seal is the difference between "Open"
and "Vantage is damaged".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _launcher_module():
    """Load `app/launcher.py` under a private module name.

    It is not a package member (the bundle ships it at Contents/Resources/launcher.py),
    and the panel must work even when `jarvis` itself cannot be imported.
    """
    if "phase40_launcher" in sys.modules:
        return sys.modules["phase40_launcher"]
    spec = importlib.util.spec_from_file_location("phase40_launcher",
                                                 REPO / "app" / "launcher.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase40_launcher"] = module
    spec.loader.exec_module(module)
    return module


class _FakeProcess:
    pid = 4242
    # An iterator, not a tuple: `_pump` reads this on a thread and an empty *tuple* is
    # false, so the pump's own guard would fire and the run would be noisy.
    stdout = iter(())

    def poll(self):
        return None


@pytest.fixture()
def spawned(tmp_path, monkeypatch):
    """Start a controller with the spawn captured, never a real server."""
    launcher = _launcher_module()

    recorded: dict = {}

    def fake_popen(argv, **kwargs):
        recorded["argv"] = argv
        recorded.update(kwargs)
        return _FakeProcess()

    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.setattr(launcher, "port_in_use", lambda *a: False)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    controller = launcher.ServerController(project_dir=tmp_path, python="python",
                                          host="127.0.0.1", port=8799,
                                          log_path=tmp_path / "app.log")
    controller.start()
    return recorded


# ---------------------------------------------------------------------------
# 1. the app must not write inside itself
# ---------------------------------------------------------------------------


def test_the_server_the_app_starts_is_told_not_to_write_bytecode(spawned):
    env = spawned["env"]
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1", (
        "a server that compiles next to its own source adds .pyc files to a bundle that "
        "was already signed, and the copy then fails its seal check")


def test_that_telling_comes_out_of_the_app_and_not_only_the_shell(spawned):
    """An environment variable a user happens to have set is not a guarantee.

    The launcher is the one thing that always runs, so the instruction has to be in it
    unconditionally -- the same reason `PYTHONUNBUFFERED` is assigned and not defaulted.
    """
    assert spawned["env"]["PYTHONUNBUFFERED"] == "1"
    assert spawned["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


# ---------------------------------------------------------------------------
# 2. what gets staged is what was signed
# ---------------------------------------------------------------------------


def test_staging_carries_no_compiled_bytecode(tmp_path, monkeypatch):
    """The build skips caches, and the seal depends on that staying true."""
    build_app = importlib.import_module("build_app")

    source = tmp_path / "checkout"
    (source / "jarvis" / "__pycache__").mkdir(parents=True)
    (source / "jarvis" / "engine.py").write_text("x = 1\n")
    (source / "jarvis" / "__pycache__" / "engine.cpython-311.pyc").write_bytes(b"\x00")
    (source / "jarvis" / "stray.pyc").write_bytes(b"\x00")
    monkeypatch.setattr(build_app, "PROJECT_DIR", source)

    staged = build_app.stage_project(tmp_path / "Resources")

    assert (staged / "jarvis" / "engine.py").is_file()
    assert not list(staged.rglob("*.pyc")), "a .pyc inside a signed bundle breaks the seal"


def test_the_build_signs_after_it_stages(monkeypatch):
    """Order matters: sign first and every staged file invalidates the signature."""
    import re

    source = (REPO / "build_app.py").read_text()
    body = re.search(r"def build\(.*?\n(?=\ndef |\Z)", source, re.S).group(0)
    assert body.index("stage_project") < body.index("codesign"), (
        "the bundle is sealed before its own program is copied into it")


# ---------------------------------------------------------------------------
# 3. a panel that is killed must not leave a listener behind
# ---------------------------------------------------------------------------


def test_a_killed_panel_stops_the_server_it_started(tmp_path, monkeypatch):
    """Measured on the delivered app: AppleScript quit -> SIGTERM to the panel, the panel
    died, and the server kept answering `200` over the wifi address with no window left to
    close it. Force Quit, logout and `shutdown` all take the same path."""
    import re
    import signal

    launcher = _launcher_module()
    assert hasattr(launcher, "install_shutdown_handlers"), (
        "nothing catches a termination signal, so the only cleanup is the Quit button")

    import signal as _signal

    original = {n: _signal.getsignal(n) for n in (_signal.SIGTERM, _signal.SIGINT)}
    stopped: list = []
    controller = launcher.ServerController(project_dir=tmp_path, python="python",
                                          host="127.0.0.1", port=8799,
                                          log_path=tmp_path / "app.log")
    monkeypatch.setattr(controller, "stop", lambda: stopped.append(True))
    try:
        launcher.install_shutdown_handlers(controller)

        for number in (signal.SIGTERM, signal.SIGINT):
            handler = signal.getsignal(number)
            assert callable(handler), f"signal {number} is left at its default"
            with pytest.raises(SystemExit):
                handler(number, None)
        assert stopped == [True, True], "the server outlived the panel that started it"
    finally:
        for number, previous in original.items():
            _signal.signal(number, previous)


def test_the_handler_is_installed_before_the_panel_can_be_closed():
    """Registering after the UI is up leaves a window where a kill orphans the server."""
    import re

    source = (REPO / "app" / "launcher.py").read_text()
    body = re.search(r"^def main\(.*?\n(?=\ndef |\Z)", source, re.S | re.M).group(0)
    assert "install_shutdown_handlers" in body, "the quit button is the only cleanup path"
    assert body.index("install_shutdown_handlers") > body.index("controller.start()"), (
        "there is nothing to stop before the server has been started")

