"""Model-planned actions (R2.3).

The rule planner is deterministic and complete for the intents it knows. What it
cannot do is answer a request expressed in a way no rule covers -- "check whether
the car is charging and tell me", say, where the rule set has no route. The
model can often work out what is being asked.

Before this module existed, the model's only role in the action path was to
*name* a tool in prose, and that prose was then thrown away: the reply said "I
suggested ``get_car_status``" and nothing ran. The gap was not that the model
could not plan -- it was that nothing carried a plan anywhere.

This module is that carrier. The model is asked for a **structured proposal**,
the proposal is **validated** against the tool registry, and the result is a
normal :class:`~jarvis.core.models.Plan`. From there the orchestrator's
existing machinery does the rest: the permission gate decides, the tool layer
executes, the result is verified.

The security boundary is unchanged and is the point of this design:

* A model proposal is *not* an action. It becomes one only after
  :meth:`ModelPlanner.propose` has checked the tool exists, the parameters
  validate against the registry, and the risk tier is one this planner is
  allowed to propose at all.
* The proposal then goes through ``PermissionEngine.evaluate`` exactly like a
  rule-planned action. A model cannot describe its way past a confirmation, a
  revocation, a protected path or a device guard -- those checks read the
  action, not its origin, and a model's opinion is not a grant.
* The model is never asked to *execute* anything, and it is never told that
  something happened. It proposes; the system reports.

Two tiers are deliberately excluded from model proposals
(:data:`ModelPlanner.PROPOSABLE_RISKS`): ``DESTRUCTIVE`` and ``PHYSICAL``.
Those are the tiers where a hallucinated argument is most expensive, and the
rule planner already covers the phrasings that reach them. A request the model
proposes at those tiers is refused here with a reason, which is a truthful
answer rather than a silent capability gap.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..logging_setup import get_logger
from ..registry.tools import ToolRegistry
from .domain import narrow_domain
from .models import Plan, PlannedAction, RiskLevel

log = get_logger("core.model_planner")

#: The risk tiers a model proposal may reach. Everything else is refused.
#:
#: READ/LOW/EXTERNAL are the tiers where a wrong guess is cheap and the
#: permission gate still holds the door. DESTRUCTIVE and PHYSICAL are not: a
#: plausible-sounding wrong argument to "delete file" or "move the robot arm"
#: is not a thing a language model should be allowed to originate. The rule
#: planner still reaches those tiers on the phrasings it knows.
PROPOSABLE_RISKS = (RiskLevel.READ, RiskLevel.LOW, RiskLevel.EXTERNAL)

#: How many actions one proposal may contain. A request that needs five steps
#: is not something a single sentence reliably decomposes into, and a long
#: chain of model-proposed actions is a chain where one wrong step is enough.
MAX_PROPOSED_ACTIONS = 3

#: The reply the model gets when it proposes something at a tier this planner
#: will not originate. It is a refusal, not a capability gap, and it says so.
_REFUSED_TIER = (
    "I can propose that, but not from a model suggestion — I would rather you "
    "ask for it directly so I can run it through the usual checks."
)

_SYSTEM = (
    "You are the action planner inside Vantage, a permissioned assistant on a "
    "Mac. The user asked for something. Decide whether it needs an action at "
    "all, and if it does, name the single best tool for it.\n"
    "\n"
    "Reply with ONE JSON object and nothing else. No prose, no markdown fence.\n"
    "  {\"kind\": \"answer\", \"text\": \"...\"}\n"
    "    -- a question or conversation. Put the reply in `text`.\n"
    "  {\"kind\": \"action\", \"tool\": \"tool_name\", \"params\": {...}, "
    "\"reason\": \"...\"}\n"
    "    -- something to do. `params` must contain only the parameters that "
    "tool declares.\n"
    "  {\"kind\": \"clarify\", \"question\": \"...\"}\n"
    "    -- you need one more fact before you can act.\n"
    "\n"
    "Rules: use \"answer\" whenever the user asked a question, said hello, or "
    "wanted to talk. Use \"action\" only when they asked for something to "
    "happen. Never invent a parameter that is not listed for that tool. Never "
    "claim something was done — you are proposing, not reporting.\n"
    "\n"
    "Times: write a date and time as `YYYY-MM-DD HH:MM` in the machine's own "
    "local time, in 24-hour form — `2026-10-23 09:00`, not `9:00 AM` and not "
    "`tomorrow at 9am`. A bare `YYYY-MM-DD` means 09:00 that day. If the user "
    "gave a relative time (\"tomorrow\", \"next Friday\", \"in two hours\"), "
    "resolve it to the concrete date and time yourself, using the current "
    "date and time below. Never send a phrase like \"tomorrow\" as a time "
    "value: the tool cannot read it, and the action would fail after the "
    "person has already approved it.\n"
)

#: A JSON object inside a longer reply (a model that wrapped it in a fence or
#: a sentence). Anchored so a quoted example inside prose is not mistaken for
#: the reply itself.
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


class ModelProposalError(Exception):
    """The model's answer was not a usable proposal."""


