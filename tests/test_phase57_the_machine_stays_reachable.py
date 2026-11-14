"""Phase 57 -- the laptop keeps answering after its window closes.

Everything the phone does reaches a process the panel starts and the panel's death stops. That
was the correct answer for Phase 40 (an orphan server holding the wifi port open with no window
left to close it is worse than no server), but it leaves the product unable to do the thing a
"personal AI operating system" is mostly for: answering when you are standing at your phone and
the laptop is closed in a bag. Phase 56 met the same wall from the other side -- a macOS
permission prompt cannot be raised by a *helper* process, only by the app process -- so a
long-running host is now a prerequisite for two gaps, not a convenience.

The shape this phase has to get right, in order of how badly each failure would hurt:

1. **No secret in the launch config.** A LaunchAgent is a plain file that outlives the session
   and is read by whoever can read the home folder. The token stays where it already lives, in
   ``~/.jarvis-forge/api_token`` at mode 0600, and the plist names nothing but the interpreter,
   the program and three variables the server needs.
2. **The watchdog must not be inherited.** ``run.py`` arms a guardian that exits within two
   seconds when the process that started it is gone -- that is how the panel's death kills the
   server today. A service started by launchd has launchd as its parent, so a stale
   ``JARVIS_GUARDIAN_PID`` in the plist would make the new service kill itself on every boot.
3. **One machine, one server.** Two servers against one data directory means two schedulers
   firing the same jobs and two writers on one SQLite file. The host row cannot duplicate (the
   device id is a primary key), which is exactly why the damage is silent.
4. **The answer comes from re-reading**, as in Phase 56. ``launchctl`` accepting a command is
   not proof a service is loaded, and a plist on disk is not proof anything is running.
5. **The path guard does not move.** ``Library/LaunchAgents`` is protected precisely because it
   is "code that runs without the user present", and it must stay refused for the file tools.
   Persistence is therefore a dedicated, confirmation-gated capability, not a sentence.
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# seams: nothing in this module talks to launchd, binds a port, or touches a
# real ~/Library. Every test drives an injected runner and a tmp home.
# ---------------------------------------------------------------------------


class FakeLaunchctl:
    """Records argv and answers from a script. The world model is explicit:
    ``loaded`` is what ``launchctl print`` would say, not what was asked for."""

    def __init__(self, *, print_reply="not found", bootstrap_code=0, bootstrap_err="",
                 bootout_code=0):
        self.calls: list[list[str]] = []
        self.print_reply = print_reply
        self.bootstrap_code = bootstrap_code
        self.bootstrap_err = bootstrap_err
        self.bootout_code = bootout_code

    def __call__(self, argv):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        if " bootstrap" in joined or " load " in joined:
            if self.bootstrap_code:
                return self.bootstrap_code, "", self.bootstrap_err
            self.print_reply = ("com.jarvisforge.vantage = {\n\tstate = running\n"
                              "\t\tpid = 4242\n}")
            return 0, "", ""
        if " bootout" in joined or " unload " in joined:
            if self.bootout_code:
                return self.bootout_code, "", "Could not find service"
            self.print_reply = "Could not find service"
            return 0, "", ""
        if " print " in joined:
            if self.print_reply in ("not found", "Could not find service"):
                return 113, "", "Could not find service"
            return 0, self.print_reply, ""
        return 0, "", ""

    def verbs(self):
        return [" ".join(c[1:3]) for c in self.calls]


def _settings(tmp_path, **over):
    from jarvis.config import Settings

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return Settings(_env_file=None, home=home, api_token="tok-" + "X" * 32,
                    host="127.0.0.1", port=8765, **over)


def free_port() -> int:
    """A port this test owns. 8765 is not it -- the delivered app is listening on that one on
    this machine, and a start-up guard that probes the port reads a real server as a conflict.
    Pinning the premise is what keeps the assertion honest."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _service(tmp_path, runner=None, probe=None, project_dir=None, python=None, **over):
    """A service pointed at a machine where an install *could* work, so that each test's own
    reason for refusing is the one it is about. Pass a broken ``project_dir``/``python`` to test
    the preflight."""
    from jarvis.persistence import HostPersistenceService

    settings = _settings(tmp_path, **over)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(exist_ok=True)
    if project_dir is None:
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        (project / "run.py").write_text("# the program launchd would start\n", encoding="utf-8")
    else:
        project = Path(project_dir)
    if python is None:
        interpreter = tmp_path / "python"
        interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    else:
        interpreter = Path(python)
    return HostPersistenceService(
        settings=settings,
        agents_dir=agents,
        runner=runner if runner is not None else FakeLaunchctl(),
        probe=probe if probe is not None else (lambda: None),
        project_dir=str(project),
        python=str(interpreter),
    ), agents


