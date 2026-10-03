"""Phase 40 (part 2): the panel dying must take the server with it.

Measured on the delivered `Vantage.app`: `tell application "Vantage" to quit` closed the
window, and the server kept answering `200` on the wifi address with its parent gone --
`ps` showed it reparented to launchd (PPID 1). It was still listening minutes later, with
nothing on screen that could stop it.

The first attempt at this phase installed `SIGTERM`/`SIGINT` handlers. That is not the path
macOS takes: Tk answers the quit event by invoking the Tcl `exit` command, which tears the
interpreter down from C, so no Python signal handler and no `atexit` callable ever runs. The
handlers are worth having for `kill` and Ctrl-C, but on their own they were a fix for a
different death.

So the guarantee is made twice: the panel's own exit path is rebound to the same cleanup the
Quit button uses, and the server stops trusting the panel to tell it anything -- it notices
its parent is gone and shuts itself down.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _launcher_module():
    if "phase40b_launcher" in sys.modules:
        return sys.modules["phase40b_launcher"]
    spec = importlib.util.spec_from_file_location("phase40b_launcher",
                                                 REPO / "app" / "launcher.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase40b_launcher"] = module
    spec.loader.exec_module(module)
    return module


class _FakeProcess:
    pid = 4242
    stdout = iter(())

    def poll(self):
        return None


# ---------------------------------------------------------------------------
# 1. the exit the system sends is the exit that cleans up
# ---------------------------------------------------------------------------

#: Run in a child process on purpose. If the panel has not rebound Tcl's `exit`, that
#: command tears the interpreter down from C -- inside the test process that would take
#: pytest with it, and a test that can only pass is not a test.
_CHILD = r"""
import sys, os
from pathlib import Path
sys.path.insert(0, {app_dir!r})
os.environ["JARVIS_HOME"] = {home!r}
os.environ["JARVIS_LOG_LEVEL"] = "WARNING"
import tkinter as tk
import tkinter.messagebox as mb
import webbrowser
import launcher

mb.showinfo = mb.showerror = lambda *a: None
webbrowser.open = lambda *a, **k: True
tk.Tk.mainloop = lambda self: None
launcher.data_home = lambda: Path({home!r})
launcher.api_get = lambda *a, **k: {{}}
launcher.api_post = lambda *a, **k: {{}}
launcher.port_in_use = lambda *a: True

class Ctrl:
    host = "127.0.0.1"; port = 8797; tail = []; project_dir_error = ""
    attached = True; process = None; bind_host = "127.0.0.1"
    def restart(self): pass
    def wait_ready(self, timeout=45.0): return True
    def running(self): return False
    def stop(self):
        marker = Path({marker!r})
        marker.write_text(str((int(marker.read_text()) if marker.exists() else 0) + 1))

launcher.build_ui(Ctrl(), lambda: None)
root = tk._default_root
print("built", flush=True)
root.tk.eval("exit")           # what Tk runs on quit and on the Quit menu item
print("survived", flush=True)
"""


def _quit_the_panel_the_way_macos_does(tmp_path):
    import subprocess

    marker = tmp_path / "stops"
    home = tmp_path / "data"
    home.mkdir()
    script = _CHILD.format(app_dir=str(REPO / "app"), home=str(home), marker=str(marker))
    run = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         timeout=60, cwd=str(REPO))
    return run, (marker.read_text() if marker.exists() else "")


def test_the_quit_macOS_sends_runs_the_same_cleanup_as_the_button(tmp_path):
    """Measured on the delivered app: the window closed, the server kept answering.

    The first fix for this phase installed SIGTERM/SIGINT handlers, and the same quit
    still orphaned the server -- Tk does not signal, it calls Tcl's `exit`, which leaves
    Python no chance to run either kind of hook.
    """
    pytest.importorskip("tkinter")
    run, stops = _quit_the_panel_the_way_macos_does(tmp_path)
    assert "built" in run.stdout, run.stderr[-400:]
    assert stops == "1", (
        f"`exit` did not stop the server (stops={stops!r}); a closed window leaves a "
        f"wifi-listening process behind")


# ---------------------------------------------------------------------------
# 2. the server does not depend on being told
# ---------------------------------------------------------------------------


def test_the_panel_tells_its_server_who_its_parent_is(tmp_path, monkeypatch):
    launcher = _launcher_module()
    recorded: dict = {}

    def fake_popen(argv, **kwargs):
        recorded.update(kwargs)
        return _FakeProcess()

    monkeypatch.delenv("JARVIS_GUARDIAN_PID", raising=False)
    monkeypatch.setattr(launcher, "port_in_use", lambda *a: False)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    controller = launcher.ServerController(project_dir=tmp_path, python="python",
                                          host="127.0.0.1", port=8799,
                                          log_path=tmp_path / "app.log")
    controller.start()
    assert recorded["env"].get("JARVIS_GUARDIAN_PID") == str(os.getpid()), (
        "the server cannot notice an orphan it was never told about")


def test_a_guardian_that_is_gone_is_recognised(monkeypatch):
    from jarvis import process

    monkeypatch.setattr(os, "getppid", lambda: os.getpid())
    assert process.guardian_gone(os.getpid()) is False, "a living parent is not gone"
    assert process.guardian_gone(4_000_000) is True, "a pid nobody owns is gone"

    # Reparenting is the exact shape observed: the parent died, launchd took the child.
    monkeypatch.setattr(os, "getppid", lambda: 1)
    assert process.guardian_gone(os.getpid()) is True


def test_a_server_that_notices_its_panel_died_shuts_itself_down(monkeypatch):
    from jarvis import process

    calls: list = []
    monkeypatch.setenv("JARVIS_GUARDIAN_PID", "4000001")   # nobody owns this pid
    thread = process.start_guardian_from_env(on_exit=lambda: calls.append("exit"),
                                             interval=0.05)
    assert thread is not None
    deadline = time.time() + 3.0
    while time.time() < deadline and not calls:
        time.sleep(0.05)
    assert calls == ["exit"], "the orphan lived on after noticing"


def test_no_guardian_is_armed_when_nobody_asked(monkeypatch):
    from jarvis import process

    monkeypatch.delenv("JARVIS_GUARDIAN_PID", raising=False)
    assert process.start_guardian_from_env(on_exit=lambda: None) is None