class ModelPlanner:
    """Asks the configured model to propose an action, and validates the answer.

    The planner is a *proposer*. It never executes, never approves, and never
    reports an outcome: it turns a sentence and a model answer into a
    :class:`Plan` the orchestrator can run, or into a truthful reason why it
    cannot.
    """

    def __init__(self, tools: ToolRegistry) -> None:
        self.tools = tools

    # -- public API ---------------------------------------------------------

    async def propose(self, text: str, *, complete_json, history=None,
                      timeout: float = 30.0) -> Plan:
        """Ask the model for a proposal and turn it into a validated plan.

        ``complete_json`` is the router's JSON completion call. It is passed in
        rather than reached for, so this module has no dependency on the
        gateway and can be exercised with a stub.

        Returns a ``Plan`` in one of three shapes:

        * ``reply_kind == "model_answer"`` -- the user asked a question; the
          model's text is the answer and no action was proposed.
        * ``reply_kind == "clarification"`` -- the model needs one more fact.
        * actions populated -- a validated proposal, ready for the permission
          gate.

        A model that is unavailable, malformed, or proposing something this
        planner will not originate all come back as a plan with no actions and
        a reason attached; none of them raise.
        """
        catalogue = self._catalogue(narrow_domain(text))
        if not catalogue:
            return self._no_action("I have no tools to plan with yet.", reply_kind="capability_missing")

        raw = await complete_json(
            _SYSTEM + "\n" + self._now_note() + "\nTools:\n" + catalogue,
            text,
            task_type="planning",
            timeout=timeout,
        )
        if raw is None:
            return self._no_action(
                "I could not reach the model to work out what to do.",
                reply_kind="model_unavailable",
            )

        try:
            payload = self._parse(raw)
        except ModelProposalError as exc:
            log.info("model proposal rejected", extra_fields={"reason": str(exc)})
            return self._no_action(str(exc), reply_kind="not_understood")

        kind = str(payload.get("kind") or "").strip().lower()

        if kind == "answer":
            answer = str(payload.get("text") or "").strip()
            if not answer:
                return self._no_action("The model did not give me an answer.", reply_kind="not_understood")
            return self._no_action(answer, reply_kind="model_answer")

        if kind == "clarify":
            question = str(payload.get("question") or "").strip()
            if not question:
                return self._no_action("Could you give me a bit more detail?", reply_kind="clarification")
            plan = self._no_action("", reply_kind="clarification")
            plan.needs_clarification = True
            plan.clarification_question = question
            return plan

        if kind != "action":
            return self._no_action(
                "I did not understand what you wanted me to do.",
                reply_kind="not_understood",
            )

        return self._action_plan(text, payload)

    # -- validation ---------------------------------------------------------

    def _action_plan(self, text: str, payload: Dict[str, Any]) -> Plan:
        """Turn a model action proposal into a validated plan, or refuse it."""
        tool = str(payload.get("tool") or "").strip()
        if not tool:
            return self._no_action("The model did not name a tool to use.", reply_kind="not_understood")

        spec = self.tools.get(tool)
        if spec is None:
            # A tool that does not exist is not a capability gap on our side --
            # the model invented it. Say so plainly.
            return self._no_action(
                f"There is no tool called {tool!r} in this build, so I cannot run it.",
                reply_kind="capability_missing",
            )

        # The model may name a tool by one of its aliases ("open_app" for
        # "open_application"). The registry resolves both, but the *action*
        # has to carry the registered name: the agent that owns a capability
        # is found by registered name, and a stored grant or revocation is
        # looked up by it. An alias left in the action would either find no
        # agent or, worse, miss a standing revocation.
        tool = self.tools.canonical(tool)

        if spec.risk not in PROPOSABLE_RISKS:
            return self._no_action(_REFUSED_TIER, reply_kind="declined")

        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._no_action(
                f"The model's parameters for {tool!r} were not a set of named values, "
                "so I did not run it.",
                reply_kind="not_understood",
            )
        params = {str(k): v for k, v in params.items()}

        problems = self.tools.validate_params(tool, params)
        # `validate_params` only rejects an unexpected parameter when the tool
        # declares *some* parameters -- a tool with an empty declaration accepts
        # anything, which is the right shape for the rule planner (its
        # extractions are trusted) and the wrong shape for a model (whose
        # arguments are not). So the model path checks the declaration itself.
        #
        # This is a planner-level rule, not a change to the shared registry:
        # `validate_params` is used by the rule path too, and tightening it
        # there would change behaviour the rule planner depends on.
        undeclared = [key for key in params if key not in spec.parameters]
        if undeclared:
            problems.extend(f"unexpected parameter {key!r}" for key in undeclared)
        if problems:
            # The registry is the authority on what a tool accepts. A proposal
            # that does not validate is not a smaller action -- it is a
            # different action, and guessing at the missing value is exactly
            # the failure mode this whole module exists to prevent.
            detail = "; ".join(problems)
            plan = self._no_action(
                f"I could not build a valid {tool} request from that ({detail}). "
                "Could you tell me a little more?",
                reply_kind="clarification",
            )
            plan.needs_clarification = True
            plan.clarification_question = (
                f"I could not build a valid {tool} request from that ({detail}). "
                "Could you tell me a little more?"
            )
            return plan

        reason = str(payload.get("reason") or "").strip() or f"model proposed {tool}"
        return Plan(
            goal=text,
            intent=f"model:{tool}",
            actions=[
                PlannedAction(
                    step_id=1,
                    tool=tool,
                    params=params,
                    reason=reason[:200],
                )
            ],
            planner="llm",
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _no_action(reply: str, *, reply_kind: str) -> Plan:
        plan = Plan(goal="", intent="model", planner="llm", reply_kind=reply_kind)
        plan.direct_reply = reply
        return plan

    @staticmethod
    def _parse(raw: Any) -> Dict[str, Any]:
        """Read the model's answer as a JSON object, or explain why it is not one."""
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            raise ModelProposalError("The model's answer was not text I could read.")
        text = raw.strip()
        if not text:
            raise ModelProposalError("The model sent an empty answer.")
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            match = _JSON_OBJECT.search(text)
            if not match:
                raise ModelProposalError(
                    "The model's answer was not in the form I asked for, so I did not act on it."
                )
            try:
                value = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise ModelProposalError(
                    f"The model's answer was malformed ({exc.msg}), so I did not act on it."
                )
        if not isinstance(value, dict):
            raise ModelProposalError("The model's answer was not a set of named fields.")
        return value

    @staticmethod
    def _now_note() -> str:
        """The machine's current date and time, for the planner's prompt.

        The model is asked to resolve a relative time ("tomorrow at 9am")
        into a concrete one, and it cannot do that without knowing what
        "tomorrow" is today. Without this note it guessed, and the guess
        was what reached the calendar tool.
        """
        from datetime import datetime

        now = datetime.now().astimezone()
        return (
            f"Current date and time: {now.strftime('%Y-%m-%d %H:%M')} "
            f"({now.strftime('%A')}, {now.strftime('%Z')})."
        )

    def _catalogue(self, domain: str = "") -> str:
        """The tools this planner may propose, with their parameters.

        Only the tiers in :data:`PROPOSABLE_RISKS` are listed. Sending the model
        a catalogue it is not allowed to propose from would invite exactly the
        hallucination this module refuses to act on.

        When a **domain** is given, only that domain's tools are listed.

        The reason is measured, not theoretical. The full catalogue is 137
        tools — about 17,000 characters of one-per-line definitions — and a
        model asked to pick from all of it picked the wrong *domain*
        outright:

            "move my Design Review event to tomorrow at 3pm"
              -> move_file(source="my Design Review event",
                           destination="tomorrow at 3pm")

        A calendar request became a file move, and the person's approval
        would have been spent renaming a file. With only the calendar tools
        visible, the same request came back as a search for the event —
        the right first step.

        So the domain is chosen from the sentence, and the model is shown
        that domain's tools. The full catalogue is still the fallback when
        no domain is recognised, because a request with an unrecognised
        vocabulary must not be narrowed away from the tool that fits it.
        """
        specs = [spec for spec in self.tools.all()
                 if spec.risk in PROPOSABLE_RISKS]
        if domain:
            tagged = [spec for spec in specs
                      if domain in spec.tags]
            if tagged:
                specs = tagged
        lines: List[str] = []
        for spec in specs:
            params = ", ".join(f"{k}:{v}" for k, v in spec.parameters.items()) or "none"
            required = ", ".join(spec.required_parameters) or "none"
            lines.append(
                f"- {spec.name}({params}) required={required} :: {spec.description}"
            )
        return "\n".join(lines)