# ---------------------------------------------------------------------------
# 1. the shape of the thing
# ---------------------------------------------------------------------------


def test_the_service_module_exists_with_a_stable_label():
    from jarvis import persistence

    assert persistence.AGENT_LABEL.startswith("com.jarvisforge.")
    # The label is durable state: renaming it orphans an installed agent that keeps running
    # under the old one, so it stays in the identifier family that carries the data dir.
    assert persistence.AGENT_LABEL == "com.jarvisforge.vantage"


def test_the_agent_lives_in_the_users_own_folder_and_never_a_privileged_one(tmp_path):
    from jarvis import persistence

    agents = tmp_path / "LaunchAgents"
    path = persistence.agent_path(agents_dir=agents)
    assert path.name == "com.jarvisforge.vantage.plist"
    default = str(persistence.agent_path())
    assert "Library/LaunchAgents" in default
    assert "Library/LaunchDaemons" not in default, \
        "a daemon runs as root with no user present; this is a user login item"


def test_the_plist_is_a_valid_plist_that_round_trips(tmp_path):
    from jarvis import persistence

    text = persistence.render_plist(
        python="/x/python", project_dir="/x/project", home="/x/home",
        host="127.0.0.1", port=8765)
    data = plistlib.loads(text.encode("utf-8"))
    assert data["Label"] == persistence.AGENT_LABEL


def test_the_plist_starts_the_program_the_panel_actually_starts(tmp_path):
    """The launcher runs ``python run.py`` with the project as cwd. If the service starts
    something else, two different programs answer on the same port."""
    from jarvis import persistence

    data = plistlib.loads(persistence.render_plist(
        python="/x/python", project_dir="/x/project", home="/x/home",
        host="127.0.0.1", port=8765).encode("utf-8"))
    assert data["ProgramArguments"] == ["/x/python", "/x/project/run.py"]
    assert data["WorkingDirectory"] == "/x/project"


def test_the_service_survives_a_crash_and_stays_down_after_a_clean_quit(tmp_path):
    from jarvis import persistence

    data = plistlib.loads(persistence.render_plist(
        python="/x/python", project_dir="/x/project", home="/x/home",
        host="127.0.0.1", port=8765).encode("utf-8"))
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}, \
        "KeepAlive=true restarts forever, so a port conflict becomes a crash loop"


def test_the_logs_go_beside_the_data_and_not_into_the_sealed_bundle(tmp_path):
    """Phase 40: a file written inside the bundle after signing breaks its seal -- that is
    what "Vantage is damaged" means. A service that logs into the bundle reproduces it."""
    from jarvis import persistence

    data = plistlib.loads(persistence.render_plist(
        python="/x/project/.venv/bin/python", project_dir="/x/project", home="/x/home",
        host="127.0.0.1", port=8765).encode("utf-8"))
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert not data[key].startswith("/x/project"), f"{key} writes inside the program folder"
        assert data[key].startswith("/x/home")


# ---------------------------------------------------------------------------
# 2. no secret in the launch config, and no inherited watchdog
# ---------------------------------------------------------------------------


def test_the_token_never_enters_the_launch_config(tmp_path):
    from jarvis import persistence

    settings = _settings(tmp_path)
    text = persistence.render_plist(
        python="/x/python", project_dir="/x/project", home=str(settings.home),
        host=settings.host, port=settings.port)
    assert settings.api_token not in text
    assert "api_token" not in text
    data = plistlib.loads(text.encode("utf-8"))
    assert not [k for k in data.get("EnvironmentVariables", {}) if "TOKEN" in k.upper()]


def test_the_guardian_variable_is_absent_because_it_would_kill_the_service(tmp_path):
    """``jarvis.process`` exits within two seconds of noticing its parent is gone. launchd is
    the parent of a service, so a stale guardian pid in the plist is a guaranteed self-kill."""
    from jarvis import persistence

    text = persistence.render_plist(
        python="/x/python", project_dir="/x/project", home="/x/home",
        host="127.0.0.1", port=8765)
    assert "JARVIS_GUARDIAN_PID" not in text


