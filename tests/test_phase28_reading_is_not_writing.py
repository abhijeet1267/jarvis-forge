"""Phase 28: asking to read something must not make JARVIS write something.

Measured on the live tree before anything was changed:

    >>> "read the readme"
        intent=create_documentation  decision=EXECUTE  tools=['documentation_created']
        reply='Created documentation: read the readme (markdown) -- read-the-readme-2340c886.md'
    >>> "show me the readme"   -> Created documentation: show me the readme …
    >>> "open README.md"       -> Created documentation: open README.md …

The cause is one regex in the design route: ``document\\w*|readme|guide`` matches
the *object* of the sentence and nothing checks its *verb*, so any sentence
containing "readme" -- including the ones that ask to read it -- authors a new
file and reports it as done. The word "read" is inside "readme".

That is the worst shape this program has found: not a wrong label, not a missing
capability, but an unrequested write, decided by a substring, with a success
message and a ledger row. And the sibling defect is that the read it was asking
for has *never* been reachable: ``read_file`` is registered, is risk tier READ,
and appears nowhere in the planner -- one of 29 registered tools no typed
sentence can reach.

The two rules these tests bind to:

* an artifact route may only author when the sentence asks for authoring;
* a sentence that asks to read reaches ``read_file``, or fails honestly.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from jarvis.config import get_settings
from jarvis.core.models import CommandRequest, PolicyDecision

REPO = Path(__file__).resolve().parents[1]

READ_PHRASINGS = [
    "read the readme",
    "read the README.md",
    "show me the readme",
    "what does the readme say",
    "open README.md",
    "read my notes.txt",
    "print the readme",
    "tell me what the guide says",
]

WRITE_PHRASINGS = [
    "write documentation for the parser",
    "create a readme for the project",
    "draft a slide deck about the release",
    "design a dashboard mockup for the console",
]


@pytest.fixture()
def workspace(tmp_area, monkeypatch) -> Path:
    monkeypatch.setattr(get_settings(), "file_workspace", str(tmp_area))
    (tmp_area / "README.md").write_text("# AMR main board\nbattery is at 44%\n")
    (tmp_area / "notes.txt").write_text("heater coil arrived\n")
    return tmp_area


def _run(core, text: str):
    return asyncio.run(core.handle(CommandRequest(text=text, source="phase28")))


# ---------------------------------------------------------------------------
# 28-A -- an authoring route needs an authoring verb
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", READ_PHRASINGS)
def test_reading_a_document_never_authors_one(core, workspace, text):
    before = sorted(p.name for p in workspace.iterdir())
    response = _run(core, text)

    authored = [a.tool for a in response.actions if a.tool.endswith("_created")]
    assert not authored, (
        f"{text!r} ran {authored} -- a request to read wrote a file instead")
    assert "Created documentation" not in response.reply, response.reply
    assert sorted(p.name for p in workspace.iterdir()) == before, "a file appeared on disk"


@pytest.mark.parametrize("text", WRITE_PHRASINGS)
def test_a_real_authoring_request_still_authors(core, workspace, text):
    """The guard must not have become "never create anything".

    Every one of these produced its artifact before the fix and must still do so.
    """
    response = _run(core, text)
    assert response.actions, response.reply
    assert any(a.tool.endswith("_created") for a in response.actions), (
        f"{text!r} no longer reaches an authoring tool: {[a.tool for a in response.actions]}")


@pytest.mark.parametrize("text", [
    "read the file README.md",
    "read README.md",
    "open README.md",
    "show me the README.md",
    "what does the file README.md say",
    "print README.md",
])
def test_the_obvious_reading_phrasings_all_reach_read_file(core, workspace, text):
    """Not writing was half the fix; the other half is that the read happens."""
    response = _run(core, text)
    assert [a.tool for a in response.actions] == ["read_file"], (
        f"{text!r} -> intent={response.intent!r} tools={[a.tool for a in response.actions]}")
    assert "battery is at 44" in response.reply, response.reply


def test_a_read_that_cannot_find_the_file_says_so_and_still_writes_nothing(core, workspace):
    response = _run(core, "read the file absent.txt")
    assert [a.tool for a in response.actions] == ["read_file"], response.reply
    assert "not found" in response.reply.lower(), response.reply
    assert sorted(p.name for p in workspace.iterdir()) == ["README.md", "notes.txt"]


def test_a_diagram_request_names_its_internal_parameter_parked_as_28B(core, workspace):
    """Parked, not fixed: "make a diagram of the build" answers with a schema
    field name rather than a question a human could answer.

    Asserted so the behaviour is visible in the suite instead of surprising
    someone later; the fix is a planner change about how missing required
    parameters are asked for, which is a separate decision.
    """
    response = _run(core, "make a diagram of the build pipeline")
    assert "components" in response.reply, response.reply
    assert response.decision is PolicyDecision.BLOCK, response.reply


# ---------------------------------------------------------------------------
# 28-B -- read_file becomes reachable from a sentence
# ---------------------------------------------------------------------------


def test_reading_a_named_file_reaches_read_file_and_returns_its_text(core, workspace):
    response = _run(core, "read the file README.md")
    assert [a.tool for a in response.actions] == ["read_file"], response.reply
    assert response.decision is PolicyDecision.EXECUTE, response.decision
    assert "battery is at 44%" in response.reply, response.reply


def test_asking_to_read_the_readme_now_reads_it(core, workspace):
    response = _run(core, "read the readme")
    assert [a.tool for a in response.actions] == ["read_file"], response.reply
    assert "AMR main board" in response.reply, response.reply


def test_a_read_needs_no_confirmation(core, workspace):
    """read_file is tier READ; a confirmation prompt for it would be a false alarm."""
    response = _run(core, "read notes.txt")
    assert response.needs_confirmation is False, response.reply
    assert response.actions[0].verification.value == "success", response.actions[0].summary


def test_a_missing_file_says_so_instead_of_creating_one(core, workspace):
    response = _run(core, "read the file absent.txt")
    assert [a.tool for a in response.actions] == ["read_file"], response.reply
    assert "Created" not in response.reply, response.reply
    assert "not found" in response.reply.lower() or "did not work" in response.reply.lower(), (
        response.reply)
    assert sorted(p.name for p in workspace.iterdir()) == ["README.md", "notes.txt"]


def test_a_bare_name_is_resolved_inside_the_workspace_not_the_cwd(core, workspace):
    """"read notes.txt" must not become "read whatever the process happens to sit
    next to", which is how a read returns an unrelated file's contents."""
    response = _run(core, "read notes.txt")
    assert "heater coil arrived" in response.reply, response.reply
    row = next(r for r in core.audit.recent(20) if r.get("tool") == "read_file")
    assert str(workspace) in json.loads(row["target"])["path"], row


