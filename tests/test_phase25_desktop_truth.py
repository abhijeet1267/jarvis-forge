"""Phase 25: the desktop app, measured instead of carried forward.

For fourteen phases the backlog said "desktop bundle exits 1", inherited from Phase 9's
P0-D1 -- a build whose ``project_path.txt`` pointed at the pre-relocation tree, so the app
died before it could say why. Building and launching a fresh bundle showed that half is
long fixed: the app starts, its server answers ``/health``, and ``JARVIS_SELFTEST=1``
prints a nine-check report.

Two things were still true, and both are the kind that hides behind a stale summary:

* **P0-D3, unfixed and masked.** ``main()`` resolved the project directory *before* the
  self-test branch, and on failure showed an alert and returned 1. So the diagnostic you
  reach for exactly when the app will not start was the one thing a broken install
  prevented from running -- and a working install cannot see it.
* **P1-D1, a promise no code keeps.** ``install_app.sh`` told the user "First launch will
  ask for Microphone and Speech Recognition permission." Nothing in the tree calls
  ``AVCaptureDevice.requestAccess`` -- the helper deliberately queries rather than asks --
  and the self-test measures ``denied``/``blocked`` with no prompt ever shown.

No test here opens a window: the alert path and the Tk probe are both patched, and the
report's rows are checked through the functions that build them.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "jarvis_launcher_phase25", REPO / "app" / "launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _controller(launcher, project_dir: Path, error: str = ""):
    controller = launcher.ServerController(
        project_dir, sys.executable, "127.0.0.1", 8765, project_dir / "app.log")
    controller.project_dir_error = error
    return controller


# ---------------------------------------------------------------------------
# 25-A -- the self-test has to run when the install is broken, because that is
#         the only moment anyone reaches for it
# ---------------------------------------------------------------------------


def test_the_selftest_runs_even_when_the_project_directory_cannot_be_found(monkeypatch):
    launcher = _launcher()
    calls = {"selftest": 0, "start": 0}

    def boom() -> Path:
        raise RuntimeError("Could not locate the jarvis-forge project directory.")

    monkeypatch.setattr(launcher, "resolve_project_dir", boom)
    monkeypatch.setattr(launcher, "resolve_python", lambda project_dir: sys.executable)
    monkeypatch.setattr(launcher, "_fatal", lambda message: None)

    def fake_start(self):
        calls["start"] += 1

    def fake_selftest(controller):
        calls["selftest"] += 1
        assert getattr(controller, "project_dir_error", ""), (
            "the self-test ran but lost the reason this install failed")
        assert calls["start"] == 0, "a server was started for a project that was not found"
        return 1

    monkeypatch.setattr(launcher.ServerController, "start", fake_start)
    monkeypatch.setattr(launcher.ServerController, "stop", lambda self: None)
    monkeypatch.setattr(launcher, "selftest", fake_selftest)
    monkeypatch.setenv("JARVIS_SELFTEST", "1")

    rc = launcher.main()

    assert calls["selftest"] == 1, (
        "a bundle that cannot locate its project still exits before the self-test runs "
        "(P0-D3): the diagnostic is gated behind the failure it exists to explain")
    assert rc == 1, "a failing self-test must not report success"


def test_the_report_says_which_checks_it_could_not_run(tmp_path, monkeypatch, capsys):
    """"3/5 checks passed" must not read like a smaller suite ran fine.

    The denominator shrinks because the server never started, and the four voice rows
    were never attempted. Saying so keeps the count honest; leaving it out lets a
    broken install look like a healthy three-check run.
    """
    launcher = _launcher()
    controller = _controller(launcher, tmp_path, error="no project directory")

    monkeypatch.setattr(launcher, "gui_checks", lambda: [("tkinter / Tk", True, "stub")])
    monkeypatch.setattr(launcher, "api_get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")))

    launcher.selftest(controller)
    out = capsys.readouterr().out

    assert "not attempted" in out, out[-400:]
    for name in ("text to speech", "speech to text", "microphone"):
        assert name in out.split("not attempted")[1], f"{name} was not named as skipped"
    assert "checks passed" in out


def test_the_normal_launch_path_still_refuses_to_start_without_a_project(monkeypatch):
    """The fix must not let a real launch proceed on a broken install."""
    launcher = _launcher()
    alerts = []

    def boom() -> Path:
        raise RuntimeError("Could not locate the jarvis-forge project directory.")

    monkeypatch.setattr(launcher, "resolve_project_dir", boom)
    monkeypatch.setattr(launcher, "_fatal", alerts.append)
    monkeypatch.delenv("JARVIS_SELFTEST", raising=False)

    assert launcher.main() == 1
    assert alerts, "a plain launch silently ignored an unresolvable project directory"


# ---------------------------------------------------------------------------
# 25-B -- the report says *why*, and never waits for a server it did not start
# ---------------------------------------------------------------------------


def test_the_project_row_of_the_report_carries_the_reason(tmp_path):
    launcher = _launcher()
    controller = _controller(launcher, tmp_path,
                             error="Could not locate the jarvis-forge project directory.")

    name, ok, detail = launcher.project_dir_check(controller)

    assert name == "project directory"
    assert ok is False
    assert "Could not locate" in detail, (
        f"the row prints a path the user already knows is wrong: {detail}")


def test_a_healthy_install_still_reports_the_directory_it_found(tmp_path):
    launcher = _launcher()
    (tmp_path / "jarvis").mkdir()
    name, ok, detail = launcher.project_dir_check(_controller(launcher, tmp_path))

    assert (name, ok) == ("project directory", True)
    assert str(tmp_path) in detail


def test_a_controller_that_never_started_is_not_waited_on(tmp_path):
    launcher = _launcher()
    broken = _controller(launcher, tmp_path, error="no project directory")
    healthy = _controller(launcher, tmp_path)

    assert launcher.server_is_reachable(healthy) is True
    assert launcher.server_is_reachable(broken) is False, (
        "the report would poll a port nothing owns for the full timeout")


def test_the_report_does_not_spend_its_timeout_on_a_dead_install(tmp_path, monkeypatch):
    launcher = _launcher()
    controller = _controller(launcher, tmp_path, error="no project directory")
    polled = []

    def fake_wait(self, timeout: float = 45.0) -> bool:
        polled.append(timeout)
        return False

    monkeypatch.setattr(launcher.ServerController, "wait_ready", fake_wait)
    monkeypatch.setattr(launcher, "gui_checks", lambda: [("tkinter / Tk", True, "stubbed")])
    monkeypatch.setattr(
        launcher, "api_get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the network was probed")))

    started = time.time()
    launcher.selftest(controller)
    elapsed = time.time() - started

    assert not polled, "wait_ready was called for a server that was never started"
    assert elapsed < 10, f"the self-test still blocked for {elapsed:.0f}s"


# ---------------------------------------------------------------------------
# 25-C -- what an installed bundle needs in order to find its source
# ---------------------------------------------------------------------------


def test_an_installed_bundle_uses_the_pointer_recorded_at_build_time(tmp_path, monkeypatch):
    """The resolution order, pinned: a copy in /Applications has no sibling checkout."""
    launcher = _launcher()
    source = tmp_path / "checkout"
    (source / "jarvis").mkdir(parents=True)
    (source / "run.py").write_text("# payload\n")

    installed = tmp_path / "Applications" / "Vantage.app" / "Contents" / "Resources"
    installed.mkdir(parents=True)
    (installed / "project_path.txt").write_text(str(source))

    # BUNDLE_ROOT is ".../Vantage.app/Contents" (launcher.py lives in Contents/Resources)
    monkeypatch.setattr(launcher, "BUNDLE_ROOT", installed.parent)
    monkeypatch.delenv("JARVIS_PROJECT_DIR", raising=False)

    assert launcher.resolve_project_dir() == source


def test_a_bundle_sitting_in_dist_finds_its_checkout_without_a_pointer(tmp_path, monkeypatch):
    """The relocation-proof branch: the source is an ancestor, not a recorded string."""
    launcher = _launcher()
    source = tmp_path / "checkout"
    (source / "jarvis").mkdir(parents=True)
    (source / "run.py").write_text("# payload\n")
    resources = source / "dist" / "Vantage.app" / "Contents" / "Resources"
    resources.mkdir(parents=True)

    monkeypatch.setattr(launcher, "BUNDLE_ROOT", resources.parent)
    monkeypatch.delenv("JARVIS_PROJECT_DIR", raising=False)

    assert launcher.resolve_project_dir() == source.resolve()


def test_a_stale_pointer_says_what_to_do_about_it(tmp_path, monkeypatch):
    launcher = _launcher()
    resources = tmp_path / "App.app" / "Contents" / "Resources"
    resources.mkdir(parents=True)
    (resources / "project_path.txt").write_text("/Users/someone/AppData/aisys/jarvis-forge")

    monkeypatch.setattr(launcher, "BUNDLE_ROOT", resources.parent)
    monkeypatch.delenv("JARVIS_PROJECT_DIR", raising=False)

    with pytest.raises(RuntimeError) as raised:
        launcher.resolve_project_dir()

    message = str(raised.value)
    assert "JARVIS_PROJECT_DIR" in message and "build_app.py" in message, (
        f"the failure names no way out: {message}")


def test_an_explicit_override_beats_a_stale_pointer(tmp_path, monkeypatch):
    """The escape hatch exists, so it must actually work -- measured, not assumed."""
    launcher = _launcher()
    good = tmp_path / "moved-checkout"
    (good / "jarvis").mkdir(parents=True)
    resources = tmp_path / "App.app" / "Contents" / "Resources"
    resources.mkdir(parents=True)
    (resources / "project_path.txt").write_text("/nonexistent/old/tree")

    monkeypatch.setattr(launcher, "BUNDLE_ROOT", resources.parent)
    monkeypatch.setenv("JARVIS_PROJECT_DIR", str(good))

    assert launcher.resolve_project_dir() == good.resolve()


# ---------------------------------------------------------------------------
# 25-D -- the installer may not promise a prompt the app never shows
# ---------------------------------------------------------------------------


def test_the_installer_does_not_promise_a_permission_prompt():
    script = (REPO / "install_app.sh").read_text()

    assert "will ask for Microphone" not in script, (
        "the installer still promises a first-launch prompt; nothing in the tree calls "
        "AVCaptureDevice.requestAccess, and the self-test measures denied/blocked")
    assert "Privacy & Security" in script, "it must at least give the manual route"
    assert "JARVIS_SELFTEST" in script, (
        "and name the one command that reports the real permission state")


def test_the_mic_helper_still_queries_rather_than_requests():
    """The design reason behind the false promise, kept honest in both directions.

    If someone later adds requestAccess, this test fails on purpose -- and the
    installer's wording changes back with it. A claim and the code behind it move
    together or not at all.
    """
    source = (REPO / "jarvis" / "voice" / "native" / "mic_state.swift").read_text()
    # Comments are where the omission is *explained*, so they are stripped before the
    # claim is checked: "requestAccess" appears in a comment precisely because it is
    # not called.
    code = "\n".join(line.split("//", 1)[0] for line in source.splitlines())

    assert "authorizationStatus" in code
    assert "requestAccess" not in code, (
        "the helper now asks for consent, so 'it never prompts' in the installer and this "
        "test both need updating -- deliberately, not by accident")
# ---------------------------------------------------------------------------
# 25-E -- the README's own counts, since this phase exists because a stale
#         audit claim was repeated for fifteen phases
# ---------------------------------------------------------------------------


def test_the_readme_counts_the_evidence_files_it_ships():
    """The layout line says "N phase reports + M CSV matrices"; it must be true.

    "desktop bundle exits 1" survived fifteen phases because nobody re-ran the check
    behind it. A count is the same kind of claim, and this repo is about not making
    those.
    """
    readme = (REPO / "README.md").read_text()
    reports = len(list((REPO / "audit").glob("*.md")))
    matrices = len(list((REPO / "audit").glob("*.csv")))

    assert f"{reports} phase reports + {matrices} CSV matrices" in readme, (
        f"audit/ holds {reports} reports and {matrices} matrices; the README says otherwise")


def test_the_readme_does_not_still_call_the_bundle_broken():
    readme = (REPO / "README.md").read_text()

    assert "BROKEN — exits 1" not in readme, "the desktop row was measured in Phase 25"
    assert "exits 1 on launch" not in readme, "same claim, reworded, in Downloads"


def test_the_readme_counts_the_planner_routes_it_ships():
    """"40 deterministic routes" was true until a rule was added, and stayed printed.

    The badge rows are the numbers a reader quotes to someone else, so they age in the
    loudest possible way: every later phase inherits them as fact.
    """
    import re

    from jarvis.core.orchestrator import JarvisCore

    readme = (REPO / "README.md").read_text()
    claimed = int(re.search(r"RulePlanner` — (\d+) deterministic routes", readme).group(1))
    measured = len(JarvisCore().planner._rules)

    assert claimed == measured, (
        f"the planner has {measured} routes; the README still advertises {claimed}")


def test_the_bundle_build_declares_the_dependency_it_needs():
    """``python build_app.py`` on a fresh clone used to fail, and nothing said why.

    The icon step shells out to app/make_icon.py, which imports Pillow; build_app.py
    raises if that subprocess fails, and Pillow was not in requirements.txt. A
    documented build command that only works on a machine with an undeclared package
    installed is the same species of claim as "the bundle exits 1" -- true once,
    repeated forever.
    """
    icon = (REPO / "app" / "make_icon.py").read_text()
    builder = (REPO / "build_app.py").read_text()
    reqs = (REPO / "requirements.txt").read_text().lower()

    assert "from PIL import" in icon
    assert "raise RuntimeError" in builder, "the build now tolerates a missing icon silently"
    assert "pillow" in reqs, "make_icon.py needs Pillow; requirements.txt must say so"


def test_ci_builds_the_bundle_it_documents():
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()

    assert "python build_app.py" in workflow, (
        "the README tells readers to build the app; CI is what proves that still works")


# ---------------------------------------------------------------------------
# 25-F -- the README's other published counts, guarded the same way
# ---------------------------------------------------------------------------
def test_the_readme_counts_the_api_routes_it_ships():
    """The README published **118** API routes; the app has more than that.

    The route count was the one badge row with no guard, so it drifted the
    furthest -- the tool count and the planner-route count were both pinned,
    and the route count was not. It is pinned now.
    """
    import re

    readme = (REPO / "README.md").read_text()
    claimed = int(re.search(r"API routes\s*\|\s*\*\*(\d+)\*\*", readme).group(1))
    source = (REPO / "jarvis" / "api" / "app.py").read_text()
    measured = sum(source.count(f'@app.{verb}("')
                   for verb in ("get", "post", "put", "patch", "delete"))

    assert claimed == measured, (
        f"the app declares {measured} API routes; the README still advertises {claimed}")


def test_the_readme_counts_the_gated_tools_out_of_the_whole():
    """A gate count that is out of date is a gate count nobody can trust.

    The README said "32 of 148" when the registry held 35 gated tools out of
    154. The number a reader quotes is the number that has to be right.
    """
    import re

    from jarvis.tools import build_default_registry

    readme = (REPO / "README.md").read_text()
    match = re.search(r"Confirmation-gated\s*\|\s*\*\*(\d+)\*\*\s*of\s*(\d+)", readme)
    assert match, "the README no longer publishes a gated-tool count"
    claimed_gated, claimed_total = int(match.group(1)), int(match.group(2))

    registry = build_default_registry()
    specs = registry.all()
    measured_gated = sum(1 for s in specs if s.requires_confirmation)
    measured_total = len(specs)

    assert (claimed_gated, claimed_total) == (measured_gated, measured_total), (
        f"the registry holds {measured_gated} gated tools out of {measured_total}; "
        f"the README says {claimed_gated} of {claimed_total}")


def test_the_readme_counts_the_risk_tiers_it_ships():
    """The risk tiers are the table that says what a sentence can reach."""
    import re

    from jarvis.core.models import RiskLevel
    from jarvis.tools import build_default_registry

    readme = (REPO / "README.md").read_text()
    match = re.search(r"Risk tiers\s*\|\s*read (\d+) · low (\d+) · external (\d+) · "
                      r"destructive (\d+) · physical (\d+)", readme)
    assert match, "the README no longer publishes the risk tiers"
    claimed = tuple(int(g) for g in match.groups())

    registry = build_default_registry()
    measured = tuple(
        sum(1 for s in registry.all() if s.risk is tier)
        for tier in (RiskLevel.READ, RiskLevel.LOW, RiskLevel.EXTERNAL,
                     RiskLevel.DESTRUCTIVE, RiskLevel.PHYSICAL)
    )

    assert claimed == measured, (
        f"the registry's risk tiers are {measured}; the README says {claimed}")


def test_the_release_doc_publishes_the_current_suite_figures():
    """The release document's numbers are the ones someone installs from.

    It carried "2800 passed / 38 skipped" while the suite was at 2992/33, and
    the test that guards the README's suite count did not exist, so nothing
    noticed.
    """
    import re

    readme = (REPO / "README.md").read_text()
    release = (REPO / "docs" / "RELEASE_AND_BUILD.md").read_text()

    # The number the release doc publishes must be the number the README
    # publishes: they are the same suite.
    def _suite_row(text):
        match = re.search(r"Test suite\s*\|\s*\*\*(\d+) passed", text)
        assert match, "no published suite count"
        return int(match.group(1))

    assert _suite_row(release) == _suite_row(readme), (
        "the release document and the README publish different suite counts")

    # And the skipped figure must not be published as zero.
    match = re.search(r"Test suite\s*\|\s*\*\*\d+ passed · (\d+) failed · (\d+) skipped",
                      release)
    assert match, "the release document no longer publishes the skipped count"
    skipped = int(match.group(2))
    assert skipped > 0, (
        "the release document publishes 0 skipped, but the suite skips "
        "environment-gated tests; a reader would look for failures that are "
        "not failures")