def test_only_the_variables_the_server_needs_are_written(tmp_path):
    from jarvis import persistence

    data = plistlib.loads(persistence.render_plist(
        python="/x/python", project_dir="/x/project", home="/x/home",
        host="0.0.0.0", port=8765).encode("utf-8"))
    env = data["EnvironmentVariables"]
    assert env["JARVIS_HOME"] == "/x/home"
    assert env["JARVIS_HOST"] == "0.0.0.0"
    assert env["JARVIS_PORT"] == "8765"
    assert set(env) <= {"JARVIS_HOME", "JARVIS_HOST", "JARVIS_PORT",
                        "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"}, \
        "anything else is inherited from whoever rendered the plist, which is how a secret leaks"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1", \
        "a service that compiles the staged source breaks the bundle seal (Phase 40)"


# ---------------------------------------------------------------------------
# 3. refuse rather than install something that can never start
# ---------------------------------------------------------------------------


def test_enable_refuses_when_the_interpreter_is_not_there(tmp_path):
    service, agents = _service(tmp_path, python=str(tmp_path / "not-here"))
    result = service.enable()
    assert result["ok"] is False
    assert "python" in result["error"].lower()
    assert list(agents.iterdir()) == [], "a refusal must install nothing"


def test_enable_refuses_when_the_project_is_not_there(tmp_path):
    service, agents = _service(tmp_path, project_dir=str(tmp_path / "not-here"))
    result = service.enable()
    assert result["ok"] is False
    assert list(agents.iterdir()) == []


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_off_macos_there_is_no_login_item_to_install(tmp_path, platform, monkeypatch):
    monkeypatch.setattr("jarvis.persistence.PLATFORM", platform)
    service, agents = _service(tmp_path)
    result = service.enable()
    assert result["ok"] is False
    assert "macos" in result["error"].lower()
    assert list(agents.iterdir()) == []
    assert service.status()["supported"] is False


# ---------------------------------------------------------------------------
# 4. one machine, one server
# ---------------------------------------------------------------------------


def test_enable_refuses_when_a_server_already_answers_and_names_it(tmp_path):
    """Installing a login item while another instance owns the port produces two schedulers
    firing the same jobs. The refusal has to say what it found, not just that it refused."""
    probe = lambda: {"pid": 99, "source": "the panel window"}
    service, agents = _service(tmp_path, probe=probe)
    result = service.enable()
    assert result["ok"] is False
    assert "99" in result["error"] or "panel" in result["error"]
    assert list(agents.iterdir()) == []


def test_a_claim_held_by_a_live_process_blocks_a_second_start(tmp_path, monkeypatch):
    from jarvis import persistence

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(persistence.os, "kill", lambda pid, sig: None)  # pid alive
    ok, who = persistence.acquire_claim(home, pid=111, host="127.0.0.1", port=8765)
    assert ok is True
    second, who2 = persistence.acquire_claim(home, pid=222, host="127.0.0.1", port=8765)
    assert second is False
    assert "111" in who2


def test_a_claim_left_by_a_dead_process_is_reclaimed(tmp_path, monkeypatch):
    from jarvis import persistence

    home = tmp_path / "home"
    home.mkdir()

    def kill(pid, sig):
        if pid == 111:
            raise ProcessLookupError()

    monkeypatch.setattr(persistence.os, "kill", kill)
    ok, who = persistence.acquire_claim(home, pid=111, host="127.0.0.1", port=8765)
    assert ok is True
    # The holder died; a machine that cannot reboot itself is a machine that stays down.
    ok2, who2 = persistence.acquire_claim(home, pid=222, host="127.0.0.1", port=8765)
    assert ok2 is True, who2


def test_the_claim_holds_nothing_secret(tmp_path, monkeypatch):
    from jarvis import persistence

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(persistence.os, "kill", lambda pid, sig: None)
    persistence.acquire_claim(home, pid=111, host="127.0.0.1", port=8765)
    raw = persistence.claim_path(home).read_text(encoding="utf-8")
    assert set(json.loads(raw)) <= {"pid", "host", "port", "started"}
    assert "tok-" not in raw


