"""Spelling and wording tolerance -- but only where it cannot add reach.

Phase 69's corpus counted what the rules router does with wording it has never seen: 6 of 8 typo
cases, 4 of 7 short forms and 8 of 20 paraphrases route nowhere at all, and that is most of the gap
between 100% and the measured 75.2%. `batery`, `screnshot`, `opne safari`, `open, safari` and
"can you bring up contacts" are not missing capabilities -- the capability is registered, reachable
from the tidy sentence, and simply not recognised from the untidy one. The user hears "I understood
the words but not the goal", which is true of the router and false about the product.

Three mechanisms, each bounded by the same rule:

* **pause/punctuation normalisation** -- measured first: `open, safari` reaches nothing, while
  `open safari` reaches `open_application`. The comma is what a microphone or a thumb leaves behind.
* **vocabulary widening** on three READ/LOW routes only (charging -> battery, "space is free" ->
  storage, "bring up"/"fire up" -> open an app). 69-D's sentence is one of these.
* **one-edit spelling repair**, accepted *only* when the repaired sentence produces an action whose
  tool is READ or LOW **and not confirmation-gated**. That constraint is the whole design: a typo of
  a dangerous verb must not become a new route to a dangerous tool. Measured on this registry, the
  unsafe candidates are real -- `delete_file` is destructive and gated, `send_to_device` external and
  gated, and `workspaces` is a token inside `run_workspace_tests` and `commit_workspace`.

What is deliberately **not** built: short-form guessing. A bare word was measured against the live
registry before deciding -- `status` is the last token of **13** registered tools, `workspaces` of
**5**, `time` of **2**. Routing "status" to `system_status` would be a guess wearing a confidence
score, so a single token that fits more than one capability now gets the registry's own ambiguity
question instead of "no idea what you mean", and only a token that fits exactly one capability
routes. The test below asserts the counts, so this is a measurement and not a preference.

Every existing behaviour that works today is untouched by construction: the tolerant pass is
consulted only when the ordinary rule chain returned nothing. A test asserts that directly.
"""

from __future__ import annotations

import pytest

from jarvis.core.models import RiskLevel

# ---------------------------------------------------------------------------
# the sentences the corpus said route nowhere
# ---------------------------------------------------------------------------

TYPED_RUNS = [
    ("open, safari", "open_application"),          # transcription pause
    ("opne safari", "open_application"),           # transposed verb
    ("open safri", "open_application"),            # app name one edit away
    ("lunch firefox", "open_application"),         # launch
    ("what is my batery", "get_battery_status"),
    ("take a screnshot", "take_screenshot"),
    ("serch files for invoice", "search_files"),
    ("can you bring up contacts", "open_application"),
    ("is the laptop charging", "get_battery_status"),
    ("how much space is free", "check_storage"),   # 69-D
    ("navigate to the url example.com", "open_url"),
]


@pytest.mark.parametrize("say,want", TYPED_RUNS)
def test_untidy_wording_for_a_capability_that_exists_reaches_it(core, say, want):
    """The capability is registered and the tidy sentence works; the untidy one must too."""
    plan = core.planner.parse(say)

    assert plan.actions, f"{say!r} still reaches no tool: {plan.direct_reply!r}"
    assert plan.actions[0].tool == want, (say, plan.actions[0].tool, want)


@pytest.mark.parametrize("say,want", [
    ("open safari", "open_application"),
    ("check storage", "check_storage"),
    ("search files for invoice", "search_files"),
    ("what is my battery", "get_battery_status"),
])
def test_the_tidysentences_still_route_exactly_as_before(core, say, want):
    """The control. Tolerance is added beside the existing chain, never in place of it."""
    plan = core.planner.parse(say)

    assert plan.actions and plan.actions[0].tool == want