def test_a_big_file_is_excerpted_and_says_so(core, workspace):
    """The reply is also what gets remembered, so an uncapped read would copy a
    whole file into memory. The excerpt is bounded and labelled."""
    (workspace / "big.txt").write_text("line\n" * 4000)
    response = _run(core, "read the file big.txt")
    assert [a.tool for a in response.actions] == ["read_file"], response.reply
    assert len(response.reply) < 4000, len(response.reply)
    assert "…" in response.reply or "truncated" in response.reply.lower(), response.reply[:200]
    assert response.actions[0].output["content"].startswith("line\n")


def test_an_absolute_path_is_honoured_by_the_route_and_guarded_by_the_engine(core, workspace):
    """Reads are not workspace-only the way writes are: the route anchors a *bare
    name*, but an explicit path is passed through and the filesystem guard
    answers for it. Refusing /etc/hosts at the route with a "will not create it
    by name" message would be nonsense about a read.

    Measured: the route claims it (``intent=read_file``) and the engine's own
    guard is what blocks -- ``/private is a protected system location`` -- with
    no action executed and nothing written.
    """
    response = _run(core, "read the file /etc/hosts")
    assert response.plan is not None and response.plan.intent == "read_file", response.reply
    assert "create it by name" not in response.reply, response.reply
    assert "protected system location" in response.reply, response.reply
    assert response.actions == [], [a.tool for a in response.actions]
    assert sorted(p.name for p in workspace.iterdir()) == ["README.md", "notes.txt"]