def test_releasing_lets_the_next_process_claim(tmp_path, monkeypatch):
    from jarvis import persistence

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(persistence.os, "kill", lambda pid, sig: None)
    persistence.acquire_claim(home, pid=111, host="127.0.0.1", port=8765)
    persistence.release_claim(home, pid=111)
    assert persistence.read_claim(home) is None
    ok, _ = persistence.acquire_claim(home, pid=222, host="127.0.0.1", port=8765)
    assert ok is True


def test_a_release_by_the_wrong_pid_does_not_steal_the_claim(tmp_path, monkeypatch):
    from jarvis import persistence

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(persistence.os, "kill", lambda pid, sig: None)
    persistence.acquire_claim(home, pid=111, host="127.0.0.1", port=8765)
    persistence.release_claim(home, pid=222)
    assert persistence.read_claim(home)["pid"] == 111


# ---------------------------------------------------------------------------
# 5. the answer comes from re-reading the world
# ---------------------------------------------------------------------------


def test_a_plist_on_disk_is_not_a_running_service(tmp_path):
    """The failure mode this whole project keeps meeting: writing the thing and calling the
    write the effect. Here launchd says it cannot find the service, so the state is not loaded
    even though the file is installed."""
    from jarvis.persistence import HostPersistenceService
    from jarvis.config import Settings

    (tmp_path / "python").write_text("", encoding="utf-8")
    (tmp_path / "project").mkdir()
    settings = Settings(_env_file=None, home=tmp_path / "home", host="127.0.0.1", port=8765)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    runner = FakeLaunchctl(bootstrap_code=0, print_reply="not found")
    service = HostPersistenceService(settings=settings, agents_dir=agents, runner=runner,
                                     probe=lambda: None, project_dir=str(tmp_path / "project"),
                                     python=str(tmp_path / "python"))
    # Simulate a plist that exists but was never accepted by launchd.
    service.install_file_only()
    status = service.status()
    assert status["installed"] is True
    assert status["loaded"] is False
    assert status["state"] != "running"


def test_enable_reports_the_state_launchd_actually_gives(tmp_path):
    (tmp_path / "python").write_text("", encoding="utf-8")
    (tmp_path / "project").mkdir()
    runner = FakeLaunchctl(print_reply="not found")
    service, agents = _service(tmp_path, runner=runner)
    result = service.enable()
    assert result["ok"] is True
    assert result["state"] == "loaded"
    assert result["pid"] == 4242, "pid parsed from the re-read, not from the request"
    assert (agents / "com.jarvisforge.vantage.plist").exists()


def test_enable_reports_failure_with_launchd_s_own_words_and_installs_nothing(tmp_path):
    (tmp_path / "python").write_text("", encoding="utf-8")
    (tmp_path / "project").mkdir()
    runner = FakeLaunchctl(bootstrap_code=1, bootstrap_err="5: Input/output error")
    service, agents = _service(tmp_path, runner=runner)
    result = service.enable()
    assert result["ok"] is False
    assert "Input/output error" in result["error"]
    assert list(agents.iterdir()) == [], \
        "a plist launchd refused to load is a lie waiting for the next reboot"


def test_disable_removes_and_proves_it_removed(tmp_path):
    (tmp_path / "python").write_text("", encoding="utf-8")
    (tmp_path / "project").mkdir()
    runner = FakeLaunchctl()
    service, agents = _service(tmp_path, runner=runner)
    service.enable()
    result = service.disable()
    assert result["ok"] is True
    assert not (agents / "com.jarvisforge.vantage.plist").exists()
    assert service.status()["installed"] is False


def test_disable_does_not_claim_removal_when_launchd_refuses(tmp_path):
    (tmp_path / "python").write_text("", encoding="utf-8")
    (tmp_path / "project").mkdir()
    runner = FakeLaunchctl(bootout_code=1)
    service, agents = _service(tmp_path, runner=runner)
    service.enable()
    result = service.disable()
    assert result["ok"] is False
    assert (agents / "com.jarvisforge.vantage.plist").exists(), \
        "the file stays when the service is still loaded -- deleting it would leave an " \
        "unloadable agent that still runs until reboot"


def test_disabling_nothing_is_not_a_failure(tmp_path):
    runner = FakeLaunchctl()
    service, agents = _service(tmp_path, runner=runner)
    (tmp_path / "python").write_text("", encoding="utf-8")
    result = service.disable()
    assert result["ok"] is True
    assert result["state"] == "not_installed"
    verbs = [c[1] for c in runner.calls]
    assert "bootout" not in verbs and "bootstrap" not in verbs, \
        "nothing to take down, so launchd is asked what it thinks but never told anything"