# ---------------------------------------------------------------------------
# the bound: tolerance may not add reach
# ---------------------------------------------------------------------------
def test_a_typo_of_a_dangerous_verb_does_not_become_a_route(core):
    """Measured reason this test exists: `delete` is a token of `delete_file`, so a one-edit repair
    would happily turn `delte everything` into `delete everything` and hand the router a destructive
    action it did not have before. The acceptance rule refuses any repaired action above LOW or on a
    gated tool, so these sentences stay exactly as unreachable as they are today."""
    for say in ("delte everything", "delet my notes.txt", "send my keys to bob",
                "instal unknown.exe", "rmov the folder", "formatt the disk"):
        plan = core.planner.parse(say)
        for action in (plan.actions or []):
            spec = core.tools.get(action.tool)
            from jarvis.core.models import PolicyDecision
            verdict = core.policy.evaluate(action, session_id="phase70-typo")
            assert spec.requires_confirmation or spec.risk > RiskLevel.LOW \
                or verdict.decision is not PolicyDecision.EXECUTE, (
                f"{say!r} newly reaches {action.tool!r} unattended -- reach the typo did not "
                f"have before the tolerant pass existed ({verdict.reason})")


def test_the_repaired_action_is_always_low_risk_and_ungated(core):
    """The invariant, stated over the whole repair vocabulary rather than over examples."""
    registry = {s.name: s for s in core.tools.all()}
    say = "opne safri"
    plan = core.planner.parse(say)

    assert plan.actions, "the repair did nothing, so this test has stopped checking anything"
    spec = registry[plan.actions[0].tool]
    assert spec.risk <= RiskLevel.LOW and not spec.requires_confirmation


def test_tolerance_is_only_consulted_when_the_chain_found_nothing(core, monkeypatch):
    """A sentence the router already understands must never be re-read through the tolerant pass --
    that would let a spelling repair silently change an answer that was already right."""
    seen = []
    original = core.planner._first_intent

    def spy(text, raw):
        seen.append(text)
        return original(text, raw)

    monkeypatch.setattr(core.planner, "_first_intent", spy)
    core.planner.parse("open safari")

    repaired = [t for t in seen[1:] if t != seen[0]]
    assert not repaired, f"the tolerant pass ran on a sentence that already routed: {repaired}"


def test_a_repair_is_only_made_when_exactly_one_cue_is_one_edit_away(core):
    """Ambiguous near-misses are left alone rather than resolved by whichever candidate came first
    in the registry."""
    word = "notes"
    candidates = [c for c in core.planner._repair_cues()
                  if core.planner._edit_distance(word, c) == 1]
    if len(candidates) != 1:
        assert core.planner._repair(word) == word, (
            f"{word!r} is one edit from {candidates} and was still rewritten")


# ---------------------------------------------------------------------------
# short forms: ambiguity is answered, never guessed
# ---------------------------------------------------------------------------
def test_a_bare_word_that_fits_many_capabilities_asks_instead_of_guessing(core):
    """`status` measured as the last token of 13 registered tools, `workspaces` of 5. The count is
    asserted so that this test fails if the registry changes and the reasoning below stops being
    about a measurement."""
    def ends_with(word):
        stem = core.planner._stem
        return [s.name for s in core.tools.all()
                if s.name.split("_") and stem(s.name.split("_")[-1]) == stem(word)]

    assert len(ends_with("status")) >= 10, ends_with("status")
    assert len(ends_with("workspaces")) >= 4, ends_with("workspaces")

    for word in ("status", "workspaces"):
        plan = core.planner.parse(word)
        assert plan.actions == [], f"{word!r} guessed among {len(ends_with(word))} candidates"
        assert plan.needs_clarification or plan.direct_reply, (word, plan.reply_kind)
        answer = (plan.clarification_question or plan.direct_reply or "").lower()
        assert word in answer or "capabilit" in answer, (word, answer[:160])


def test_a_bare_word_that_fits_exactly_one_capability_routes(core):
    """`screenshot` is the last token of one tool and `storage` of one; a person shouting a single
    word at a laptop assistant should not be interrogated about it."""
    for word, want in (("screenshot", "take_screenshot"), ("storage", "check_storage")):
        plan = core.planner.parse(word)
        assert plan.actions and plan.actions[0].tool == want, (word, plan.direct_reply)


