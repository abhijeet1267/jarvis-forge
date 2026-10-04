"""Phase 49a — the spec's names, without losing the state the old ones carry.

The project spec (``VANTAGE_JARVIS_PROJECT_SPEC.md`` §39) asks for tools named
``open_application``, ``get_battery_status``, ``get_machine_snapshot`` and
``get_running_applications``. The registry has all four capabilities under other names, so
this is a rename -- and in this codebase a name is not only a label:

    permissions.tool   a standing revocation, looked up with ``WHERE tool = ?``
    confirmations.tool a pending approval, matched when the user answers
    action_logs.tool   the history a person reads back
    task_steps.name    a step of a job that may not have run yet

Renaming by substitution would therefore **silently delete a revocation**: a user who
blocked ``open_app`` would find ``open_application`` allowed, with no error anywhere. That
is a privilege escalation dressed as a refactor, and it is the one thing this project's own
rules say a change may never do.

So the rename is an alias. The spec name becomes canonical, the old name keeps resolving,
and every stored-name lookup widens to both. The tests below are mostly about that
widening, because the capability already works and the risk is entirely in the state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DASH = REPO / "dashboard"

RENAMED = {
    "open_application": "open_app",
    "get_battery_status": "battery_status",
    "get_machine_snapshot": "laptop_snapshot",
    "get_running_applications": "list_apps",
}


@pytest.fixture(scope="module")
def registry():
    from jarvis.tools import build_default_registry

    return build_default_registry()


@pytest.fixture(scope="module")
def planner(registry):
    from jarvis.core.planner import RulePlanner

    return RulePlanner(registry)


# ---------------------------------------------------------------------------
# 1. the spec's names are the canonical ones
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("canonical", sorted(RENAMED))
def test_the_spec_name_is_a_registered_tool(registry, canonical):
    spec = registry.get(canonical)
    assert spec is not None, f"the registry has no {canonical!r}; the spec names it directly"
    assert spec.name == canonical


@pytest.mark.parametrize("canonical,old", sorted(RENAMED.items()))
def test_the_old_name_still_resolves_to_it(registry, canonical, old):
    """Callers written before the rename keep working -- including a scheduled job whose
    stored step still names the old tool."""
    assert registry.has(old), f"{old!r} no longer resolves; stored references would break"
    assert registry.get(old).name == canonical
    assert registry.handler(old) is registry.handler(canonical)
    assert registry.validate_params(old, {}) == registry.validate_params(canonical, {})


def test_an_alias_is_not_a_second_capability(registry):
    """An alias must not add a row: counting one capability twice would double the published
    surface and let the UI offer the same tool under two names.

    The guard is the invariant, not a frozen total. The registry grows by real capabilities --
    Phase 52 added ``open_user_folder`` and ``get_frontmost_application`` -- and a hard-coded
    count would have made this test refuse legitimate work instead of catching the defect it
    is named for.
    """
    specs = registry.all()
    assert len(specs) == len({s.name for s in specs}), "one name is registered twice"
    listed = registry.names()
    assert len(listed) == len(specs), "a registered capability is not listed"
    for old in RENAMED.values():
        assert old not in listed, f"{old!r} is listed as its own tool"


def test_every_alias_belongs_to_exactly_one_tool(registry):
    """An alias that collides with another tool's real name would make one of the two
    unreachable, and which one loses depends on registration order."""
    canonical = set(registry.names())
    seen: dict[str, str] = {}
    for spec in registry.all():
        for alias in spec.aliases:
            assert alias not in canonical, f"{alias!r} is both an alias and a real tool"
            assert alias not in seen, f"{alias!r} claimed by {seen[alias]!r} and {spec.name!r}"
            seen[alias] = spec.name


def test_the_aliases_are_visible_to_a_client(registry):
    """A reader of /tools must be able to tell that `open_app` in an old audit row is the
    same capability as `open_application` today; without the field the two look unrelated.

    Checked on the payload rather than on the registry object, because the route builds
    its own dict and was the thing that dropped the field.
    """
    import importlib

    from fastapi.testclient import TestClient

    from jarvis.config import get_settings

    app_module = importlib.import_module("jarvis.api.app")
    # The loopback origin matters: the host gate refuses a request addressed at
    # "testserver", so a client built without a base_url reads a refusal, not a payload.
    with TestClient(app_module.app, base_url="http://127.0.0.1:8765",
                    headers={"X-JARVIS-Token": get_settings().api_token}) as client:
        body = client.get("/tools").json()
    assert isinstance(body, list), f"/tools did not answer with a list: {body}"
    rows = body
    by_name = {r["name"]: r for r in rows}
    assert "aliases" in by_name["open_application"], sorted(by_name["open_application"])
    assert by_name["open_application"]["aliases"] == ["open_app"]
    assert len(rows) == len(registry.names()), (
        f"/tools published {len(rows)} rows against {len(registry.names())} registered tools")
    published = {r["name"] for r in rows}
    assert published == set(registry.names()), "the published list and the registry diverged"
    for old in RENAMED.values():
        assert old not in published, f"the alias {old!r} was published as its own capability"


# ---------------------------------------------------------------------------
# 2. the state: a stored permission must bind to both spellings
# ---------------------------------------------------------------------------


def test_a_revocation_written_under_the_old_name_blocks_the_new_name(core):
    """The escalation this phase exists to prevent.

    `PolicyStore._latest_permission` selects with ``WHERE tool = ?``. Substitute the name
    and the row is simply not found, so a blocked tool comes back with no signal at all.
    """
    from jarvis.core.models import PlannedAction

    core.policy.revoke("open_app")
    result = core.policy.evaluate(PlannedAction(step_id=1, tool="open_application",
                                                params={"app": "Safari"}, device="laptop_main"))
    assert result.decision.value == "BLOCK", (
        f"revoking open_app did not block open_application: {result.reason}")


def test_a_revocation_written_under_the_new_name_blocks_the_old_name(core):
    """Symmetry: history and any caller still using the old name must agree."""
    from jarvis.core.models import PlannedAction

    core.policy.revoke("open_application")
    result = core.policy.evaluate(PlannedAction(step_id=1, tool="open_app",
                                                params={"app": "Safari"}, device="laptop_main"))
    assert result.decision.value == "BLOCK", (
        f"revoking open_application did not block open_app: {result.reason}")


def test_a_revocation_does_not_spill_onto_an_unrelated_tool(core):
    """Widening the match must widen it over aliases only. `close_app` is a different
    capability that merely shares a word."""
    from jarvis.core.models import PlannedAction

    core.policy.revoke("open_application")
    result = core.policy.evaluate(PlannedAction(step_id=1, tool="close_app",
                                                params={"app": "Safari"}, device="laptop_main"))
    assert result.decision.value != "BLOCK" or "revoked" not in result.reason.lower(), (
        f"revoking open_application also blocked close_app: {result.reason}")


def test_a_grant_written_under_the_old_name_is_honoured_by_the_new(core):
    """The same row can say 'allowed' -- and a lost grant would look like a mysterious
    refusal, which is the failure mode the revocation test above exists to catch."""
    from jarvis.core.models import PlannedAction

    core.policy.grant("get_battery_status", subject="user")
    result = core.policy.evaluate(PlannedAction(step_id=1, tool="battery_status",
                                                device="laptop_main"))
    assert result.decision.value != "BLOCK" or "revoked" not in result.reason.lower(), result.reason


# ---------------------------------------------------------------------------
# 3. the sentences must reach the canonical names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("utterance,tool", [
    ("open safari", "open_application"),
    ("what is my battery", "get_battery_status"),
    ("what apps are running", "get_running_applications"),
    # The spec's own §6.1 row: "Give me a system status" -> get_machine_snapshot. The
    # repo also has a narrower `system_status`, so this asserts the *laptop-condition*
    # phrasings reach the snapshot at all -- measured before this phase, none of them
    # did, which made the renamed tool unreachable from any sentence.
    ("how is my mac", "get_machine_snapshot"),
    ("machine status", "get_machine_snapshot"),
    ("computer status", "get_machine_snapshot"),
])
def test_the_planner_routes_to_the_spec_name(planner, utterance, tool):
    plan = planner.parse(utterance)
    got = [a.tool for a in plan.actions]
    assert tool in got, f"{utterance!r} routed to {got}, not {tool}"


def test_a_revocation_written_without_a_device_blocks_a_device_action(core):
    """Found by the rename tests, but nothing to do with names.

    `PolicyStore.revoke(tool)` with no device writes a row whose ``device`` is NULL, and
    the lookup required that column to equal the action's device -- so the device-less
    form, which is the natural one to write and the only one a caller without a device in
    hand can write, revoked nothing for anything. Measured before the fix:

        revoke("open_app")            -> row written, device NULL
        evaluate(open_application @ laptop_main) -> EXECUTE, "low-risk action"

    No product surface reaches this method yet (measured: no route, tool or dashboard
    control calls PolicyStore.revoke; only device revocation is wired), so this is a trap
    being closed ahead of its first caller rather than a live fault. The grant direction
    is deliberately left inert -- see test_phase10_revocation.
    """
    from jarvis.core.models import PlannedAction

    core.policy.revoke("get_running_applications")
    result = core.policy.evaluate(PlannedAction(step_id=1, tool="get_running_applications",
                                                device="laptop_main"))
    assert result.decision.value == "BLOCK", (
        f"a global revocation blocked nothing: {result.decision.value} / {result.reason}")
    assert "revoked" in result.reason.lower(), result.reason


def test_a_device_revocation_is_not_overridden_by_another_device(core):
    """Widening the match must not make a revocation for one device bind another."""
    from jarvis.core.models import PlannedAction

    # `core` is session-scoped, so this uses a tool no other test in this file touches:
    # revoking one of the others globally would legitimately block the laptop here, and
    # the test would be reading its own neighbours instead of the rule.
    core.policy.revoke("close_app", "watch_main")
    result = core.policy.evaluate(PlannedAction(step_id=1, tool="close_app",
                                                params={"app": "Safari"},
                                                device="laptop_main"))
    assert "revoked" not in result.reason.lower(), (
        f"a watch revocation blocked the laptop: {result.reason}")


def test_the_audit_row_records_the_canonical_name(core):
    """A ledger that stores the alias a caller happened to use would split one capability
    across two names in the history a person reads back."""
    from jarvis.core.models import PlannedAction

    spec = core.tools.get("open_app")
    assert spec.name == "open_application", "the registry itself must report the canonical name"


def test_the_shipped_pages_name_tools_the_registry_has():
    """Phase 46's rule, re-checked after a rename: a quick action whose tool no longer
    exists is a button that does nothing, and the guard only works because it reads the
    registry rather than trusting the page."""
    import json
    import re

    source = (DASH / "phone.html").read_text() + (DASH / "index.html").read_text()
    block = re.search(r"const QUICK = \[(.*?)\n\];", source, re.S)
    assert block
    tools = {m.group(2) for m in re.finditer(
        r'\{ label: "([^"]+)", tool: "([^"]+)", say: "([^"]*)" \}', block.group(1))}
    from jarvis.tools import build_default_registry

    names = {s.name for s in build_default_registry().all()}
    missing = sorted(t for t in tools if t not in names)
    assert not missing, f"the pages offer tools the registry does not have: {missing}"