def test_status_asks_the_machine_every_time(tmp_path):
    runner = FakeLaunchctl()
    service, agents = _service(tmp_path, runner=runner)
    before = len(runner.calls)
    service.status()
    service.status()
    assert len(runner.calls) - before == 1 or len(runner.calls) > before, \
        "a cached answer outlives the user turning it off in System Settings"


def test_the_state_distinguishes_the_four_real_situations(tmp_path):
    (tmp_path / "python").write_text("", encoding="utf-8")
    service, agents = _service(tmp_path, runner=FakeLaunchctl(print_reply="not found"))
    assert service.status()["state"] == "not_installed"
    service.enable()
    assert service.status()["state"] == "loaded"
    service.disable()
    assert service.status()["state"] == "not_installed"


# ---------------------------------------------------------------------------
# 6. the path guard did not move
# ---------------------------------------------------------------------------


def test_the_file_tools_still_refuse_to_write_into_launch_agents():
    from jarvis.security.paths import protection_reason

    reason = protection_reason("~/Library/LaunchAgents/com.evil.plist")
    assert reason, "persistence stays refused to an ordinary write, phase 57 included"
    assert protection_reason("~/Library/LaunchDaemons/com.evil.plist")


def test_the_persistence_module_does_not_route_through_the_file_tools():
    """If it did, it would be blocked by the guard above -- and if the guard were widened to
    let it through, every sentence would gain the power to install a login item."""
    import inspect

    from jarvis import persistence

    source = inspect.getsource(persistence)
    assert "tools.files" not in source
    assert "import files" not in source


# ---------------------------------------------------------------------------
# 7. governed, reachable, and not a sentence
# ---------------------------------------------------------------------------


def test_the_three_capabilities_are_registered_with_their_gates():
    from jarvis.tools import build_default_registry
    from jarvis.core.models import RiskLevel

    registry = build_default_registry()
    status = registry.get("get_host_persistence_status")
    enable = registry.get("enable_host_persistence")
    disable = registry.get("disable_host_persistence")
    assert status.risk is RiskLevel.READ
    for spec in (enable, disable):
        assert spec.risk is RiskLevel.EXTERNAL, \
            "EXTERNAL is what changes something outside the process; every EXTERNAL tool " \
            "in this registry is confirmation-gated"
        assert spec.requires_confirmation is True
    assert status.requires_confirmation is False


def test_a_registry_row_is_not_a_second_source_of_truth():
    """Tool count is measured, never frozen; this asserts the invariant instead."""
    from jarvis.tools import build_default_registry

    registry = build_default_registry()
    specs = list(registry.all())
    names = [s.name for s in specs]
    assert len(names) == len(set(names))
    assert {s.name for s in specs} == set(registry.names())


@pytest.mark.parametrize("sentence", [
    "start vantage on login",
    "keep running when I close the window",
    "make vantage a login item",
    "enable host persistence",
    "does vantage start on login",
])
def test_an_ambiguous_sentence_about_startup_gets_the_state_not_the_change(sentence):
    """"Do I start at login" and "make me start at login" are the same words to a regex, and
    only one of them is a question. The read is what an ambiguous phrasing earns: a login item
    is the one capability whose effect is that something runs when nobody is watching, so a
    mis-parse must not be able to install it."""
    from jarvis.core.planner import RulePlanner
    from jarvis.tools import build_default_registry

    plan = RulePlanner(build_default_registry()).parse(sentence)
    for action in plan.actions:
        assert action.tool != "enable_host_persistence"
        assert action.tool != "disable_host_persistence"


@pytest.mark.parametrize("sentence,tool", [
    ("install the login item", "enable_host_persistence"),
    ("remove the login item", "disable_host_persistence"),
])
def test_the_explicit_command_reaches_the_tool_and_cannot_run_without_a_card(sentence, tool):
    """The control is the confirmation gate, not a ban on the words -- which is how the other
    27 gated capabilities work. Deleting a file is reachable by sentence because a person
    approves it first; persistence is reachable the same way, and a route that reached it past
    the card would be the actual defect."""
    from jarvis.core.planner import RulePlanner
    from jarvis.tools import build_default_registry

    registry = build_default_registry()
    plan = RulePlanner(registry).parse(sentence)
    assert [a.tool for a in plan.actions] == [tool]
    assert registry.get(tool).requires_confirmation is True


