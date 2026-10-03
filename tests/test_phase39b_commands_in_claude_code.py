"""Phase 39b: giving commands to Claude Code, through the gate rather than around it.

The earlier ask was "give it next instruction as like continue, next prompt for that project".
That stayed unstarted in Phase 35 for one reason, recorded there: there was no entry point to
call. Measured again now:

```
~/.local/bin/claude            -> present, 2.1.260
claude --help                  -> -p/--print, --output-format json, --add-dir, -c/--continue
claude --help (note under -p)  -> "the workspace trust dialog is skipped when run in
                                  non-interactive mode"
--dangerously-skip-permissions -> exists. Never passed by this tool.
```

So the capability is real today. Which is exactly why it is confirmation-gated: a sentence from a
phone that starts a coding agent in a directory, with its trust prompt skipped and its own tool
access, is an action on the laptop. It reports what the agent *answered*, and says so when the
answer cannot be parsed, rather than treating "the process exited 0" as a job done.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from jarvis.core.models import PlannedAction, PolicyDecision, RiskLevel, Verification


def _ctx(core):
    from jarvis.tools.base import ToolContext

    return ToolContext(device_id="laptop_main", session_id="p39b")


def _fake_agent(monkeypatch, module, *, code=0, payload=None, out="", err=""):
    seen = {}

    async def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        seen["cwd"] = kw.get("cwd")
        body = json.dumps(payload) if payload is not None else out
        return code, body, err

    monkeypatch.setattr(module, "run_command", fake_run)
    return seen


# ---------------------------------------------------------------------------
# 1. the gate
# ---------------------------------------------------------------------------


def test_the_agent_tool_exists_and_is_confirmation_gated(core):
    spec = core.tools.get("run_coding_agent")
    assert spec is not None, "the tool must exist to answer the ask at all"
    assert spec.risk is RiskLevel.EXTERNAL, spec.risk
    assert spec.requires_confirmation is True, (
        "starting a coding agent in a directory is an action on the laptop")


def test_the_permission_engine_asks_before_it_runs(core, tmp_path):
    verdict = core.policy.evaluate(PlannedAction(
        tool="run_coding_agent",
        params={"prompt": "fix the failing tests", "path": str(tmp_path)},
        device="laptop_main"))
    assert verdict.decision is PolicyDecision.CONFIRM, verdict.decision


def test_a_protected_directory_is_blocked_not_merged_into_a_confirmation(core):
    """The gate asks before an action; it does not ask permission to touch what it
    already refuses."""
    verdict = core.policy.evaluate(PlannedAction(
        tool="run_coding_agent",
        params={"prompt": "delete the cache", "path": str(Path.home())},
        device="laptop_main"))
    assert verdict.decision is PolicyDecision.BLOCK, verdict.decision


def test_the_dangerous_flag_is_never_constructed(core, monkeypatch, tmp_path):
    """`--dangerously-skip-permissions` exists on this machine. The tool must not reach
    for it, and a test that only checks the happy path would not notice if it did."""
    from jarvis.tools import coding_agent as module

    monkeypatch.setattr(module, "agent_cli_path", lambda: "/usr/local/bin/claude")
    seen = _fake_agent(monkeypatch, module, payload={"type": "result", "result": "done",
                                                     "is_error": False})
    asyncio.run(module.run_coding_agent(_ctx(core), prompt="hi", path=str(tmp_path)))
    joined = " ".join(seen["argv"])
    assert "dangerously" not in joined, joined


# ---------------------------------------------------------------------------
# 2. the call it makes
# ---------------------------------------------------------------------------


def test_it_runs_inside_the_workspace_it_was_given(core, monkeypatch, tmp_path):
    from jarvis.tools import coding_agent as module

    monkeypatch.setattr(module, "agent_cli_path", lambda: "/usr/local/bin/claude")
    seen = _fake_agent(monkeypatch, module, payload={"type": "result", "result": "ok",
                                                     "is_error": False})
    asyncio.run(module.run_coding_agent(_ctx(core), prompt="list the tests",
                                        path=str(tmp_path)))
    assert seen["cwd"] == str(tmp_path), seen
    assert str(tmp_path) in seen["argv"], "the agent must be scoped to that directory"


def test_a_workspace_name_resolves_instead_of_being_asked_for(core, monkeypatch, tmp_path):
    from jarvis.tools import coding as coding_module
    from jarvis.tools import coding_agent as module

    # A name unique to this test: the session-scoped registry can hold an
    # `agent-test` registered by another file, and two same-named workspaces really
    # are ambiguous -- the tool refuses to guess, which is the correct answer.
    workspace = tmp_path / "agent-test-39b"
    workspace.mkdir()
    asyncio.run(coding_module.open_workspace(_ctx(core), path=str(workspace)))

    monkeypatch.setattr(module, "agent_cli_path", lambda: "/usr/local/bin/claude")
    seen = _fake_agent(monkeypatch, module, payload={"type": "result", "result": "ok",
                                                     "is_error": False})
    result = asyncio.run(module.run_coding_agent(_ctx(core), prompt="run the tests",
                                                 path="agent-test-39b"))
    assert result.ok is True, result.error or result.message
    assert seen["cwd"] == str(workspace), seen


def test_a_directory_that_is_not_there_runs_nothing(core, monkeypatch, tmp_path):
    from jarvis.tools import coding_agent as module

    monkeypatch.setattr(module, "agent_cli_path", lambda: "/usr/local/bin/claude")
    seen = _fake_agent(monkeypatch, module)
    result = asyncio.run(module.run_coding_agent(
        _ctx(core), prompt="hi", path=str(tmp_path / "ghost")))
    assert result.ok is False
    assert seen == {}, "an invented directory must not reach the agent"


def test_a_missing_agent_is_reported_as_a_capability_and_not_run(core, monkeypatch, tmp_path):
    from jarvis.tools import coding_agent as module

    monkeypatch.setattr(module, "agent_cli_path", lambda: None)
    seen = _fake_agent(monkeypatch, module)
    result = asyncio.run(module.run_coding_agent(_ctx(core), prompt="hi", path=str(tmp_path)))
    assert result.ok is False
    assert "claude" in (result.error or result.message).lower()
    assert seen == {}


# ---------------------------------------------------------------------------
# 3. the answer, and what counts as having got one
# ---------------------------------------------------------------------------


def test_the_agents_own_answer_is_what_comes_back(core, monkeypatch, tmp_path):
    from jarvis.tools import coding_agent as module

    monkeypatch.setattr(module, "agent_cli_path", lambda: "/usr/local/bin/claude")
    _fake_agent(monkeypatch, module, payload={
        "type": "result", "subtype": "success", "is_error": False,
        "result": "Fixed 3 failing tests in tests/test_app.py.",
        "num_turns": 4, "total_cost_usd": 0.012})
    result = asyncio.run(module.run_coding_agent(
        _ctx(core), prompt="fix the failing tests", path=str(tmp_path)))
    assert result.verification is Verification.SUCCESS
    assert "Fixed 3 failing tests" in result.message, result.message
    assert result.output["turns"] == 4


def test_an_error_result_from_the_agent_is_not_reported_as_success(core, monkeypatch, tmp_path):
    from jarvis.tools import coding_agent as module

    monkeypatch.setattr(module, "agent_cli_path", lambda: "/usr/local/bin/claude")
    _fake_agent(monkeypatch, module, payload={
        "type": "result", "is_error": True, "result": "credit balance is too low"})
    result = asyncio.run(module.run_coding_agent(_ctx(core), prompt="hi", path=str(tmp_path)))
    assert result.verification is not Verification.SUCCESS, result.output
    assert "credit balance" in (result.error or result.message)


def test_output_that_cannot_be_parsed_is_unverified_not_a_success(core, monkeypatch, tmp_path):
    """A process that exits 0 without an answer is not an answer."""
    from jarvis.tools import coding_agent as module

    monkeypatch.setattr(module, "agent_cli_path", lambda: "/usr/local/bin/claude")
    _fake_agent(monkeypatch, module, code=0, out="")
    result = asyncio.run(module.run_coding_agent(_ctx(core), prompt="hi", path=str(tmp_path)))
    assert result.verification is Verification.UNKNOWN, result.verification
    # `unverified()` means "the handler ran; whether the world changed is not
    # established" -- the message has to say that out loud rather than imply success.
    admitted = any(phrase in result.message.lower()
                   for phrase in ("without an answer", "cannot tell", "could not confirm"))
    assert admitted, result.message


def test_a_nonzero_exit_carries_the_agents_own_words(core, monkeypatch, tmp_path):
    from jarvis.tools import coding_agent as module

    monkeypatch.setattr(module, "agent_cli_path", lambda: "/usr/local/bin/claude")
    _fake_agent(monkeypatch, module, code=1, err="Not logged in · Please run /login")
    result = asyncio.run(module.run_coding_agent(_ctx(core), prompt="hi", path=str(tmp_path)))
    assert result.ok is False
    assert "Not logged in" in (result.error or result.message)


# ---------------------------------------------------------------------------
# 4. reaching it from a sentence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase,expected_prompt", [
    ("ask claude to fix the failing tests", "fix the failing tests"),
    ("in claude code run the test suite", "run the test suite"),
])
def test_a_sentence_that_asks_the_agent_reaches_the_tool(core, phrase, expected_prompt):
    plan = core.planner.parse(phrase)
    actions = [a for a in plan.actions if a.tool == "run_coding_agent"]
    assert actions, [(a.tool, a.params) for a in plan.actions]
    assert actions[0].params["prompt"] == expected_prompt, actions[0].params


def test_continue_in_claude_code_asks_to_continue_the_session(core):
    plan = core.planner.parse("continue in claude code")
    actions = [a for a in plan.actions if a.tool == "run_coding_agent"]
    assert actions, [(a.tool, a.params) for a in plan.actions]
    assert actions[0].params.get("resume") is True, actions[0].params


def test_the_confirmation_names_the_instruction_not_the_parameter_dump(core, tmp_path):
    """32-B, one tool at a time: a phone showing `prompt=... | path=...` asks the user
    to parse a dict before deciding."""
    spec = core.tools.get("run_coding_agent")
    preview = core.policy._preview(spec, PlannedAction(
        tool="run_coding_agent",
        params={"prompt": "add a README to the project", "path": str(tmp_path)},
        device="laptop_main"))
    assert "add a README to the project" in preview, preview
    assert "prompt=" not in preview, preview
    assert tmp_path.name in preview, preview
