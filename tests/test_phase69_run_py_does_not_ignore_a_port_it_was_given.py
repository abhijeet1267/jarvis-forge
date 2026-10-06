"""`run.py serve --port 8899` must not bind 8765.

Discovered while verifying Phase 69, and it is the kind of defect that corrupts every verification
that follows it. The command was typed to start an isolated instance on a spare port; the process
came up as `Uvicorn running on http://127.0.0.1:8765` -- the real port -- because `main()` reads
`sys.argv[1]` as the mode and never looks at the rest. The flags were not rejected, they were
dropped. A dashboard tab that was already open at 127.0.0.1:8765 then started polling the new
process and got 401 for every read.

So this is two requirements:

* a flag the operator passes has to reach the setting it names (`--port`, `--host`, `--home`);
* a flag nothing consumes has to fail loudly, because a silently ignored `--port` is how "I verified
  it on an isolated instance" becomes false.

`jarvis/relay.py` is the module that genuinely takes `--port`; `run.py` did not, and said nothing.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def runner():
    """`run.py` is a script, not a package member, so it is loaded by path."""
    spec = importlib.util.spec_from_file_location("vantage_run", REPO / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def clean_env(monkeypatch):
    for key in ("JARVIS_PORT", "JARVIS_HOST", "JARVIS_HOME"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_a_port_flag_reaches_the_setting_that_binds_it(runner, clean_env):
    mode = runner.apply_flags(["serve", "--port", "8899"])

    assert mode == "serve"
    assert os.environ["JARVIS_PORT"] == "8899", "the flag was accepted and then thrown away"


def test_the_home_flag_reaches_the_directory_the_data_lives_in(runner, clean_env):
    runner.apply_flags(["serve", "--home", "/tmp/elsewhere"])

    assert os.environ["JARVIS_HOME"] == "/tmp/elsewhere"


def test_an_unknown_flag_is_refused_rather_than_ignored(runner, clean_env):
    """The whole point. Silence is what made the incident possible."""
    with pytest.raises(SystemExit) as raised:
        runner.apply_flags(["serve", "--prt", "8899"])

    assert raised.value.code != 0
    assert "--prt" in str(raised.value), "the message must name the flag that was refused"


def test_a_port_that_is_not_a_number_is_refused_before_binding(runner, clean_env):
    with pytest.raises(SystemExit) as raised:
        runner.apply_flags(["serve", "--port", "not-a-port"])

    assert raised.value.code != 0


def test_a_flag_without_its_value_is_refused(runner, clean_env):
    with pytest.raises(SystemExit) as raised:
        runner.apply_flags(["serve", "--port"])

    assert raised.value.code != 0


def test_no_flags_changes_nothing(runner, clean_env):
    """`python run.py` must still mean "use the settings", not "set a port nobody asked for"."""
    assert runner.apply_flags(["serve"]) == "serve"
    assert "JARVIS_PORT" not in os.environ
    assert "JARVIS_HOME" not in os.environ


def test_the_bare_invocation_still_means_serve(runner, clean_env):
    assert runner.apply_flags([]) == "serve"


def test_the_relay_module_keeps_its_own_port_flag():
    """Guard against the fix being read as a general rule: `python -m jarvis.relay --port N` is a
    different program that already parses its arguments."""
    sys.path.insert(0, str(REPO))
    from jarvis import relay

    assert "--port" in Path(relay.__file__).read_text()