def test_a_sentence_can_still_ask_where_the_laptop_will_be_running():
    from jarvis.core.planner import RulePlanner
    from jarvis.tools import build_default_registry

    plan = RulePlanner(build_default_registry()).parse("does vantage start on login")
    assert [a.tool for a in plan.actions] == ["get_host_persistence_status"]


def test_the_dashboard_shows_the_measured_state_and_offers_the_switch():
    page = (Path(__file__).resolve().parents[1] / "dashboard" / "index.html").read_text(
        encoding="utf-8")
    assert "/persistence" in page
    assert "start on login" in page.lower() or "login item" in page.lower()
    assert "KeepAlive" not in page, "launchd vocabulary belongs in the module, not the page"


# ---------------------------------------------------------------------------
# 8. the server itself: one process per data directory, and no restart loop
# ---------------------------------------------------------------------------


def test_the_server_claims_the_directory_before_it_binds(tmp_path):
    from jarvis import persistence

    home = tmp_path / "home"
    home.mkdir()
    port = free_port()
    seen = {}

    def serve(host, port):
        # What the server sees while it runs is the point: the claim has to exist before the
        # port does, or two processes can both pass a port check.
        seen["claim"] = persistence.read_claim(home)

    ok, code = persistence.serve_with_claim(home, pid=321, host="127.0.0.1", port=port,
                                           serve=serve)
    assert ok is True
    assert code == 0
    assert seen["claim"]["pid"] == 321
    assert persistence.read_claim(home) is None, "the claim ends when the server does"


def test_a_second_server_is_refused_before_it_binds(tmp_path, monkeypatch):
    from jarvis import persistence

    home = tmp_path / "home"
    home.mkdir()
    port = free_port()  # the realistic case is the same directory *and* the same port
    persistence.acquire_claim(home, pid=111, host="127.0.0.1", port=port)
    monkeypatch.setattr(persistence, "_alive", lambda pid: True)
    calls = []

    def serve(host, port):
        calls.append((host, port))

    ok, code = persistence.serve_with_claim(home, pid=222, host="127.0.0.1",
                                           port=port, serve=serve)
    assert ok is False
    assert code == 0, "a refused second instance is not a crash, so it must not restart"
    assert calls == [], "it never reached the bind"
    assert persistence.read_claim(home)["pid"] == 111, "the refusal must not steal the lock"


def test_a_port_conflict_exits_cleanly_because_launchd_would_otherwise_loop(tmp_path):
    """``KeepAlive = {SuccessfulExit = false}`` means a non-zero exit is relaunched. A port that
    somebody else holds will never free up by trying again, so the honest exit code for a bind
    failure is success: stay down, and say why in the log."""
    from jarvis import persistence

    home = tmp_path / "home"
    home.mkdir()

    def serve(host, port):
        raise OSError(98, "Address already in use")

    ok, code = persistence.serve_with_claim(home, pid=321, host="127.0.0.1",
                                           port=free_port(), serve=serve)
    assert ok is True
    assert code == 0
    assert persistence.read_claim(home) is None


def test_the_port_is_probed_before_the_application_starts(tmp_path):
    """Found live, not in the suite: uvicorn does not raise on a busy port, it runs the whole
    application first, prints "Application startup complete", starts the scheduler, and *then*
    fails to bind and exits 3. So a losing second instance had already attached a scheduler to the
    shared database, and under KeepAlive it would be retried forever. The port has to be asked
    before the app is allowed to wake up."""
    import socket

    from jarvis import persistence

    home = tmp_path / "home"
    home.mkdir()
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    taken = squatter.getsockname()[1]

    called = []

    def serve(host, port):
        called.append(port)

    try:
        ok, code = persistence.serve_with_claim(home, pid=321, host="127.0.0.1", port=taken,
                                               serve=serve)
        assert called == [], "the application must not start on a port nobody can reach"
        assert code == 0
        assert persistence.read_claim(home) is None, "a refusal leaves no claim"
    finally:
        squatter.close()


def test_a_real_crash_is_not_reported_as_a_clean_stop(tmp_path):
    """Distinguishing the two is the whole point of the exit code: a crash should be retried by
    launchd, a conflict should not."""
    from jarvis import persistence

    home = tmp_path / "home"
    home.mkdir()

    def serve(host, port):
        raise RuntimeError("the database could not be opened")

    with pytest.raises(RuntimeError):
        persistence.serve_with_claim(home, pid=321, host="127.0.0.1", port=free_port(),
                                     serve=serve)
    assert persistence.read_claim(home) is None, "a crash must not leave the lock behind"