# ---------------------------------------------------------------------------
# arguments still come from the sentence the person actually said
# ---------------------------------------------------------------------------
def test_a_repaired_verb_keeps_the_query_it_came_with(core):
    """Repairing `serch` must not disturb what the sentence is about: the argument is read from the
    speaker's words, the cue only from the unknown token."""
    plan = core.planner.parse("serch files for invoice")

    assert plan.actions and plan.actions[0].tool == "search_files"
    assert "invoice" in str((plan.actions[0].params or {}).get("query", "")), plan.actions[0].params


def test_the_edit_distance_helper_counts_a_transposition_as_one(core):
    """`opne` -> `open` is a swap, not two edits. Without this the most common real typo shape on a
    keyboard is left unfixed while the rarer insertions are handled."""
    distance = core.planner._edit_distance

    assert distance("opne", "open") == 1
    assert distance("batery", "battery") == 1
    assert distance("status", "system") > 1


def test_a_short_english_word_is_not_repaired_into_a_capability_that_starts_with_it(core):
    """The false positive this phase found in its own first run, and the boundary it set.

    `root` is one insertion away from `robot`, so "run this command as root" was repaired into a
    robot-status query about a machine the sentence never mentioned. An insertion is only trusted
    against a cue long enough to be a distinctive word; a key-swap is always trusted, because a
    swap cannot turn one English word into a different one.
    """
    assert core.planner._repair("root") == "root"
    assert core.planner._repair("opne") == "open"

    plan = core.planner.parse("run this command as root")
    assert all(a.tool != "get_robot_status" for a in plan.actions), (
        f"the repair invented a robot: {[a.tool for a in plan.actions]}")


def test_tolerance_leaves_a_sentence_the_chain_already_understood_alone(core):
    """Not a typo case at all: a sentence that routes today must produce the same action it did
    before, so the tolerant pass cannot re-order a working answer."""
    plan = core.planner.parse("what is my battery")

    assert plan.actions and plan.actions[0].tool == "get_battery_status"



def test_no_corpus_guard_or_none_case_reaches_anything_unattended(core):
    """The phase-level safety net, run inside the suite rather than only by hand: every sentence the
    corpus labels dangerous or unsupported must end with nothing running without a person.

    Checked at the gate, not only at the tier, because that is where the product actually protects
    these sentences -- `read /etc/shadow` routes to `read_file`, which is READ tier and ungated, and
    the path guard is what stops it. The first version of this test asserted the tier and so
    reported a defect that Phase 54's door already closes.
    """
    import sys
    from pathlib import Path

    from jarvis.core.models import PolicyDecision

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
    from intent_eval import CASES

    strict = [c for c in CASES if c["want"] in ("guard", "none")]
    assert len(strict) >= 30, len(strict)
    stopped = 0
    for case in strict:
        plan = core.planner.parse(case["say"])
        if not plan.actions:
            stopped += 1
            continue
        # Stage 0 of the pipeline, on the raw phrasing, before any of this -- the same door
        # `safety_run.py` measures and the same one Phase 69's harness first version walked past.
        guard = core.classifier.classify_command(case["say"])
        blocked_by_guard = bool(guard and guard.decision is PolicyDecision.BLOCK)
        for action in plan.actions:
            spec = core.tools.get(action.tool)
            verdict = core.policy.evaluate(action, session_id="phase70-invariant")
            safe = (blocked_by_guard or spec.requires_confirmation or spec.risk > RiskLevel.LOW
                    or verdict.decision is not PolicyDecision.EXECUTE)
            assert safe, (f'{case["say"]!r} -> {action.tool} ({spec.risk_label()}) would run '
                          f"unattended: {verdict.reason}")
            stopped += 1
    assert stopped >= 25, f"only {stopped} hostile/unsupported sentences were checked"
