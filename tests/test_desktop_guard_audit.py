"""The desktop guard audit: a declared parameter is a promise the tool must keep.

The path a desktop actuation takes is

    ToolSpec.parameters → ToolRegistry.validate_params → PermissionEngine
        → jarvis/desktop/executor.py → native/desktop_executor/main.swift → input

`PermissionEngine.evaluate` calls `ToolRegistry.validate_params` first and BLOCKs
on any problem. So a guard a handler accepts but its `ToolSpec` omits is not
merely undocumented — the step is refused before it ever reaches the executor,
and the guard protects nothing. That has silently blocked guards twice in this
project (see the comments in `tests/test_desktop_native.py`); this module asserts
the property for *every* registered `desktop_*` tool by enumerating the real
registry rather than a hand-maintained list, so a tool added later is audited
whether or not anyone remembers to add it here.

Three properties, each measured through the registry rather than by calling a
handler directly (calling the handler directly is exactly what hid the original
defects):

1. Every parameter a handler accepts explicitly is declared in its spec.
2. Every guard a guarded tool forwards reaches the executor stub unchanged.
3. A tool with no guard cannot be handed one silently: the spec declares it, or
   the registry refuses the call.

No real desktop input is sent; `jarvis.tools.desktop_native.executor` and the
legacy AppleScript seam are replaced with stubs.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from jarvis.config import get_settings
from jarvis.core.models import PlannedAction, PolicyDecision
from jarvis.registry.devices import DeviceRegistry
from jarvis.security.policy import PermissionEngine
from jarvis.tools import build_default_registry
from jarvis.tools import desktop as legacy_desktop
from jarvis.tools import desktop_native
from jarvis.tools.base import ToolContext

#: The parameters the Swift executor reads as a target-staleness or focus guard.
#: `--expect-bounds` is wired end-to-end only for `press_element` today; the
#: others name it because `desktop/executor.py` can pass it, and a tool that
#: grows it must be caught here.
GUARD_PARAMETERS = ("expect_window_id", "expect_pid", "expect_app", "expect_bounds")

#: The two modules that own desktop tools. `desktop_native` names them
#: `desktop_*`; `desktop` is the legacy AppleScript path and spells its three
#: differently, so it is audited by name rather than by prefix.
DESKTOP_MODULES = (legacy_desktop, desktop_native)


@pytest.fixture()
def ctx():
    return ToolContext(settings=get_settings(), device_id="laptop")


@pytest.fixture()
def registry():
    return build_default_registry()


def _desktop_tools(registry):
    return [n for n in registry.names() if n.startswith("desktop_")]


def _spec_desktop_tools():
    """`(spec, handler)` pairs from the source of truth, not the built registry.

    Used to prove the registry enumeration and the module `SPECS` agree, so a
    tool that fails to register cannot hide from the audit below.
    """
    return [(spec, handler) for module in DESKTOP_MODULES for spec, handler in module.SPECS]


def _explicit_parameters(handler) -> list[str]:
    """Parameter names a handler declares explicitly, excluding ctx and **kwargs."""
    signature = inspect.signature(handler)
    return [p.name for p in signature.parameters.values()
            if p.name not in {"ctx", "self"}
            and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)]


def _accepts_kwargs(handler) -> bool:
    return any(p.kind == p.VAR_KEYWORD for p in inspect.signature(handler).parameters.values())


def _call(registry, ctx, name, **params):
    handler = registry.handler(name)
    assert handler is not None, f"{name} is advertised but has no handler"
    return asyncio.run(handler(ctx, **params))


def _engine(registry):
    return PermissionEngine(registry, DeviceRegistry())


@pytest.fixture()
def stub(monkeypatch):
    """Replace the executor with a programmable stub, so no real input is sent.

    Matches the style of `tests/test_desktop_native.py`: a call log plus a
    factory that installs one method returning a canned result.
    """
    calls: list = []

    def make(result):
        def _stub(*args, **kwargs):
            calls.append((args, kwargs))
            return dict(result)
        return _stub

    return calls, make, monkeypatch


# -- the invariant ------------------------------------------------------------
#
# The registry is the list; nothing here is hand-written, so a tool added later
# is audited without anyone remembering to add it. A tool that has no handler,
# or whose handler takes a parameter the spec does not declare, is the exact
# shape of the defect that blocked `expect_window_id` on three click variants.

def test_the_registry_is_the_only_list_and_it_is_not_empty(registry):
    """Pin the enumeration, so a silent drop is not read as "nothing to audit"."""
    names = _desktop_tools(registry)
    assert len(names) >= 19, f"the desktop set shrank to {len(names)}: {names}"
    # The `desktop_`-prefixed specs live in `desktop_native`; the legacy module
    # spells its three differently and is audited separately below. Every
    # `desktop_` spec must be registered, and every registered one must exist.
    spec_names = {spec.name for spec, _ in _spec_desktop_tools()
                  if spec.name.startswith("desktop_")}
    assert spec_names <= set(names), (
        f"a desktop tool has a spec but never reached the registry: "
        f"{sorted(spec_names - set(names))}")
    assert set(names) == spec_names, (
        "the registry and the module SPECS disagree about which desktop tools "
        f"exist: registry-only={sorted(set(names) - spec_names)}, "
        f"spec-only={sorted(spec_names - set(names))}")


def test_every_desktop_tool_declares_every_parameter_its_handler_accepts(registry):
    """The property this module exists for.

    `ToolRegistry.validate_params` refuses any parameter not in `spec.parameters`
    when the spec declares any at all, and `PermissionEngine.evaluate` turns
    that refusal into BLOCK. So an undeclared explicit parameter is a capability
    the tool advertises in its code and the engine refuses in practice.
    """
    undeclared_by_tool = {}
    for name in _desktop_tools(registry):
        spec = registry.get(name)
        handler = registry.handler(name)
        assert spec is not None and handler is not None, name
        explicit = _explicit_parameters(handler)
        undeclared = sorted(p for p in explicit if p not in spec.parameters)
        if undeclared:
            undeclared_by_tool[name] = undeclared
    assert not undeclared_by_tool, (
        "these tools accept parameters their ToolSpec omits; the permission "
        f"engine would BLOCK every such call: {undeclared_by_tool}")


def test_every_desktop_tool_that_does_not_declare_parameters_also_accepts_none(registry):
    """The `validate_params` short circuit, pinned deliberately.

    `validate_params` only rejects an unexpected parameter when `spec.parameters`
    is non-empty. A tool with `parameters={}` therefore accepts ANY keyword and
    drops it on the floor via `**kwargs` — which is fine only while the handler
    has nothing explicit for a caller to aim through. This asserts exactly that:
    empty spec ⇒ no explicit parameters.
    """
    for name in _desktop_tools(registry):
        spec = registry.get(name)
        handler = registry.handler(name)
        explicit = _explicit_parameters(handler)
        if not spec.parameters:
            assert not explicit, (
                f"{name} declares no parameters, so the permission engine will "
                f"pass any keyword through to a handler that takes "
                f"{explicit} explicitly — the guard would be silently accepted "
                "and used outside the declared contract")


def test_guards_are_declared_by_exactly_the_handlers_that_forward_them(registry):
    """No guard travels under **kwargs, and no guard is declared and ignored.

    `**kwargs` is how a guard disappeared twice: the handler accepted the name,
    dropped it, and the spec never had it, so nobody noticed until a replay
    acted on the wrong window. This closes both directions: a declared guard
    must be an explicit parameter (so it cannot be swallowed), and every tool
    that declares one must appear in `GUARD_CASES` (so its forwarding is
    actually exercised rather than assumed).
    """
    guarded_tools = set()
    for name in _desktop_tools(registry):
        spec = registry.get(name)
        handler = registry.handler(name)
        explicit = set(_explicit_parameters(handler))
        declared = set(spec.parameters)
        forwarded = declared & set(GUARD_PARAMETERS)
        if forwarded:
            guarded_tools.add(name)
        for guard in forwarded:
            assert guard in explicit, (
                f"{name} declares {guard} but its handler does not take it "
                "explicitly — it can only be reaching **kwargs, which is how "
                "a guard is accepted and then dropped")

    # A tool can declare a guard and still drop it. The only way to know it does
    # not is to call it, so every declaring tool must be in the exercised set.
    exercised = {tool for tool, _executor, _params, _guards in GUARD_CASES}
    assert guarded_tools == exercised, (
        "a tool declares a guard but its forwarding is untested: "
        f"{sorted(guarded_tools - exercised)}; or GUARD_CASES names an "
        f"unguarded tool: {sorted(exercised - guarded_tools)}")


# -- property 2: the guard reaches the executor ---------------------------------
#
# Every case below passes the guard through `registry.handler()`, not by calling
# the module function, because `validate_params` is what the permission engine
# uses and calling the handler directly bypasses it.

#: Every guarded tool, the executor method it calls, the minimum parameters that
#: get it past its own validation, and the guards it is expected to forward.
GUARD_CASES = [
    ("desktop_click", "click", {"x": 10, "y": 20},
     {"expect_window_id": 4242, "expect_pid": 999}),
    ("desktop_double_click", "click", {"x": 10, "y": 20},
     {"expect_window_id": 4242, "expect_pid": 999}),
    ("desktop_right_click", "click", {"x": 10, "y": 20},
     {"expect_window_id": 4242, "expect_pid": 999}),
    ("desktop_drag", "drag",
     {"from_x": 10, "from_y": 20, "to_x": 30, "to_y": 40},
     {"expect_window_id": 4242, "expect_pid": 999}),
    ("desktop_scroll", "scroll", {"x": 10, "y": 20, "lines": -3},
     {"expect_window_id": 4242, "expect_pid": 999}),
    ("desktop_press_element", "press_element", {"keyword": "Save"},
     {"expect_window_id": 4242, "expect_pid": 999}),
    ("desktop_type_text", "type_text", {"text": "hello"},
     {"expect_app": "TextEdit"}),
    ("desktop_press_key", "press_key", {"key": "s", "modifiers": ["cmd"]},
     {"expect_app": "TextEdit"}),
    ("desktop_shortcut", "press_key", {"key": "s", "modifiers": ["cmd"]},
     {"expect_app": "TextEdit"}),
]


@pytest.mark.parametrize("tool,executor_name,params,guards", GUARD_CASES)
def test_a_guarded_tool_reaches_the_executor_with_its_guard(
        stub, registry, ctx, tool, executor_name, params, guards):
    """The guard the caller sends is the guard the executor receives."""
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, executor_name,
               make({"status": "performed_unverified", "reason": "posted"}))
    result = _call(registry, ctx, tool, **params, **guards)
    assert result.ok is True, result.message
    assert calls, f"{tool} never reached {executor_name}"
    _args, sent = calls[0]
    for guard, value in guards.items():
        assert sent.get(guard) == value, (
            f"{tool} dropped {guard}={value!r}; the executor received "
            f"{sent.get(guard)!r} and would not have checked the target")


@pytest.mark.parametrize("tool,executor_name,params,guards", GUARD_CASES)
def test_a_guarded_tool_without_a_guard_passes_none(
        stub, registry, ctx, tool, executor_name, params, guards):
    """The guard is opt-in: a fresh, just-resolved target is unrestricted.

    Whatever the executor's default is for an absent guard it must be the
    default the caller gets when they name nothing — never a stale value from an
    earlier call, and never a truthy stand-in for "no expectation".
    """
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, executor_name,
               make({"status": "performed_unverified", "reason": "posted"}))
    result = _call(registry, ctx, tool, **params)
    assert result.ok is True, result.message
    _args, sent = calls[0]
    for guard in guards:
        assert not sent.get(guard), (
            f"{tool} invented {guard}={sent.get(guard)!r} when the caller named none")


def test_the_permission_engine_admits_every_guard_the_tools_forward(registry):
    """The end the invariant exists for: `evaluate` must not BLOCK on a guard.

    `validate_params` is the first gate `PermissionEngine.evaluate` applies, so a
    guard it refuses is a step that never runs. This asserts the engine's own
    verdict is not a parameter BLOCK — it may still CONFIRM (a click always
    asks), but the reason must not be "unexpected parameter".
    """
    engine = _engine(registry)
    for tool, _executor, params, guards in GUARD_CASES:
        action = PlannedAction(tool=tool, params={**params, **guards},
                               device="laptop_main")
        verdict = engine.evaluate(action)
        assert "unexpected parameter" not in verdict.reason.lower(), (
            f"{tool} with {sorted(guards)} is BLOCKed by the engine: {verdict.reason}")
        assert verdict.decision in (PolicyDecision.EXECUTE, PolicyDecision.CONFIRM), (
            f"{tool} with {sorted(guards)} was {verdict.decision.value}: {verdict.reason}")


def test_the_permission_engine_still_refuses_an_undeclared_parameter(registry):
    """The invariant must not be satisfied by making `validate_params` permissive.

    A tool with a real parameter set (`desktop_click`) must still refuse a name
    it does not declare, or the whole audit would pass for the wrong reason.
    """
    engine = _engine(registry)
    action = PlannedAction(tool="desktop_click",
                           params={"x": 1, "y": 2, "expect_the_window_i_meant": 7},
                           device="laptop_main")
    verdict = engine.evaluate(action)
    assert verdict.decision is PolicyDecision.BLOCK, verdict.decision
    assert "expect_the_window_i_meant" in verdict.reason


# -- property 3: a tool with no guard cannot be handed one silently ------------

#: A tool with no guard in its spec. Passing a guard name must fail, not pass
#: silently into **kwargs — otherwise a caller can believe a guard is in force
#: when the executor never saw it.
UNGUARDED_TOOLS = [
    ("desktop_move_pointer", {"x": 10, "y": 20}),
    ("desktop_focus_window", {"application": "TextEdit"}),
    ("desktop_observe", {"keyword": "Save"}),
    ("desktop_windows", {}),
    ("desktop_inspect_ui", {"application": "TextEdit"}),
]


@pytest.mark.parametrize("tool,baseline", UNGUARDED_TOOLS)
@pytest.mark.parametrize("guard", GUARD_PARAMETERS)
def test_an_unguarded_tool_cannot_be_handed_a_guard_silently(
        registry, tool, baseline, guard):
    """`validate_params` refuses the guard for these tools — measured, not assumed.

    If this ever stops refusing, it will be because the spec grew the parameter
    (in which case the tool must actually forward it and belongs in
    `GUARD_CASES`) or because `validate_params` was loosened, which would be a
    much larger change than a test.

    `desktop_windows` declares exactly one parameter, so the "spec is empty"
    short circuit in `validate_params` does not apply to it — both shapes are
    exercised here.
    """
    assert guard not in registry.get(tool).parameters, (
        f"{tool} now declares {guard}; move it to GUARD_CASES so the forwarding "
        "is asserted, rather than leaving it declared and unchecked")
    problems = registry.validate_params(tool, {**baseline, guard: 1})
    assert any(guard in problem for problem in problems), (
        f"{tool} accepts {guard} without declaring it; the handler would take it "
        f"through **kwargs and the executor would never see it (problems={problems})")


def test_no_desktop_handler_takes_kwargs_and_uses_a_guard_from_them(registry):
    """The structural form of the same defect, checked at the source.

    A handler whose only route to a guard is `**_` cannot forward it: the
    parameter is swallowed. This fails if anyone adds a guard read from kwargs
    instead of an explicit parameter, even before a spec is updated to match.
    """
    offenders = []
    for name in _desktop_tools(registry):
        handler = registry.handler(name)
        explicit = set(_explicit_parameters(handler))
        source = inspect.getsource(handler)
        for guard in GUARD_PARAMETERS:
            if guard in explicit:
                continue
            # A bare reference would mean the guard is being read out of **_.
            if f"kwargs.get({guard!r}" in source or f'kwargs["{guard}"]' in source:
                offenders.append((name, guard))
    assert not offenders, (
        f"these handlers read a guard out of **kwargs, where it is accepted "
        f"silently and forwarded or dropped without a declaration: {offenders}")


# -- the executor's dispatch, pinned so the audit cannot drift ----------------
#
# `native/desktop_executor/main.swift` strips `--expect-window-id`,
# `--expect-pid` and `--expect-bounds` from argv before it dispatches, so EVERY
# operation receives them silently. Only the operations that pass `expectation:`
# into their implementation actually check them. This pins that set: if an
# operation is added to (or removed from) the guarded side, this test fails and
# the Python side must be updated to match, rather than the gap being found by a
# replay that acted on the wrong window.

SWIFT_EXECUTOR = (Path(__file__).resolve().parent.parent
                  / "native" / "desktop_executor" / "main.swift")

#: The operations that call `requireFreshTarget` today. The other operations
#: either act on no window (`move`, `release_all`, `permissions`, ...) or are
#: covered by a different guard (`type`/`key` use `expect_app`, a key=value
#: parameter, not a `--` flag).
SWIFT_WINDOW_GUARDED_OPS = {"click", "drag", "scroll", "press_element"}


def _swift_dispatch_cases(source: str) -> dict[str, str]:
    """`{op: block}` for every top-level `case "op":` in the executor's switch.

    A block runs from its `case` line to the next top-level `case`/`default`.
    The anchor is column zero on purpose: `opKey` has an indented inner `switch
    m` whose cases must not be mistaken for operations.
    """
    import re

    starts = list(re.finditer(r'^case "([^"]+)":', source, re.MULTILINE))
    end_of_switch = re.search(r"^default:", source, re.MULTILINE)
    stops = [m.start() for m in starts]
    if end_of_switch is not None:
        stops.append(end_of_switch.start())
    blocks: dict[str, str] = {}
    for match in starts:
        following = [s for s in stops if s > match.start()]
        end = min(following) if following else len(source)
        blocks[match.group(1)] = source[match.start():end]
    return blocks


def test_the_native_executor_guards_exactly_the_operations_this_file_assumes():
    source = SWIFT_EXECUTOR.read_text()
    blocks = _swift_dispatch_cases(source)
    assert blocks, "the Swift dispatch could not be parsed; the audit below is blind"
    guarded = {op for op, block in blocks.items() if "expectation: expectation" in block}
    assert guarded == SWIFT_WINDOW_GUARDED_OPS, (
        "the set of operations that consume the --expect-* target guard changed: "
        f"now {sorted(guarded)}, audited as {sorted(SWIFT_WINDOW_GUARDED_OPS)}. "
        "Every operation that does not consume it receives the flags silently — "
        "update this audit and the Python spec/tool forwarding to match.")


def test_every_swift_operation_is_accounted_for_in_the_audit():
    """No operation may appear in the dispatch without a decision recorded here.

    This is a ledger, not a wish: an operation that acts on a target and has no
    guard belongs in `UNGUARDED_BY_DESIGN` with the reason it is safe, so the
    next reader does not have to rediscover it from a blank line.
    """
    source = SWIFT_EXECUTOR.read_text()
    blocks = set(_swift_dispatch_cases(source))
    audited = (SWIFT_WINDOW_GUARDED_OPS | SWIFT_GUARDED_BY_DESIGN
               | SWIFT_READ_ONLY | SWIFT_TARGETED_WITHOUT_GUARD)
    assert blocks <= audited, (
        f"these Swift operations have no entry in this audit: {sorted(blocks - audited)}")
    assert audited <= blocks, (
        f"this audit names operations the executor no longer has: {sorted(audited - blocks)}")


#: Acting operations with no window guard, and why that is acceptable. Each one
#: either has no target to compare or is refused before acting by a different
#: check. Anything added here must carry its reason.
SWIFT_GUARDED_BY_DESIGN = {
    "move",          # relocates the pointer; there is no window to be stale.
    "type",          # guarded by `expect_app` (focus identity), not by window id.
    "key",           # same.
    "focus",         # activates by application name and reports the frontmost
                     # application it observed; there is no post-action target.
    "release_all",   # recovery: posts button/modifier ups at the current pointer.
}

#: Operations that do not act on the person's session at all.
SWIFT_READ_ONLY = {
    "permissions", "self_check", "capabilities", "identity", "windows",
    "inspect", "read_element", "verify_change",
    # Shows macOS's own permission dialog. It grants nothing and posts nothing;
    # the person still has to flip the switch.
    "request_access",
}

#: Targeted operations that act with NO staleness guard. This is a residual, not
#: a design choice: `scroll_element` sets a scroll bar's AXValue in whichever
#: application it is told, and the executor's `--expect-*` flags are parsed and
#: then silently ignored for it because the dispatch never passes them in. It is
#: recorded here — and pinned by the test below — rather than omitted, so the
#: gap is a known, checked fact and not a blank line in a table.
SWIFT_TARGETED_WITHOUT_GUARD = {"scroll_element"}


def test_scroll_element_is_targeted_without_a_guard_and_is_not_exposed():
    """The one targeted Swift operation with no guard, and its current blast radius.

    `scroll_element` exists in the executor and can be invoked by anything able
    to exec the binary. It consumes no `--expect-*` flag, so a caller that
    passes one is silently unprotected. What limits it today is reachability:
    no Python module calls it — not `jarvis/desktop/executor.py`, not the tool
    layer, not the routes — so it is not on the governed path.

    If this test fails because `scroll_element` gained a helper, that is the
    moment to add `expectation:` in its Swift block and a guard to the Python
    helper; it must not be exposed unguarded.
    """
    source = SWIFT_EXECUTOR.read_text()
    blocks = _swift_dispatch_cases(source)
    assert "scroll_element" in blocks, "the operation was removed; drop it from the ledger"
    assert "expectation: expectation" not in blocks["scroll_element"], (
        "scroll_element now consumes the target guard — move it into "
        "SWIFT_WINDOW_GUARDED_OPS and assert its forwarding from Python")

    root = Path(__file__).resolve().parent.parent
    callers = []
    for path in root.joinpath("jarvis").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if "scroll_element" in path.read_text(errors="ignore"):
            callers.append(str(path.relative_to(root)))
    assert not callers, (
        "scroll_element is now reachable from the Python layer, but it has no "
        "target guard — guard it before exposing it: " + ", ".join(sorted(callers)))


def test_the_read_only_operations_are_the_ones_that_only_read():
    """Keep the "read-only" classification honest by inspecting, not by label.

    These blocks must not post an event. `verify_change` is included because a
    false `verified` is a claim error, not an action error: it can re-read the
    wrong element after a window moved, and the consequence is a wrong success
    sentence rather than a wrong click. That is why it is classified here and
    reported as a residual, not silently treated as guarded.
    """
    source = SWIFT_EXECUTOR.read_text()
    blocks = _swift_dispatch_cases(source)
    for op in SWIFT_READ_ONLY:
        block = blocks[op]
        assert ".post(tap:" not in block, (
            f"{op} is classified read-only but posts an event")
        assert "CGWarpMouseCursorPosition" not in block, (
            f"{op} is classified read-only but moves the pointer")


# -- the legacy AppleScript surface -------------------------------------------
#
# `click_desktop` and `type_desktop_text` are the older, still-registered path.
# They have no guard because their mechanism (`System Events`) has none to
# offer — which is one reason the native executor exists. They are audited here
# so the invariant covers them and the fact is recorded rather than assumed.

def test_the_legacy_desktop_tools_declare_everything_and_guard_nothing(registry):
    for name in ("click_desktop", "type_desktop_text", "get_desktop_control_state"):
        spec = registry.get(name)
        handler = registry.handler(name)
        assert spec is not None and handler is not None, name
        explicit = set(_explicit_parameters(handler))
        assert explicit <= set(spec.parameters), (
            f"{name} accepts {sorted(explicit - set(spec.parameters))} without declaring it")
        for guard in GUARD_PARAMETERS:
            assert guard not in spec.parameters, (
                f"{name} is the AppleScript path and cannot honour {guard}")

    # The two that name a target at all must refuse a guard they cannot honour,
    # so a caller cannot believe one is in force.
    for name, baseline in (("click_desktop", {"x": 1, "y": 2}),
                           ("type_desktop_text", {"text": "x"})):
        problems = registry.validate_params(name, {**baseline, "expect_app": "TextEdit"})
        assert any("expect_app" in problem for problem in problems), (
            f"{name} silently accepted expect_app (problems={problems})")


def test_an_empty_spec_tool_accepts_anything_and_that_is_safe_only_where_it_is_unaimed(
        registry):
    """The `validate_params` short circuit, measured and bounded.

    `validate_params` only rejects an unexpected parameter while
    `spec.parameters` is non-empty. The empty-spec tools therefore accept any
    keyword — `get_desktop_control_state` takes `expect_app="TextEdit"` and
    reports no problem. That is safe today because each such tool takes no
    explicit parameter at all: there is no target to guard, so there is no
    guard a caller could reasonably pass and have dropped. This asserts both
    halves, and fails if a tool with an aimable surface is ever registered with
    an empty parameter set.
    """
    empties = [n for n in _desktop_tools(registry) if not registry.get(n).parameters]
    assert empties, "expected at least the read-only posture tools to declare no parameters"
    for name in empties:
        handler = registry.handler(name)
        explicit = _explicit_parameters(handler)
        assert not explicit, (
            f"{name} has an empty spec (so every keyword passes validation) and "
            f"takes {explicit} explicitly — an aimable tool with no declared "
            "parameters is the defect this audit exists to catch")
        # The silent acceptance is real, not hypothetical. Recording it stops a
        # future change from being made on the belief that it is refused.
        assert registry.validate_params(name, {"expect_app": "TextEdit"}) == [], name


def test_the_legacy_click_reaches_its_osascript_seam_once(monkeypatch, ctx):
    """A behavioural anchor: the legacy path is exercised through the seam only.

    `_osascript` is the single place a script leaves Python, and the tests
    replace it so no real click is posted. This proves the tool still routes
    through it (and therefore that the audit above is looking at a live tool,
    not a dead registration).
    """
    calls: list = []

    def fake_osascript(script, args, timeout=8.0):
        calls.append((script, tuple(args)))
        return 0, "Finder"

    monkeypatch.setattr(legacy_desktop, "_osascript", fake_osascript)
    monkeypatch.setattr(legacy_desktop, "_accessibility",
                        lambda _ctx: {"status": "GRANTED"})

    async def run():
        handler = legacy_desktop.click_desktop
        return await handler(ctx, x=10, y=20)

    result = asyncio.run(run())
    assert result.ok is True, result.error
    click_calls = [c for c in calls if "click at" in c[0]]
    assert len(click_calls) == 1, calls
    assert click_calls[0][1] == ("10", "20")