def test_the_run_script_hands_the_bind_to_the_claiming_wrapper():
    """run.py keeps no lifecycle logic of its own, or the guard above can be bypassed by the
    one caller that matters."""
    import inspect

    import run as run_module

    source = inspect.getsource(run_module)
    assert "serve_with_claim" in source
    assert "start_guardian_from_env" in source, \
        "the panel's watchdog stays -- this change must not resurrect the orphan server"


def test_the_run_script_probes_the_port_before_printing_success():
    """Found live: a stale backend holding :8765 made a second `run.py` print the
    "Vantage core -> .../dashboard" banner, then fail to bind and exit quietly -- so the
    browser hit the OLD process, which 404'd on /dashboard. The port has to be probed and
    refused *before* the success banner prints, or a losing second start masquerades as a
    working start and the user chases a 404 from a dead process."""
    import inspect

    import run as run_module

    source = inspect.getsource(run_module)
    # The success banner is the `print(f"Vantage core ...` line. The port probe and its
    # "REFUSED to start" refusal must both precede it, or a second start against an
    # already-bound port prints success and then exits -- leaving the stale server
    # answering with a 404. (Match the print statement itself, not the word in a comment.)
    banner = source.index('print(f"Vantage core')
    probe = source.index("port_is_free(probe_host")
    refuse = source.index("REFUSED to start")
    assert probe < banner, (
        "the port must be probed before the success banner prints, or a losing second "
        "start prints success and then exits")
    assert refuse < banner, (
        "the busy-port refusal must print before the success banner, so a stale backend "
        "can't masquerade as a working start")
    assert "SystemExit(1)" in source


def test_launchd_is_asked_about_the_job_not_about_the_whole_domain(tmp_path, monkeypatch):
    """Measured on the real machine, and it is the difference between a working status line and
    a lie. ``launchctl print gui/<uid> <label>`` does not fail: it prints the *domain* dump
    ("service count = 444") and exits 0. Treating exit 0 as "loaded" therefore reports every
    machine as running the job, and ``pid`` can be picked up from some unrelated service in that
    dump. The target has to be one argument."""
    from jarvis import persistence

    seen = []

    def runner(argv):
        seen.append(list(argv))
        return 0, "gui/501 = {\n\ttype = login\n\tservice count = 444\n\tpid = 99\n}", ""

    (tmp_path / "python").write_text("", encoding="utf-8")
    service, agents = _service(tmp_path, runner=runner)
    status = service.status()
    assert seen[0][1:] == ["print", f"gui/{persistence.os.getuid()}/com.jarvisforge.vantage"], \
        "the label must be part of the target, or launchd answers about the domain"
    assert status["loaded"] is False, "an answer that is not about this job is not evidence"
    assert status["pid"] == 0


def test_an_exit_zero_answer_saying_not_found_is_not_loaded(tmp_path):
    """``launchctl print`` for a service that is not there prints "Bad request. / Could not find
    service" and still exits 0 on this macOS. The exit code carries no information here."""
    from jarvis import persistence

    def runner(argv):
        return 0, "Bad request.\nCould not find service \"com.evil\" in domain", ""

    service, agents = _service(tmp_path, runner=runner)
    status = service.status()
    assert status["loaded"] is False
    assert status["state"] == "not_installed"


def test_disable_waits_for_launchd_to_finish_tearing_the_job_down(tmp_path):
    """Found by running it against the real launchd: ``bootout`` returned 0, and the single
    re-read made immediately afterwards still found the job in the table -- so ``disable`` reported
    a refusal about a job that had in fact stopped, and left the plist on disk. Teardown is not
    synchronous, so the read-back gets a short bounded window, the same shape Phase 34 gives a
    sentence: a few hundred milliseconds, not a retry ladder."""
    from jarvis import persistence

    class SlowTeardown(FakeLaunchctl):
        def __init__(self):
            super().__init__()
            self.after_bootout = 0

        def __call__(self, argv):
            joined = " ".join(argv)
            if " bootout" in joined:
                self.after_bootout = 2          # still in the table for two reads
                self.pending_gone = ("com.jarvisforge.vantage = {\n\tstate = running\n"
                                     "\t\tpid = 4242\n}")
                self.print_reply = "Could not find service"
                return 0, "", ""
            if " print " in joined and self.after_bootout:
                self.after_bootout -= 1
                return 0, self.pending_gone, ""
            return super().__call__(argv)

    (tmp_path / "python").write_text("", encoding="utf-8")
    service, agents = _service(tmp_path, runner=SlowTeardown())
    service.enable()
    result = service.disable()
    assert result["ok"] is True, result
    assert not (agents / "com.jarvisforge.vantage.plist").exists()
    assert service.status()["state"] == "not_installed"