def test_a_relative_path_anchors_to_the_workspace_not_the_process_cwd(core, workspace):
    """One rule for names: bare or relative, they resolve inside the file
    workspace. Resolving them against the process instead would mean "read
    ./notes.txt" returning some unrelated file the server happens to sit next to
    -- the same class of surprise as a relative delete."""
    (workspace / "sub").mkdir()
    (workspace / "sub" / "notes.txt").write_text("relative anchor works\n")
    response = _run(core, "read ./sub/notes.txt")
    assert "relative anchor works" in response.reply, response.reply

    escaping = _run(core, "read ../../etc/hosts")
    assert escaping.plan is not None and escaping.plan.intent == "read_file"
    assert "workspace" not in escaping.reply, escaping.reply
    assert "Created" not in escaping.reply, escaping.reply
    assert sorted(p.name for p in workspace.iterdir()) == ["README.md", "notes.txt", "sub"]


# ---------------------------------------------------------------------------
# 28-C -- what the sweep says, and the claim it retires
# ---------------------------------------------------------------------------


def _unreachable_tools(core) -> list[str]:
    """Registered tools whose name appears nowhere in the planner as a route."""
    planner = (REPO / "jarvis" / "core" / "planner.py").read_text()
    return sorted(n for n in core.tools.names() if f'"{n}"' not in planner)


def test_read_file_is_reachable_now_that_the_route_exists(core):
    assert "read_file" not in _unreachable_tools(core), (
        "the README's long-standing 'no read_file route' item is still true")


def test_the_sweep_is_measured_not_improvised(core):
    """The 29 is the number this phase found; one of them is now routed.

    Pinning the count is deliberate: it is the debt ledger for this defect
    family, and it only moves when someone actually routes another capability (in
    which case they should update it on purpose, not watch it drift).
    """
    unreachable = _unreachable_tools(core)
    assert len(unreachable) == 28, unreachable
    assert "read_file" not in unreachable


def test_the_readme_no_longer_claims_there_is_no_read_file_route():
    readme = (REPO / "README.md").read_text()
    assert "no `read_file` route" not in readme, (
        "the open item was closed in code; the claim has to move with it")


def test_a_raising_rule_is_named_and_not_mistaken_for_no_match(core):
    """Phase 28 found a bug through this hole.

    A route that raised on every call was reported by the planner as "no rule
    matched", which is the same answer a non-matching rule gives -- so a broken
    append route looked like a sentence JARVIS could not understand. The failure
    now names the rule, at a level a quiet log still shows.

    Asserted against the logger directly because the project's logger does not
    propagate, which is why ``caplog`` sees nothing here.
    """
    import logging

    planner = core.planner

    def broken_rule(text, raw):
        raise RuntimeError("deliberate: simulates a route with a lost line")

    broken_rule.__name__ = "_broken_rule_for_test"
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("jarvis.core.planner")
    handler = _Capture(level=logging.WARNING)
    logger.addHandler(handler)
    planner._rules.insert(0, broken_rule)
    try:
        plan = planner.parse("what time is it")
    finally:
        planner._rules.remove(broken_rule)
        logger.removeHandler(handler)

    assert plan.intent == "time", plan.intent
    named = [r for r in records
             if getattr(r, "extra_fields", {}).get("rule") == "_broken_rule_for_test"]
    assert named, [r.getMessage() for r in records]
    assert "deliberate" in named[0].extra_fields["error"], named[0].extra_fields
    assert named[0].levelno >= logging.WARNING, named[0].levelno


def test_the_guard_is_a_verb_rule_not_a_word_list():
    """The fix is structural: the route asks whether the sentence is authoring,
    instead of adding another special case for the word "readme"."""
    planner = (REPO / "jarvis" / "core" / "planner.py").read_text()
    assert re.search(r"_AUTHOR_VERBS|_CONSUME_VERBS", planner), (
        "the design route should gate on the sentence's verb class")