def test_a_teardown_that_never_settles_is_reported_as_a_failure(tmp_path):
    """The window bounds the wait; it does not decide the answer. A job launchd will not give up
    stays a refusal with the file left where it is."""
    from jarvis import persistence

    stubborn = FakeLaunchctl(print_reply="com.jarvisforge.vantage = {\n\tpid = 4242\n}")

    def runner(argv):
        joined = " ".join(argv)
        if " bootout" in joined:
            return 0, "", ""                     # accepts the request, keeps the job anyway
        return stubborn(argv)

    service, agents = _service(tmp_path, runner=runner)
    service.enable()
    result = service.disable(settle=0.2)
    assert result["ok"] is False
    assert (agents / "com.jarvisforge.vantage.plist").exists()
    assert "still" in result["error"].lower() or "loaded" in result["error"].lower()


# ---------------------------------------------------------------------------
# 9. the read-only surface the panel polls
# ---------------------------------------------------------------------------


def _client(headers=True):
    import importlib

    from fastapi.testclient import TestClient

    from tests.conftest import AUTH_HEADERS

    module = importlib.import_module("jarvis.api.app")
    # The loopback base_url matters: the host gate refuses a request addressed at
    # "testserver", so a client built without one reads a refusal, not a payload.
    return TestClient(module.app, base_url="http://127.0.0.1:8765",
                      headers=AUTH_HEADERS if headers else {})


def test_the_surface_reports_the_state_and_who_was_asked():
    with _client() as client:
        body = client.get("/persistence").json()
    for key in ("state", "installed", "loaded", "label", "answered_by", "supported"):
        assert key in body, body
    assert body["label"] == "com.jarvisforge.vantage"
    assert "launchctl" in body["answered_by"], \
        "a state with no subject is a claim about nothing -- Phase 50's rule"


def test_the_surface_needs_a_token():
    with _client(headers=False) as client:
        assert client.get("/persistence").status_code == 401


def test_the_surface_cannot_install_anything():
    """The page polls this route. If a poll could install a login item, the dashboard would be
    asking for persistence on nobody's behalf -- the same rule Phase 50 set for permissions."""
    with _client() as client:
        assert client.post("/persistence").status_code == 405
        assert client.post("/persistence/enable").status_code == 404


# ---------------------------------------------------------------------------
# 10. a card that polls must not erase the answer it was just given
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn,card,note", [
    ("loadPermissions", "permissions", "permNote"),
    ("loadPersistence", "persistence", "persistNote"),
])
def test_the_answer_to_a_press_survives_the_refresh_that_follows_it(fn, card, note):
    """Found in the browser, not in the suite: the press writes its reply into a note rendered
    *inside* the card, and the very next line re-renders that card from the re-read -- so the
    answer is replaced by its own default text the instant it arrives. On screen it looks like
    nothing happened. The note has to live outside the container a poll rewrites."""
    page = (Path(__file__).resolve().parents[1] / "dashboard" / "index.html").read_text(
        encoding="utf-8")
    start = page.index(f"async function {fn}(")
    # The reflowed page indents functions inconsistently, so "up to the next column-0
    # function" no longer works. Extract the body by its braces instead: the extent is
    # determined by depth, not by the page's layout.
    open_brace = page.index("{", start)
    depth = 0
    end = None
    for j in range(open_brace, len(page)):
        if page[j] == "{":
            depth += 1
        elif page[j] == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    assert end is not None, f"could not find the end of {fn}()"
    body = page[start:end]
    assert f'id="{note}"' not in body, (
        f"{fn}() rewrites #{card}'s innerHTML, so a note rendered inside it is reset to its "
        f"default by the re-read that follows the press")
    assert page.count(f'id="{note}"') == 1, "the note still has to exist, outside the card"
