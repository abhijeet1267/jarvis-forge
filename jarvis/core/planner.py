"""Intent detection and task planning (sections 9, 34).

The ``RulePlanner`` is a deterministic, fully offline intent parser. It is the
default planner and the guaranteed fallback for the LLM planner -- so the core
loop works with no API key, no network and no vendor.

Design rules:

* A plan may only reference tools that exist in the registry.
* A plan may only supply parameters that the tool declares.
* Detection runs on lowercased text, but *argument extraction runs on the
  original text* -- otherwise case-sensitive file paths get mangled.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import get_settings
from ..logging_setup import get_logger
from ..registry.apps import AppRegistry
from ..registry.tools import ToolRegistry
from .models import Plan, PlannedAction

log = get_logger("core.planner")

_WAKE = re.compile(r"^\s*(hey\s+|ok\s+|okay\s+)?jarvis[,.\s]+", re.I)
_WS = re.compile(r"\s+")

#: The only sequence markers this planner will decompose on. A bare "then" is
#: deliberately excluded -- it appears inside the content a user asks to be
#: written ("containing: first line then second line"), and splitting there would
#: both truncate that content and invent a step nobody asked for. So a sequence
#: has to be joined with an explicit conjunction.
_SEQUENCE = re.compile(r"\s+and\s+then\s+|\s*,\s*and\s+then\s+|\s*,\s+then\s+", re.I)

#: Words that unambiguously name a vehicle. Kept narrow on purpose: "fuel" or
#: "mileage" alone can belong to an app or a laptop question, so they only route
#: to the car when a word from this list (or one of the vehicle-exclusive
#: readings below) is present.
_CAR_WORDS = r"\b(car|cars|vehicle|vehicles|\bobd\b|obd-?ii|ignition|check engine)\b"

#: Phrase -> metric key, ordered most specific first: the rule takes the first
#: match, so "engine temperature" must be tried before a shorter phrase that is
#: contained inside it.
_CAR_METRIC_WORDS: Dict[str, str] = {
    "engine temperature": "coolant_temp",
    "engine temp": "coolant_temp",
    "engine rpm": "engine_rpm",
    "rev counter": "engine_rpm",
    "mass air flow": "maf_rate",
    "manifold pressure": "manifold_pressure",
    "fuel": "fuel_level",
    "petrol": "fuel_level",
    "diesel": "fuel_level",
    "odometer": "odometer",
    "mileage": "odometer",
    "rpm": "engine_rpm",
    "coolant": "coolant_temp",
    "radiator": "coolant_temp",
    "overheat": "coolant_temp",
    "oil": "oil_temp",
    "throttle": "throttle",
    "maf": "maf_rate",
    "barometric": "barometric_pressure",
    "battery": "battery_voltage",
    "voltage": "battery_voltage",
    "12v": "battery_voltage",
    "charge": "charge_level",
    "charging": "charge_level",
}

#: Readings whose word is common enough that it must not route to the car on its
#: own -- a file called "castor oil", a "33 rpm" record, a charging cable or a
#: mileage tracker are all real phrases. They reach a car tool only when the car
#: is named as well.
_CAR_NEEDS_CAR_WORD = {
    "oil", "throttle", "maf", "mileage", "rev counter", "rpm",
    "battery", "voltage", "12v", "charge", "charging",
}


@dataclass
class Intent:
    """A parsed intent before it becomes a plan."""

    name: str
    params: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.8
    note: Optional[str] = None
    tool: Optional[str] = None


# Reasons a capability cannot answer *right now*, named for what is actually
# missing. These used to be "not implemented" placeholders; email, calendar and
# transfer all have working local backends now, and a capability that exists
# must never be described as absent. Being precise here is what keeps JARVIS
# honest instead of hallucinating success.
UNAVAILABLE_REASONS: Dict[str, str] = {
    "phone": (
        "no phone is paired with this laptop yet, so there is nothing to read "
        "from it"
    ),
}

#: Wording that names a phone-like destination.
_DEVICE_WORDS = r"(phone|mobile|android|iphone|pixel|handset)"

#: Wording that names the wearable. Deliberately narrow -- "watch" is also a
#: verb, which the rule itself filters.
_WATCH_WORDS = r"\b(watch|wrist|smartwatch)\b"

RuleFn = Callable[[str, str], Optional[Intent]]


class RulePlanner:
    """Deterministic natural-language -> plan mapper."""

    def __init__(self, tools: ToolRegistry, apps: Optional[AppRegistry] = None) -> None:
        self.tools = tools
        self.apps = apps or AppRegistry()
        self._rules: List[RuleFn] = [
            self._emergency_stop,
            self._help,
            self._list_apps,
            self._screenshot,
            self._car,
            self._location,
            self._watch,
            self._battery,
            self._system_status,
            self._time,
            self._memory_controls,
            self._recall,
            self._remember,
            self._explain_failure,
            self._project,
            self._list_directory,
            self._compress,
            self._delete,
            self._copy_move,
            self._write_file,
            self._append_file,
            self._close_app,
            self._open_app,
            self._research,
            self._vision,
            self._search_files,
            self._design,
            self._laptop_center_prompt,
            self._coding,
            self._device_management,
            self._transfer,
            self._scheduler,
            self._email,
            self._calendar,
            self._home,
            self._robotics,
            self._laptop_center,
            self._run_command,
        ]

    # -- public --------------------------------------------------------------
    def parse(self, text: str, context: Optional[Dict[str, Any]] = None) -> Plan:
        context = context or {}
        raw = _WS.sub(" ", _WAKE.sub("", text.strip())).strip()
        lowered = raw.lower()

        composed, dropped = self._composed(raw, text)
        if composed is not None:
            return composed

        intent = self._first_intent(lowered, raw)
        plan = self._to_plan(text, intent) if intent else self._unrecognised(text)
        plan.unhandled = dropped
        return plan

    def _unrecognised(self, text: str) -> Plan:
        return Plan(
            goal=text,
            intent="unknown",
            reply_kind="not_understood",
            direct_reply=(
                "I understood the words but not the goal yet. Try things like "
                '"open VS Code", "find my report", "what is my battery", '
                '"remember that ...", or "what did I work on yesterday".'
            ),
            planner="rules",
        )

    def _first_intent(self, text: str, raw: str) -> Optional[Intent]:
        """Run the rule chain and return the first intent that matches."""
        for rule in self._rules:
            try:
                intent = rule(text, raw)
            except Exception as exc:  # pragma: no cover - defensive
                log.info("planner rule error", extra_fields={"error": str(exc)})
                continue
            if intent:
                return intent
        return None

    def _composed(self, raw: str, original: str) -> Tuple[Optional[Plan], List[str]]:
        """Turn one sentence that states a sequence into an ordered multi-step plan.

        Returns ``(plan, dropped)``. Every clause has to match an existing
        tool-backed route for a plan to be built; if any clause fails, the whole
        sentence falls back to single-route parsing, which is the conservative
        answer -- the cost of not decomposing is that JARVIS asks for more detail,
        the cost of decomposing wrongly is that it performs an action nobody asked
        for.

        The clauses that did not become steps are named back, with the reason the
        route itself gave. Until Phase 19 they were computed and discarded, so
        "what is the time and then create a file called ../x.txt containing: nope"
        answered with the time and left the refusal unmentioned.
        """
        clauses = [clause.strip() for clause in _SEQUENCE.split(raw) if clause.strip()]
        if len(clauses) < 2:
            return None, []

        actions: List[PlannedAction] = []
        dropped: List[str] = []
        for index, clause in enumerate(clauses):
            intent = self._first_intent(clause.lower(), clause)
            if intent is not None and intent.tool and self.tools.has(intent.tool):
                actions.append(
                    PlannedAction(
                        step_id=len(actions) + 1,
                        tool=intent.tool,
                        params=intent.params,
                        reason=f"step {len(actions) + 1} of the stated sequence ({intent.name})",
                    )
                )
                continue
            # A route that refused already explains itself; a clause nothing
            # matched is handed back in its own words rather than guessed at.
            dropped.append(
                intent.note if intent is not None and intent.note
                else f'I don\'t know how to "{clause}".'
            )

        if dropped:
            return None, dropped
        return Plan(goal=original, intent="workflow", actions=actions, planner="rules"), []

    # -- helpers -------------------------------------------------------------
    def _to_plan(self, original: str, intent: Intent) -> Plan:
        plan = Plan(goal=original, intent=intent.name, planner="rules")

        if intent.note and not intent.tool:
            plan.direct_reply = intent.note
            plan.reply_kind = "declined"
            return plan

        if not intent.tool:
            plan.needs_clarification = True
            plan.reply_kind = "declined"
            plan.clarification_question = intent.note or "Could you give me a bit more detail?"
            return plan

        if not self.tools.has(intent.tool):
            plan.direct_reply = f"I do not have a capability called {intent.tool!r} yet."
            plan.reply_kind = "capability_missing"
            return plan

        plan.actions = [
            PlannedAction(
                step_id=1,
                tool=intent.tool,
                params=intent.params,
                reason=f"matched intent {intent.name}",
            )
        ]
        return plan

    @staticmethod
    def _arg(raw: str) -> str:
        """Clean an extracted path argument without changing its case."""
        value = raw.strip().strip('"').strip("'").strip()
        value = re.sub(r"[.,;!?]+$", "", value).strip()
        return value

    @staticmethod
    def _match(pattern: str, raw: str, group: int = 1) -> Optional[str]:
        """Case-insensitive match against the original text, preserving case."""
        found = re.search(pattern, raw, re.I)
        return found.group(group) if found else None

    # -- file creation / modification ----------------------------------------
    #
    # Deliberately narrow. These two rules exist so a typed sentence can reach
    # `write_file` at all -- Phase 11 proved the tool and the pipeline, but the
    # planner had no route, so the capability was unreachable from the UI. This
    # is not a general file-manipulation parser, and it is not trying to be one:
    # a request that does not match these shapes stays `unknown`, which is the
    # safe answer.
    _CREATE_FILE = re.compile(
        r"\b(?:create|make|write|save)\b(?:(?:\s+(?:a|an|new|the|this|my|text|empty|simple))+\s+)?"
        r"file\s+(?:called|named|as|under the name|to)\s+"
        r"[\"'“]?(?P<path>[^\"'”\n]+?)[\"'”]?\s+"
        r"(?:containing|with the contents?|with|that says|reading)\s*:?\s*(?P<content>.*)$",
        re.I,
    )
    _APPEND_TO = re.compile(
        r"\bappend\b.*?\b(?:to|into)\s+[\"'“]?(?P<path>[\w./~+-]+)[\"'”]?\s*"
        r"(?::\s*(?P<content>.+)|\s+(?P<content_bare>.+))$",
        re.I,
    )

    def _anchored(self, name: str) -> tuple[Optional[str], Optional[str]]:
        """Resolve a user-typed filename inside the file workspace.

        Returns ``(path, None)`` or ``(None, refusal)``. The route is
        workspace-only: any name that resolves outside it is refused here,
        whether it walked out with ``..``, went through a symlink, or was given
        as an absolute path. So a typed sentence cannot reach a system file at
        all, rather than reaching it and relying on the next layer to notice.

        That is stricter than the system guard alone, which only refuses *OS*
        protected locations and would happily accept ``~/escape.txt``. The
        engine's path guard still runs on whatever is produced; this narrows the
        route, it does not replace the guard. Writing anywhere else still
        requires the direct tool call a non-speaking client makes with an
        explicit path.
        """
        workspace = Path(os.path.expanduser(get_settings().file_workspace)).resolve()
        candidate = Path(os.path.expanduser(name.strip()))
        if not candidate.is_absolute():
            candidate = workspace / candidate
        resolved = candidate.resolve()
        inside = resolved == workspace or str(resolved).startswith(str(workspace) + os.sep)
        if not inside:
            return None, (
                f"{name.strip()} lands outside my file workspace ({workspace}), so I "
                "will not create it by name. If you really mean that location, give "
                "me the full path and I will ask before writing."
            )
        return str(resolved), None

    def _write_file(self, text: str, raw: str) -> Optional[Intent]:
        found = self._CREATE_FILE.search(raw)
        if not found:
            return None
        name = (found.group("path") or "").strip()
        content = (found.group("content") or "").strip().strip("\"'”“")
        if not name:
            return Intent("write_file", note="What should the file be called?")
        if not content:
            return Intent("write_file", note=f"What should I put in {name}?")
        path, refusal = self._anchored(name)
        if refusal:
            return Intent("write_file", note=refusal)
        return Intent(
            "write_file",
            tool="write_file",
            params={"path": path, "content": content + "\n", "mode": "overwrite"},
            confidence=0.9,
        )

    def _append_file(self, text: str, raw: str) -> Optional[Intent]:
        found = self._APPEND_TO.search(raw)
        if not found:
            return None
        content = (found.group("content") or found.group("content_bare") or "").strip()
        name = (found.group("path") or "").strip()
        if not name or not content:
            return None
        path, refusal = self._anchored(name)
        if refusal:
            return Intent("append_file", note=refusal)
        return Intent(
            "append_file",
            tool="write_file",
            params={"path": path, "content": content + "\n", "mode": "append"},
            confidence=0.9,
        )

    # -- rules ---------------------------------------------------------------
    def _emergency_stop(self, text: str, raw: str) -> Optional[Intent]:
        if re.search(r"\b(emergency stop|stop everything|stop all|halt all|abort everything)\b", text):
            return Intent("emergency_stop", tool="emergency_stop", confidence=0.99)
        if re.fullmatch(r"stop", text):
            return Intent("emergency_stop", tool="emergency_stop", confidence=0.9)
        if re.search(r"\bstop\b.*\b(robot|robots|amr|amrs|arm|arms|physical|experiment|machine)\b", text):
            return Intent("emergency_stop", tool="emergency_stop", confidence=0.95)
        return None

    def _help(self, text: str, raw: str) -> Optional[Intent]:
        if re.search(r"\b(help|what can you do|capabilities|commands)\b", text):
            names = ", ".join(self.tools.names())
            return Intent(
                "help",
                note=(
                    "I can open and close applications, search/copy/move/delete files, "
                    "read system and battery status, take screenshots, run allowlisted "
                    "commands, remember facts and answer questions about your projects. "
                    f"Registered tools: {names}."
                ),
                confidence=0.9,
            )
        return None

    def _list_apps(self, text: str, raw: str) -> Optional[Intent]:
        if re.search(r"\b(list|show|what)\b.*\bapps?\b", text) or "available apps" in text:
            return Intent("list_apps", tool="list_apps")
        return None

    def _screenshot(self, text: str, raw: str) -> Optional[Intent]:
        if re.search(r"\b(screenshot|screen shot|capture the screen|grab the screen)\b", text):
            return Intent("screenshot", tool="take_screenshot", confidence=0.95)
        return None

    _CAR_FAULT_WORDS = (
        r"\b(diagnostic|diagnostics|trouble code|trouble codes|error code|error codes"
        r"|fault code|fault codes|\bdtc\b|dtcs|check engine|engine light)\b"
    )
    _CAR_SERVICE_WORDS = r"\b(service|servicing|maintenance|maintain|oil change|inspection|wrench)\b"

    def _car(self, text: str, raw: str) -> Optional[Intent]:
        """Vehicle intents (plan §18, grown from the v0.1 stub into v1.7 tools).

        The keyword gate is what lets this rule sit early in the list without
        stealing anything: it claims a phrase only when the car is named, or
        when a vehicle-exclusive reading (fuel, odometer, coolant, rpm)
        appears. A plain "what is my battery" stays with the laptop battery
        rule, "diagnostic codes" on its own stays unclaimed, and
        "what is the status of my robots" stays with robotics.
        """
        car_named = bool(re.search(_CAR_WORDS, text))
        metric_word = next((phrase for phrase in _CAR_METRIC_WORDS if phrase in text), "")
        if metric_word in _CAR_NEEDS_CAR_WORD:
            if not car_named:
                return None
        elif not car_named and not metric_word:
            return None
        # "remember that my car is a diesel" is a memory, not a request to read
        # the car. This rule runs before _remember, so it has to say so.
        if re.search(r"\b(remember|note that|note this|don't forget|dont forget)\b", text):
            return None

        if re.search(self._CAR_FAULT_WORDS, text) or re.search(r"\b(mil|warning light|dashboard light)\b", text):
            return Intent("car_diagnostics", tool="get_car_diagnostics", confidence=0.9)
        if re.search(self._CAR_SERVICE_WORDS, text):
            return Intent("car_service_status", tool="get_car_service_status", confidence=0.85)

        if metric_word:
            return Intent(
                "car_telemetry",
                tool="get_car_telemetry",
                params={"metric": _CAR_METRIC_WORDS[metric_word]},
                confidence=0.9 if car_named else 0.8,
            )
        return Intent("car_status", tool="get_car_status", params={"metric": "all"}, confidence=0.85)

    def _watch(self, text: str, raw: str) -> Optional[Intent]:
        """Watch companion intents (plan §4.4, v1.8).

        Claimed only when the watch is named, and deliberately not when "watch"
        is a verb ("watch this folder", "watch out"). Ordered before ``_battery``
        and ``_transfer`` so "buzz my watch" and "send a notification to my
        watch" reach the wearable instead of the laptop battery or a file
        transfer.
        """
        if not re.search(_WATCH_WORDS, text):
            return None
        if re.search(r"\bwatch(?:ing)?\b(?!\s+(?:my|the\s+smart))", text) and not re.search(
            r"\b(my|the|his|her)\s+watch\b|\bsmartwatch\b|\bwrist\b", text
        ):
            return None

        if re.search(r"\b(vibrat\w*|buzz|haptic)\b", text):
            if re.search(r"\bsos\b|\bemergenc\w+\b", text):
                pattern = "sos"
            elif re.search(r"\blong\b", text):
                pattern = "long"
            elif re.search(r"\balert\b|\balarm\b", text):
                pattern = "alert"
            else:
                pattern = "short"
            return Intent("watch_vibrate", tool="vibrate_watch", params={"pattern": pattern},
                          confidence=0.9)

        routine = self._watch_routine(text)
        if routine:
            return Intent("watch_routine", tool="run_watch_routine",
                          params={"routine": routine}, confidence=0.9)

        if re.search(r"\b(outbox|queued|pending|waiting|unanswered|did .{0,20}answer)\b", text):
            return Intent("watch_outbox", tool="get_watch_outbox", confidence=0.85)

        if re.search(r"\b(confirm|approve|approval|deny|answer)\b", text):
            question = self._watch_text_arg(raw)
            return Intent(
                "watch_confirm",
                tool="ask_watch_confirmation",
                params={"question": question} if question else {},
                confidence=0.85,
            )

        if re.search(r"\b(notify|notification|notifications|show|display|send)\b", text):
            title = self._watch_text_arg(raw)
            return Intent(
                "watch_notify",
                tool="send_watch_notification",
                params={"title": title} if title else {},
                confidence=0.85,
            )

        return Intent("watch_status", tool="get_watch_status", confidence=0.85)

    @staticmethod
    def _watch_routine(text: str) -> str:
        """Quick-routine name when the phrase asks for one, else ""."""
        if not re.search(r"\b(start|run|do|trigger|begin|begin)\b", text):
            return ""
        if re.search(r"\bstudy\b", text):
            return "study"
        if re.search(r"\bsleep\b|bedtime", text):
            return "sleep"
        if re.search(r"\bleav\w*\b.*\bhome\b|\baway\b", text):
            return "leave_home"
        if re.search(r"\barriv\w+\b.*\bhome\b|\bhome\b.*\barriv\w+\b", text):
            return "arrive_home"
        if re.search(r"\bsos\b|\bemergenc\w+\b", text):
            return "emergency"
        return ""

    @staticmethod
    def _watch_text_arg(raw: str) -> str:
        """The text a watch prompt should show: a quote, or the clause after "that".

        Sliced from the original text, not the lowercased copy, so a notification
        reading "Build Finished" keeps its case.
        """
        quoted = re.search(r"[\"'“‘](.+?)[\"'”’]", raw)
        if quoted:
            return quoted.group(1).strip()
        tail = re.search(r"\bthat\s+(.+)$", raw, re.I)
        if tail:
            return tail.group(1).strip(" .!?")
        return ""

    def _location(self, text: str, raw: str) -> Optional[Intent]:
        if re.search(r"\b(location summary|where (have|did) i|places i|travel (summary|history))\b", text):
            date = "yesterday" if "yesterday" in text else "today"
            return Intent("location_summary", tool="get_location_summary", params={"date": date})
        return None

    def _battery(self, text: str, raw: str) -> Optional[Intent]:
        if not re.search(r"\bbattery\b|\bpower level\b|\bcharge level\b", text):
            return None
        # "watch" is absent here on purpose: _watch runs earlier and answers a
        # watch battery from the watch's own reported state.
        if re.search(r"\b(phone|mobile|android)\b", text):
            return Intent(
                "phone_battery",
                note=f"I cannot read that device's battery: {UNAVAILABLE_REASONS['phone']}.",
            )
        # A robot's battery is read via robot telemetry, never laptop battery.
        if re.search(r"\b(amr|amrs|robot|robots|robot arm|\barm\b)\b", text):
            return None
        return Intent("laptop_battery", tool="battery_status", confidence=0.95)

    def _system_status(self, text: str, raw: str) -> Optional[Intent]:
        if re.search(r"\b(system status|system health|cpu|memory usage|disk usage|load average)\b", text):
            return Intent("system_status", tool="system_status")
        return None

    def _time(self, text: str, raw: str) -> Optional[Intent]:
        if re.search(r"\b(what time|the time|current time|what.s the date|today.s date)\b", text):
            return Intent("time", tool="get_time", confidence=0.9)
        return None

    def _remember(self, text: str, raw: str) -> Optional[Intent]:
        # "do you remember X?" is a recall, not a store -- never treat it as one.
        if re.search(r"\b(do|did|can|could|would)\s+you\s+remember\b", text):
            return None
        content = self._match(r"\b(?:remember|note|keep in mind)\b[:,]?\s+(?:that\s+)?(.+)", raw)
        if content is None:
            return None
        content = content.strip()
        if len(content) < 3:
            return Intent("remember", note="What should I remember?", confidence=0.5)
        project = ""
        project_match = self._match(r"\b(?:for|in|of)\s+(?:my\s+)?([A-Za-z0-9 ]+?)\s+project\b", raw)
        if project_match:
            project = project_match.strip()
        return Intent(
            "remember",
            tool="remember_fact",
            params={"content": content, "scope": "long_term", "project": project},
        )

    def _memory_controls(self, text: str, raw: str) -> Optional[Intent]:
        """Inspect, export, delete and configure memory (v0.9).

        Runs before ``_recall``: "what do you remember" contains "do you
        remember", so the broader recall pattern would otherwise claim it.
        """
        # --- overview ------------------------------------------------------
        # "what do you remember about the deploy runbook" names a topic and wants
        # its content; answering it with item counts is a non-answer. The overview
        # keeps the bare phrasings, which really do ask for the state of memory.
        has_topic = re.search(
            r"\bwhat do you (?:remember|know)\b\s+(?:about|on|regarding|of)\s+\S", text)
        if re.search(r"\b(what do you (remember|know)|memory (overview|status|summary)|"
                     r"what's in your memory|whats in your memory)\b", text) and not has_topic:
            return Intent("memory_overview", tool="memory_overview", params={})

        # --- export --------------------------------------------------------
        if re.search(r"\b(export|dump|back ?up)\b.*\b(memor\w*)\b", text):
            scope = self._match(
                r"\b(?:export|dump|back ?up)\b.*?\b(temporary|session|project|long_term|long term|private)\b",
                raw,
            )
            scope = (scope or "").replace(" ", "_")
            return Intent(
                "export_memories",
                tool="export_memories",
                params={"scope": scope} if scope else {},
            )

        # --- clear a scope -------------------------------------------------
        if re.search(r"\b(clear|wipe|erase|empty)\b.*\b(memor\w*)\b", text):
            scope = self._match(
                r"\b(temporary|session|project|long[_ ]term|private)\b", raw
            )
            if scope:
                return Intent(
                    "clear_memory_scope",
                    tool="clear_memory_scope",
                    params={"scope": scope.replace(" ", "_")},
                )
            return Intent(
                "clear_memory_scope",
                note=(
                    "Which scope should I clear? One of: temporary, session, "
                    "project, long_term, private."
                ),
            )

        # --- forget one ----------------------------------------------------
        if re.search(r"\b(forget|delete)\b.*\b(that|this|it|memor\w*)\b", text):
            memory_id = self._match(r"\b(MEM-[0-9a-f]{6,})\b", raw)
            if memory_id:
                return Intent(
                    "forget_memory", tool="forget_memory", params={"memory_id": memory_id}
                )
            return Intent(
                "forget_memory",
                note=(
                    "Which memory should I forget? Tell me its id (MEM-…), or ask "
                    'me to "show what you remember" first.'
                ),
            )

        # --- categories ----------------------------------------------------
        if re.search(r"\b(stop|disable|dont|don't)\b.*\b(remember\w*|storing)\b", text):
            category = self._match(
                r"\b(preferences?|facts?|tasks?|projects?|decisions?|experiments?|"
                r"failures?|fixes|documents?|conversations?|devices?)\b",
                raw,
            )
            if category:
                singular = {
                    "preferences": "preference", "facts": "fact", "tasks": "task",
                    "projects": "project", "decisions": "decision",
                    "experiments": "experiment", "failures": "failure",
                    "fixes": "fix", "documents": "document",
                    "conversations": "conversation", "devices": "device",
                }.get(category.lower(), category.lower())
                return Intent(
                    "set_memory_category",
                    tool="set_memory_category",
                    params={"category": singular, "enabled": False},
                )
            return Intent(
                "set_memory_category",
                note=(
                    "Which kind should I stop storing? For example: preferences, "
                    "facts, decisions, failures, documents."
                ),
            )

        return None

    def _recall(self, text: str, raw: str) -> Optional[Intent]:
        if not re.search(r"\b(what did i work on|what have i (worked|done)|what did i do|recall|do you remember)\b", text):
            return None
        query = self._match(
            r"\b(?:what did i work on|what have i (?:worked|done)|what did i do|recall|do you remember)\b\s*(.*)",
            raw,
        )
        query = (query or "").strip()
        # "what do you remember about X" hands the extractor "about X"; the
        # preposition is not part of what the user is looking for.
        query = re.sub(r"^(?:about|on|regarding|of|over|if|whether)\s+", "", query, flags=re.I).strip()

        # A date word is a range to read, not a phrase to search for. Turning
        # "yesterday" into the query string "yesterday work" made an undated,
        # best-ranked lookup look like an answer about the day that was asked about.
        since = until = ""
        if re.search(r"\byesterday\b", query, re.I):
            since = until = (date.today() - timedelta(days=1)).isoformat()
        elif re.search(r"\btoday\b", query, re.I):
            since = until = date.today().isoformat()

        query = re.sub(r"\b(yesterday|today)\b", "", query, flags=re.I).strip()
        if not query and not since:
            query = "recent work"

        params: Dict[str, Any] = {"query": query}
        if since:
            params["since"] = since
            params["until"] = until
        return Intent("recall", tool="search_memory", params=params)

    def _explain_failure(self, text: str, raw: str) -> Optional[Intent]:
        if not re.search(r"\bwhy did\b.*?\b(fail|break|overheat)\b", text):
            return None
        project = self._match(r"\bmy\s+([A-Za-z0-9 ]+?)\s+(?:project|fail|break)", raw)
        if not project:
            return Intent(
                "explain_failure",
                note='Which project should I look up? For example: "why did my AMR project fail?"',
            )
        return Intent("explain_failure", tool="explain_failures", params={"project": project.strip()})

    def _project(self, text: str, raw: str) -> Optional[Intent]:
        # A recurring request ("every Monday summarise my project progress") is
        # a schedule, not an immediate summary -- let the scheduler rule take it.
        if re.search(r"\b(every|each|daily|weekly)\b", text):
            return None

        summary = re.search(r"\b(summary|status|progress)\b.*\bproject\b", text) or re.search(
            r"\bproject\b.*\b(summary|status|progress)\b", text
        )
        if summary:
            name = self._match(r"\b(?:my|the)\s+([A-Za-z0-9 ]+?)\s+project\b", raw)
            params = {"project": name.strip()} if name else {}
            return Intent("project_summary", tool="project_summary", params=params)

        create = self._match(
            r"\b(?:this is|new project|create (?:a )?project|start (?:a )?project|make (?:a )?project)\b[:,]?\s*(?:called|named)?\s*(.*)",
            raw,
        )
        if create is not None:
            name = create.strip()
            if not name:
                return Intent("create_project", note="What should I call the project?")
            return Intent("create_project", tool="create_project", params={"name": name})

        if re.search(r"\b(continue|resume|open)\b.*\bproject\b", text):
            return Intent(
                "continue_project",
                note=(
                    "Project continuation needs the design and file agents, which are not "
                    "in this build yet. I can still summarise a project if you name it."
                ),
            )
        return None

    def _search_files(self, text: str, raw: str) -> Optional[Intent]:
        # Trigger on a file-type word OR a filename-like token with an
        # extension, so "find nested.txt" works and not just "find my report".
        has_type_word = re.search(
            r"\b(files?|documents?|reports?|pdfs?|folders?|dirs?|directories|projects?|"
            r"images?|photos?|notes?|spreadsheets?|presentations?)\b",
            text,
        )
        has_extension = re.search(r"\b[\w./~-]+\.[a-z0-9]{2,5}\b", raw, re.I)
        if not (has_type_word or has_extension):
            return None

        captured = self._match(
            r"\b(?:find|search for|search|locate|look for|where is)\b\s+(.+)", raw
        )
        if captured is None:
            return None

        rest = captured.strip()
        # Empty means "let the tool use its own default root". Sending "~"
        # explicitly would be a lie about what the user asked for, and the path
        # guard rightly refuses a named home directory.
        directory = ""

        # "... in /path" / "... under ~/dir" narrows the search. Only treat the
        # trailing clause as a directory when it actually looks like a path,
        # otherwise "report in the AMR project" would search inside "project".
        trailing = re.search(r"\s+(?:in|under|inside)\s+(.+)$", rest, re.I)
        if trailing:
            candidate = self._arg(trailing.group(1))
            rest = rest[: trailing.start()].strip()
            if re.match(r"^(?:~|/|\./)", candidate):
                directory = candidate

        query = re.sub(r"^(?:my|the)\s+", "", rest.strip(), flags=re.I)

        # "latest"/"newest" is a request for recency ordering, not a word to
        # silently drop -- it changes which result should come back first.
        wants_recent = bool(
            re.search(r"\b(latest|newest|most recent|recent|newest)\b", rest, re.I)
        )
        query = re.sub(r"^(?:latest|newest|most recent)\s+", "", query, flags=re.I)
        query = re.sub(
            r"\s+(?:files?|documents?|reports?|pdfs?|folders?)$", "", query, flags=re.I
        ).strip()

        if not query:
            return Intent("search_files", note="What file should I look for?")

        params = {
            "query": query,
            "limit": 10,
            "sort": "recent" if wants_recent else "relevance",
        }
        if directory:
            params["directory"] = directory

        return Intent(
            "search_files",
            tool="search_files",
            params=params,
        )

    def _list_directory(self, text: str, raw: str) -> Optional[Intent]:
        captured = self._match(r"\b(?:list|show me)\b.*?\b(?:contents of|files in|folder)\b\s*(.*)", raw)
        if captured is None:
            return None
        path = self._arg(captured) or "~"
        return Intent("list_directory", tool="list_directory", params={"path": path})

    def _compress(self, text: str, raw: str) -> Optional[Intent]:
        captured = self._match(r"\b(?:compress|zip|archive)\b\s+(.+)", raw)
        if captured is None:
            return None
        path = self._arg(captured)
        if not path or path.lower() in {"this", "that", "it"}:
            return Intent("compress", note="Which file or folder should I compress?")
        return Intent("compress", tool="compress_path", params={"path": path})

    def _delete(self, text: str, raw: str) -> Optional[Intent]:
        captured = self._match(r"\b(?:delete|remove|trash|get rid of)\b\s+(.+)", raw)
        if captured is None:
            return None
        path = self._arg(captured)
        # Never infer a broad target from a vague pronoun.
        if not path or path.lower() in {"this", "that", "it", "these", "those", "them"}:
            return Intent(
                "delete",
                note=(
                    "I will not guess what to delete. Tell me the exact file, or say "
                    '"find <name>" first so we can confirm the target together.'
                ),
            )
        permanent = bool(re.search(r"\bpermanent(ly)?\b|\bfor good\b|\bunrecoverable\b", text))
        return Intent("delete", tool="delete_file", params={"path": path, "permanent": permanent})

    def _copy_move(self, text: str, raw: str) -> Optional[Intent]:
        copy_match = re.search(r"\bcopy\b\s+(.+?)\s+to\s+(.+)", raw, re.I)
        if copy_match:
            return Intent(
                "copy",
                tool="copy_file",
                params={
                    "source": self._arg(copy_match.group(1)),
                    "destination": self._arg(copy_match.group(2)),
                },
            )
        move_match = re.search(r"\b(?:move|rename)\b\s+(.+?)\s+to\s+(.+)", raw, re.I)
        if move_match:
            src = move_match.group(1).strip()
            dst = move_match.group(2).strip()
            # "move the amr to (1, 2)" is robot motion, not a file move -- defer
            # it to the robotics rule. A bare coordinate destination, or a short
            # robot reference (no path separator, no file extension) as the
            # source, is not a file path.
            is_coord = re.fullmatch(r"\(-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\)", dst)
            robot_phrase = bool(
                re.search(r"\b(amr|amrs|arm|arms|robot|robots|gripper)\b", src, re.I)
                and "/" not in src
                and not re.search(r"\.[a-z0-9]{1,5}$", src, re.I)
            )
            if is_coord or robot_phrase:
                return None
            return Intent(
                "move",
                tool="move_file",
                params={
                    "source": self._arg(move_match.group(1)),
                    "destination": self._arg(move_match.group(2)),
                },
            )
        return None

    def _close_app(self, text: str, raw: str) -> Optional[Intent]:
        captured = self._match(r"\b(?:close|quit|exit)\b\s+(.+)", raw)
        if captured is None:
            return None
        app = self.apps.resolve(self._arg(captured))
        if app is None:
            return None
        return Intent("close_app", tool="close_app", params={"app": app.key})

    def _open_app(self, text: str, raw: str) -> Optional[Intent]:
        captured = self._match(r"\b(?:open|launch|start|run)\b\s+(.+)", raw)
        if captured is None:
            return None
        app = self.apps.resolve(self._arg(captured))
        if app is None:
            return None
        return Intent("open_app", tool="open_app", params={"app": app.key}, confidence=0.9)

    def _transfer(self, text: str, raw: str) -> Optional[Intent]:
        """Send a file to a paired device (v0.6).

        Matches "send report.pdf to my phone", "transfer the latest report to my
        phone", "send this folder to my phone".
        """
        if not re.search(rf"\b(transfer|send|beam|share|copy)\b.*\b(to|onto)\b.*\b{_DEVICE_WORDS}\b", text):
            return None

        # Pull the source out of the original text, preserving its case.
        source = self._match(
            rf"\b(?:transfer|send|beam|share|copy)\b\s+(?:the\s+|my\s+)?(.+?)\s+(?:to|onto)\s+(?:my\s+|the\s+)?{_DEVICE_WORDS}\b",
            raw,
        )
        if source is None:
            return Intent("transfer", note="What should I send, and to which device?")

        source = self._arg(source)
        # "this file" / "that one" / "it" name nothing specific, so refuse
        # rather than guessing which file to send off the machine.
        vague = re.sub(r"\s+(file|folder|document|one)$", "", source, flags=re.I).strip().lower()
        if vague in {"", "this", "that", "it", "these", "those", "the"}:
            return Intent(
                "transfer",
                note=(
                    "I will not guess which file to send. Tell me the exact file, "
                    'for example: "send /tmp/report.pdf to my phone".'
                ),
            )

        # "latest"/"newest" means: resolve the newest matching file first.
        wants_latest = bool(re.search(r"\b(latest|newest|most recent)\b", source, re.I))
        source = re.sub(r"^(?:latest|newest|most recent)\s+", "", source, flags=re.I).strip()
        if not source:
            return Intent("transfer", note="Which file should I send?")

        return Intent(
            "transfer",
            tool="send_to_device",
            params={
                "path": source,
                "target_device": "phone_main",
                "resolve_latest": wants_latest,
            },
        )

    def _research(self, text: str, raw: str) -> Optional[Intent]:
        """Research Agent (v1.0).

        Runs before the file-search rule: "research X" contains the substring
        "search", and while a word-boundary pattern will not match it, putting
        this first removes any doubt.
        """
        researchy = re.search(
            r"\b(research|papers?|literature|citation|cite|references?|arxiv|"
            r"related work|state of the art)\b",
            text,
        )
        if not researchy:
            return None

        # --- gaps ----------------------------------------------------------
        if re.search(r"\b(gap|gaps|open question|unresolved|limitation)\b", text):
            return Intent("find_research_gaps", tool="find_research_gaps", params={})

        # --- brief ---------------------------------------------------------
        if re.search(r"\b(brief|summary of the research|write up)\b", text):
            topic = self._match(r"\b(?:brief|write up)\b\s+(?:on|about|for)?\s*(.*)", raw)
            if not topic:
                topic = self._match(r"\bresearch\b\s+(.*)", raw)
            if not topic:
                return Intent("research_brief", note="What topic should the brief cover?")
            return Intent("research_brief", tool="research_brief", params={"topic": topic.strip()})

        # --- compare -------------------------------------------------------
        if re.search(r"\bcompare\b", text):
            ids = re.findall(r"\b(PAPER-[0-9a-f]{6,})\b", raw)
            if not ids:
                return Intent(
                    "compare_papers",
                    note=(
                        "Which papers should I compare? Give me their ids, or ask me "
                        'to "list my papers" first.'
                    ),
                )
            return Intent(
                "compare_papers", tool="compare_papers", params={"paper_ids": ",".join(ids)}
            )

        # --- cite ----------------------------------------------------------
        if re.search(r"\b(cite|citation for)\b", text):
            paper_id = self._match(r"\b(PAPER-[0-9a-f]{6,})\b", raw)
            if not paper_id:
                return Intent(
                    "cite_paper",
                    note=(
                        "Which paper should I cite? I can only cite papers already in "
                        "my store — I will not invent a citation."
                    ),
                )
            return Intent("cite_paper", tool="cite_paper", params={"paper_id": paper_id})

        # --- list ----------------------------------------------------------
        if re.search(r"\b(list|show|what)\b.*\b(papers?|references?)\b", text):
            return Intent("list_papers", tool="list_papers", params={})

        # --- search --------------------------------------------------------
        topic = self._match(r"\b(?:research|find work on|look up|papers on|papers about)\b\s*(.*)", raw)
        if not topic:
            return Intent("search_papers", note="What should I research?")

        return Intent(
            "search_papers",
            tool="search_papers",
            params={"query": topic.strip(), "source": "arxiv", "limit": 5},
        )

    def _vision(self, text: str, raw: str) -> Optional[Intent]:
        """Vision Agent (v1.3).

        Runs before the design rule: "read this screenshot" is a vision request,
        not a documentation one — but both share the word "read".
        """
        screen = re.search(r"\b(screen|screenshot|display|what'?s on my)\b", text)
        image = re.search(r"\b(image|picture|photo|screenshot)\b", text)
        ocr = re.search(r"\b(read|ocr|extract text|what does it say|transcribe)\b", text)
        compare = re.search(r"\b(compare|diff|difference|changed|change)\b", text)
        capability = re.search(r"\b(can you see|vision)\b", text)

        # Path, if the user named one.
        path = self._match(r"(/\S+\.(?:png|jpg|jpeg|gif|webp|tiff|bmp))", raw) or \
            self._match(r"(~\S+\.(?:png|jpg|jpeg|gif|webp|tiff|bmp))", raw) or ""

        # The guard is deliberately narrow. This rule runs before file search, so
        # a loose guard would steal "find my report". Vision needs either a
        # screen reference, an image path, or an explicit capability question.
        if not (screen or path or capability):
            return None

        # --- capability check ----------------------------------------------
        if capability and not (image or screen):
            return Intent("vision_capabilities", tool="vision_capabilities", params={})

        # --- compare --------------------------------------------------------
        if compare:
            paths = re.findall(r"(/\S+\.(?:png|jpg|jpeg|gif|webp|tiff|bmp))", raw)
            if len(paths) >= 2:
                return Intent(
                    "compare_images",
                    tool="compare_images",
                    params={"first": paths[0], "second": paths[1]},
                )
            return Intent(
                "compare_images", note="Which two images should I compare?"
            )

        # --- what's on screen ----------------------------------------------
        if screen and (ocr or re.search(r"\bwhat\b", text)):
            return Intent("capture_screen", tool="capture_screen", params={"read": True})

        if screen:
            return Intent("capture_screen", tool="capture_screen", params={"read": True})

        # --- locate text in an image ---------------------------------------
        if re.search(r"\b(find|locate|where)\b", text) and (image or path):
            # Stop the query at "in/on/at", so "find the word Submit in /tmp/ui.png"
            # searches for "Submit", not "Submit in /tmp/ui.png".
            needle = self._match(
                r"\b(?:find|locate|where)\b\s+(?:the\s+)?(?:text\s+|word\s+|button\s+)?[\"']?(.*?)[\"']?\s+(?:in|on|at)\s+",
                raw,
            ) or self._match(
                r"\b(?:find|locate|where)\b\s+(?:the\s+)?(?:text\s+|word\s+|button\s+)?[\"']?(.*?)[\"']?$",
                raw,
            )
            if path and needle:
                return Intent(
                    "find_image_text",
                    tool="find_image_text",
                    params={"path": path, "query": needle.strip()},
                )

        # --- read text ------------------------------------------------------
        if ocr and (image or path):
            if not path:
                return Intent(
                    "read_image_text",
                    note="Which image should I read? Give me its path.",
                )
            return Intent("read_image_text", tool="read_image_text", params={"path": path})

        # --- inspect --------------------------------------------------------
        if image or path:
            if not path:
                return Intent(
                    "inspect_image", note="Which image should I look at? Give me its path."
                )
            return Intent("inspect_image", tool="inspect_image", params={"path": path})

        return None

    def _design(self, text: str, raw: str) -> Optional[Intent]:
        """Design Agent (v1.2).

        Diagram, UI, documentation and presentation requests. Runs before the
        coding rule, since "design a …" and "document …" are distinct from
        repository work.
        """
        diagram = re.search(r"\b(diagram|architecture diagram|system diagram|draw)\b", text)
        flow = re.search(r"\b(flowchart|flow chart|process flow)\b", text)
        ui = re.search(r"\b(ui|ux|mockup|mock up|wireframe|screen|layout|dashboard design)\b", text)
        docs = re.search(r"\b(document\w*|readme|guide|write up the docs)\b", text)
        deck = re.search(r"\b(presentation|slides?|deck|slide deck)\b", text)
        assets = re.search(r"\b(design assets?|my assets?)\b", text)
        review = re.search(r"\b(analyze|review|critique)\b", text)

        if not (diagram or flow or ui or docs or deck or assets):
            return None

        def _title() -> str:
            """The request's subject, used as the artifact title.

            Simpler and more robust than keyword-specific capture: drop the
            leading imperative verb and any article, and keep the rest.
            "draw a diagram of the JARVIS pipeline" -> "diagram of the JARVIS
            pipeline"; "design a dashboard mockup" -> "dashboard mockup".
            """
            subject = re.sub(
                r"^\s*(?:please\s+)?(?:create|make|design|draw|write|build|generate|"
                r"mock up|produce|prepare)\s+", "", raw, count=1, flags=re.I
            )
            subject = re.sub(r"^(?:a|an|the)\s+", "", subject.strip(), flags=re.I)
            return subject.strip().rstrip(".")

        # --- review ---------------------------------------------------------
        if review and assets:
            asset_id = self._match(r"\b(ASSET-[0-9a-f]{6,})\b", raw)
            if asset_id:
                return Intent("analyze_design", tool="analyze_design", params={"asset_id": asset_id})
            return Intent(
                "analyze_design", note="Which asset should I analyze? Give me its id (ASSET-…)."
            )

        # --- list -----------------------------------------------------------
        if assets and re.search(r"\b(list|show|what)\b", text):
            return Intent("list_design_assets", tool="list_design_assets", params={})

        # --- deck ----------------------------------------------------------
        if deck:
            title = _title()
            if not title:
                return Intent("create_presentation", note="What should the presentation be called?")
            return Intent("create_presentation", tool="create_presentation", params={"title": title})

        # --- documentation -------------------------------------------------
        if docs:
            title = _title()
            if not title:
                return Intent("create_documentation", note="What should the document be called?")
            return Intent(
                "create_documentation", tool="create_documentation", params={"title": title}
            )

        # --- flow ----------------------------------------------------------
        if flow:
            title = _title()
            if not title:
                return Intent("create_flowchart", note="What should I call the flowchart?")
            return Intent("create_flowchart", tool="create_flowchart", params={"title": title})

        # --- ui ------------------------------------------------------------
        if ui:
            title = _title()
            if not title:
                return Intent("create_ui_concept", note="What should I call the concept?")
            return Intent("create_ui_concept", tool="create_ui_concept", params={"title": title})

        # --- architecture diagram -----------------------------------------
        title = _title()
        if not title:
            return Intent(
                "create_architecture_diagram", note="What should I call the diagram?"
            )
        return Intent(
            "create_architecture_diagram",
            tool="create_architecture_diagram",
            params={"title": title},
        )

    def _coding(self, text: str, raw: str) -> Optional[Intent]:
        """Coding Agent (v1.1).

        Runs before the file-search rule so "search the code for X" is treated as
        a code search rather than a file lookup, but the keywords are narrow
        enough that "find my report" still reaches file search.
        """
        coding_noun = re.search(
            r"\b(workspace|repository|repo|codebase|source tree|coding)\b", text
        )
        code_verb = re.search(
            r"\b(search|grep|find)\b.*\b(code|source|function|class)\b", text
        )
        test_verb = re.search(r"\b(run|execute)\b.*\b(tests?|test suite)\b", text)
        commit_verb = re.search(r"\bcommit\b", text)

        if not (coding_noun or code_verb or test_verb or commit_verb):
            return None

        # A workspace reference, which most coding actions need.
        workspace = self._match(r"\b(WS-[0-9a-f]{6,})\b", raw) or ""

        # --- commit ---------------------------------------------------------
        if commit_verb:
            # Take only what follows "message" -- otherwise the phrase before it
            # ("commit this with message …") ends up inside the message.
            message = self._match(r"\bmessages?\s+[\"']?(.+?)[\"']?$", raw)
            return Intent(
                "commit_workspace",
                tool="commit_workspace",
                params={
                    "workspace_id": workspace,
                    "message": (message or "").strip(),
                    # Must be explicitly authorised; the tool refuses otherwise.
                    "authorized": True,
                },
            )

        # --- tests ----------------------------------------------------------
        if test_verb:
            return Intent(
                "run_workspace_tests",
                tool="run_workspace_tests",
                params={"workspace_id": workspace, "runner": ""},
            )

        # --- search ---------------------------------------------------------
        if code_verb:
            query = self._match(
                r"\b(?:search|grep|find)\b\s+(?:the\s+)?(?:code|source)\s+(?:for\s+)?(.+)$", raw
            )
            if not query:
                return Intent("search_code", note="What should I search the code for?")
            return Intent(
                "search_code",
                tool="search_code",
                params={"workspace_id": workspace, "query": query.strip()},
            )

        # --- open -----------------------------------------------------------
        if re.search(r"\bopen\b", text) and re.search(r"\b(workspace|repo|repository)\b", text):
            path = self._match(r"(/[\w./-]+)", raw)
            if not path:
                return Intent(
                    "open_workspace", note="Which directory should I open as a workspace?"
                )
            return Intent("open_workspace", tool="open_workspace", params={"path": path})

        # --- inspect / report ----------------------------------------------
        if re.search(r"\b(report|summar\w*|overview)\b", text):
            return Intent(
                "coding_report", tool="coding_report", params={"workspace_id": workspace}
            )

        return Intent(
            "inspect_workspace",
            tool="inspect_workspace",
            params={"workspace_id": workspace},
        )

    def _device_management(self, text: str, raw: str) -> Optional[Intent]:
        """Pairing, listing and unpairing devices (v0.6)."""
        # Status is checked before listing: "what is my phone status" contains
        # both "what" and "phone", so the broader list pattern would swallow it.
        if re.search(rf"\b{_DEVICE_WORDS}\b.*\b(status|state|online|connected|battery)\b", text):
            return Intent(
                "get_device_status",
                tool="get_device_status",
                params={"target_device": "phone_main"},
            )

        if re.search(r"\b(how is|how's|status of|progress)\b.*\btransfer\b", text):
            return Intent("get_transfer_status", tool="get_transfer_status")

        if re.search(r"\b(cancel|stop|abort)\b.*\b(transfer|sending)\b", text):
            return Intent("cancel_transfer", note="Which transfer should I cancel?")

        if re.search(rf"\b(unpair|remove|forget|revoke)\b.*\b{_DEVICE_WORDS}\b", text):
            return Intent(
                "remove_phone",
                tool="remove_phone",
                params={"target_device": "phone_main"},
            )

        if re.search(rf"\b(pair|connect|link)\b.*\b{_DEVICE_WORDS}\b", text) or re.search(
            r"\b(pair|connect)\b.*\b(new device|device)\b", text
        ):
            return Intent("register_phone", tool="register_phone")

        if re.search(r"\b(list|show)\b.*\b(devices|phones|paired)\b", text) or re.search(
            r"\bwhat\b.*\bdevices\b", text
        ):
            return Intent("list_devices", tool="list_devices")

        return None

    # -- scheduler -----------------------------------------------------------
    def _scheduler(self, text: str, raw: str) -> Optional[Intent]:
        """Recurring jobs and the daily briefing (v0.8).

        Runs before the calendar rule: "remind me at 8 PM" is a schedule, not a
        calendar read, but both share the word "remind".
        """
        # --- the briefing itself ------------------------------------------
        # "every"/"each" makes it a schedule; "daily briefing" on its own is a
        # request to produce one now.
        if re.search(r"\b(briefing|brief me|catch me up)\b", text) and not re.search(
            r"\bevery\b|\beach\b", text
        ):
            return Intent("daily_briefing", tool="daily_briefing", params={})

        # --- create a schedule ---------------------------------------------
        # "schedule two hours for project work tomorrow" is a request to *make*
        # one. The word "schedule" alone used to fall into the manage branch
        # above, so it listed the (empty) schedule set instead, answered
        # "You have nothing scheduled", and recorded success -- measured in the
        # Phase 15 acceptance sweep, the user was shown a VERIFIED chip for a
        # block that was never created.
        create_requested = bool(
            re.match(r"(?:can you |could you |please )?schedule\s+\S", text)
        )

        # --- manage existing schedules -------------------------------------
        if not create_requested and (
            re.search(r"\b(scheduled|schedules?|recurring|repeating)\b", text)
            or re.search(r"\bwhat.*(scheduled|recurring)\b", text)
        ):
            if re.search(r"\b(cancel|delete|remove|stop)\b", text):
                return Intent(
                    "cancel_schedule",
                    tool="cancel_schedule",
                    params={"schedule_id": self._match(r"\b([A-Z]+-[0-9a-f]{6,})", raw) or ""},
                )
            if re.search(r"\bpause\b", text):
                return Intent(
                    "pause_schedule",
                    tool="pause_schedule",
                    params={"schedule_id": self._match(r"\b([A-Z]+-[0-9a-f]{6,})", raw) or ""},
                )
            if re.search(r"\b(resume|unpause|restart)\b", text):
                return Intent(
                    "resume_schedule",
                    tool="resume_schedule",
                    params={"schedule_id": self._match(r"\b([A-Z]+-[0-9a-f]{6,})", raw) or ""},
                )
            return Intent("list_schedules", tool="list_schedules", params={})

        # --- create a schedule ---------------------------------------------
        recurring = re.search(r"\b(every|each|daily|weekly)\b", text)
        reminder = re.search(r"\bremind me\b", text)

        if not (recurring or reminder or create_requested):
            return None

        # The schedule phrase itself. Bounded deliberately: an unbounded match
        # would swallow the rest of the sentence ("every morning give me my
        # daily briefing" must yield "every morning", not the whole clause).
        _CADENCE = (
            r"(?:day|morning|evening|night|afternoon|weekday|week|"
            r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
            r"mon|tue|wed|thu|fri|sat|sun|\d+\s*(?:minutes?|mins?|hours?|hrs?))"
        )
        _AT = r"(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)?"
        schedule_text = self._match(
            rf"\b((?:every|each)\s+{_CADENCE}{_AT})", raw
        )
        if not schedule_text:
            schedule_text = self._match(r"\b(at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", raw)

        if not schedule_text:
            return Intent(
                "schedule_task",
                note=(
                    "When should I do that? For example: 'every morning' or "
                    "'at 8 PM'."
                ),
            )

        # What should it actually do?
        action, params = "reminder", {}
        if re.search(r"\b(briefing|brief me)\b", text):
            action = "briefing"
        elif re.search(r"\bproject (progress|status|summary)\b", text):
            action = "project_summary"
        elif re.search(r"\bcalendar\b", text):
            action = "list_events"
            params = {"date": "today"}
        elif reminder:
            message = self._match(r"\bremind me\b.*?\bto\s+(.+)$", raw)
            if message:
                params = {"message": message.strip()}

        if create_requested and action == "reminder" and not params.get("message"):
            # This phrasing used to be answered by listing schedules, so it never
            # reached a create at all. Now that it does, do not file a job whose
            # content was invented -- ask what the time is for.
            return Intent(
                "schedule_task",
                note=(
                    "What should I put in that time? For example: 'remind me to write "
                    "the AMR review at 3 PM', or 'every morning give me my briefing'."
                ),
            )

        name = action
        if action == "reminder" and params.get("message"):
            name = params["message"][:60]

        return Intent(
            "schedule_task",
            tool="schedule_task",
            params={
                "name": name,
                "schedule": schedule_text.strip(),
                "action": action,
                "params": params,
            },
        )

    def _email(self, text: str, raw: str) -> Optional[Intent]:
        """Mailbox actions (v0.7). Sending is never inferred -- only explicit."""
        # "send it" only means email when a draft is actually waiting. Checked
        # before the keyword guard, since the phrase itself names no medium.
        if re.fullmatch(r"(?:please\s+)?(?:send|fire off)\s+(?:it|this|that|the draft)[.!]?", text):
            from ..tools.email import latest_draft

            pending = latest_draft()
            if pending is None:
                return Intent(
                    "send_email",
                    note=(
                        "There is no draft waiting to send. Draft one first, for "
                        'example: "draft an email to someone@example.com".'
                    ),
                )
            return Intent("send_email", tool="send_email", params={"draft_id": pending["draft_id"]})

        has_email_noun = re.search(r"\b(e-?mails?|mail|inbox|unread)\b", text)
        # "send to Arun" names a verb and a recipient and nothing else. That is a
        # missing payload, which the plan's safety list calls Clarify -- not an
        # unrecognised sentence, and never a guess at what to send.
        bare_to = self._match(r"\b(?:send|forward|dispatch)\s+to\s+([A-Za-z][A-Za-z' .-]{1,40}?)\s*[.!]?$", raw)
        if bare_to and not has_email_noun:
            who = re.sub(r"^(?:my|our|the)\s+", "", bare_to, flags=re.I).strip()
            if who.split()[0].lower() not in {
                "phone", "laptop", "watch", "tablet", "it", "this", "that", "him", "her", "them",
            }:
                return Intent(
                    "send_email",
                    note=(
                        f"Send what to {who}, and by which medium? Draft it and I will "
                        f'ask before sending: "send an email to {who} saying ...".'
                    ),
                )

        # "write a reply to x@y.com" names no medium, but a drafting verb plus
        # "reply" is a mail request in practice.
        has_reply_intent = "reply" in text and re.search(r"\b(draft|compose|write)\b", text)
        if not (has_email_noun or has_reply_intent):
            return None

        # --- draft --------------------------------------------------------
        # Checked before "send": "draft an email to x@y.com" contains the word
        # "email to x@y.com", which a naive send pattern would claim.
        draft_to = self._match(
            r"\b(?:draft|compose|write|prepare)\b\s+(?:an?\s+)?(?:email|mail|message|reply)?\s*"
            r"(?:to\s+)?(.+)$",
            raw,
        )
        if draft_to and re.search(r"\b(draft|compose|write|reply)\b", text):
            target = self._arg(re.sub(r"^(?:a|an|the)\s+", "", draft_to, flags=re.I))
            # Strip a possessive: "to my teammate" names the contact "teammate".
            target = re.sub(r"^(?:my|our|the)\s+", "", target, flags=re.I).strip()
            if target:
                return Intent(
                    "draft_email",
                    tool="draft_email",
                    params={"to": target, "subject": "", "body": ""},
                )

        # "send an email to Priya saying the build is green" carries a recipient and
        # a body, so it is a drafting request: the sending verb alone never reaches
        # send_email, which still needs an explicit address (below) and a
        # confirmation. Phrased with a person instead of an address, the honest next
        # step is the draft, and the contact lookup says plainly what it could not resolve.
        send_phrased = re.search(
            r"\b(?:send|fire off|forward)\b[^.]*?\bto\s+(?!\S+@)([A-Za-z][A-Za-z' .-]{1,40}?)\s+"
            r"\b(?:saying|that say|with the subject)\b\s+(.+)$",
            raw,
            re.I,
        )
        if send_phrased:
            who = re.sub(r"^(?:my|our|the)\s+", "", send_phrased.group(1), flags=re.I).strip()
            return Intent(
                "draft_email",
                tool="draft_email",
                params={
                    "to": self._arg(who),
                    "subject": "",
                    "body": self._arg(send_phrased.group(2)),
                },
            )

        # --- send ---------------------------------------------------------
        # Requires an explicit sending verb. Merely naming an address does not
        # authorise sending to it.
        send_match = self._match(
            r"\b(?:send|fire off|dispatch)\b\s+(?:an?\s+)?(?:email|mail|message)?\s*"
            r"(?:to\s+)?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
            raw,
        )
        if send_match:
            return Intent(
                "send_email",
                tool="send_email",
                params={"to": send_match},
                confidence=0.85,
            )

        # "send the email" with a draft waiting.
        if re.search(r"\b(send|fire off|dispatch)\b", text) and re.search(
            r"\b(it|this|that|the (email|mail|message|draft)|my (reply|response))\b", text
        ):
            from ..tools.email import latest_draft

            pending = latest_draft()
            if pending is not None:
                return Intent(
                    "send_email", tool="send_email", params={"draft_id": pending["draft_id"]}
                )

        # --- summarize ----------------------------------------------------
        if re.search(r"\b(summar\w*|catch me up|brief me)\b", text):
            return Intent("summarize_email", tool="summarize_email", params={"limit": 5})

        # --- unread / search ----------------------------------------------
        if re.search(r"\b(unread|new|important)\b", text):
            return Intent(
                "search_email",
                tool="search_email",
                params={"query": "", "unread_only": True, "limit": 10},
            )

        from_match = self._match(r"\bfrom\s+([A-Za-z][A-Za-z0-9 ._-]{1,40})$", raw)
        if from_match:
            return Intent(
                "search_email",
                tool="search_email",
                params={"query": from_match.strip(), "limit": 10},
            )

        if re.search(r"\b(check|read|show|any|list)\b", text):
            return Intent("search_email", tool="search_email", params={"query": "", "limit": 10})

        # An email-ish request we do not understand still deserves an answer.
        return Intent(
            "email",
            note=(
                "I can search your mail, read a message, summarise recent ones, draft "
                'a reply, or send something you have already drafted. For example: '
                '"check my unread emails" or "draft an email to someone@example.com".'
            ),
        )

    # -- calendar ------------------------------------------------------------
    _WEEKDAYS = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }

    def _parse_when(self, raw: str) -> Optional[str]:
        """Parse a natural time phrase into an ISO string.

        Returns None when it cannot be understood, so the caller asks rather
        than inventing a time.
        """
        from datetime import datetime, timedelta

        text = raw.lower()
        now = datetime.now().astimezone()
        base = None

        if "tomorrow" in text:
            base = now + timedelta(days=1)
        elif "today" in text or "tonight" in text:
            base = now
        elif "day after tomorrow" in text:
            base = now + timedelta(days=2)
        else:
            for name, index in self._WEEKDAYS.items():
                if re.search(rf"\b{name}\b", text):
                    ahead = (index - now.weekday()) % 7 or 7
                    base = now + timedelta(days=ahead)
                    break

        if base is None:
            explicit = self._match(r"(\d{4}-\d{2}-\d{2})", raw)
            if explicit:
                try:
                    base = datetime.fromisoformat(explicit).replace(tzinfo=now.tzinfo)
                except ValueError:
                    return None

        if base is None:
            return None

        hour, minute = 9, 0  # a sensible default when no time is given
        # NB: use re.search directly -- ``_match`` returns a *string*, not a
        # match object, so indexing it would read characters.
        clock = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", raw, re.I)
        if clock:
            hour = int(clock.group(1))
            minute = int(clock.group(2) or 0)
            meridiem = (clock.group(3) or "").replace(".", "").lower()
            if meridiem == "pm" and hour < 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
            elif not meridiem and hour <= 7:
                # "at 5" in the evening reads as 17:00.
                hour += 12

        if hour > 23 or minute > 59:
            return None
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()

    def _calendar(self, text: str, raw: str) -> Optional[Intent]:
        """Calendar actions (v0.7)."""
        calendar_words = re.search(
            r"\b(calendar|schedule|meeting|event|appointment|free time|availability|"
            r"plan my day|remind|book)\b",
            text,
        )
        # "what do I have tomorrow" names no calendar noun, but a time word plus
        # a question is a schedule request in practice.
        schedule_question = re.search(
            r"\b(what|anything|what's|whats)\b.*\b(have|on|scheduled|planned)\b", text
        ) and re.search(r"\b(today|tomorrow|tonight|this week|next week|morning|afternoon)\b", text)

        if not calendar_words and not schedule_question:
            return None

        # --- create -------------------------------------------------------
        create = re.search(r"\b(schedule|book|create|add|set up|arrange)\b", text)
        if create and not re.search(r"\b(find|when|what|show|list|do i have)\b", text):
            title = self._match(
                r"\b(?:schedule|book|create|add|set up|arrange)\b\s+(?:an?\s+)?(?:meeting|event|appointment)?\s*(.*)",
                raw,
            )
            when = self._parse_when(raw)
            title = re.sub(
                r"\b(today|tomorrow|tonight|on \d{4}-\d{2}-\d{2})\b.*$", "", title or "", flags=re.I
            )
            title = re.sub(r"\s+at\s+\d{1,2}(:\d{2})?\s*(am|pm)?.*$", "", title, flags=re.I)
            title = self._arg(title) or "Meeting"
            if when is None:
                return Intent(
                    "create_event",
                    note=(
                        "When should I schedule that? For example: "
                        '"schedule a project meeting tomorrow at 5 PM".'
                    ),
                )
            return Intent(
                "create_event",
                tool="create_event",
                params={"title": title, "start": when},
            )

        # --- free time ----------------------------------------------------
        if re.search(r"\b(free time|availability|free slot|when am i free)\b", text):
            date = "tomorrow" if "tomorrow" in text else "today"
            return Intent(
                "find_free_time",
                tool="find_free_time",
                params={"date": date, "duration_minutes": 60},
            )

        # --- next meeting -------------------------------------------------
        if re.search(r"\b(next meeting|next event|next appointment|when is my next)\b", text):
            return Intent("next_event", tool="next_event", params={})

        # --- plan my day --------------------------------------------------
        if re.search(r"\bplan my day\b", text):
            date = "tomorrow" if "tomorrow" in text else "today"
            return Intent("daily_schedule", tool="daily_schedule", params={"date": date})

        # --- list / what do I have ---------------------------------------
        if re.search(r"\b(what|anything|show|list|summarize|read)\b", text):
            date = "tomorrow" if "tomorrow" in text else ("yesterday" if "yesterday" in text else "today")
            return Intent("list_events", tool="list_events", params={"date": date, "days": 1})

        return Intent(
            "calendar",
            note=(
                "I can read your schedule, find free time, and create or cancel "
                'events. For example: "what do I have tomorrow" or '
                '"schedule a project meeting tomorrow at 5 PM".'
            ),
        )

    def _home(self, text: str, raw: str) -> Optional[Intent]:
        # --- routines ------------------------------------------------------
        routine = self._match_home_routine(text)
        if routine:
            return Intent("home_routine", tool="run_home_routine", params={"routine": routine})

        # --- status / overview --------------------------------------------
        if re.search(
            r"\b(home status|house status|home overview|how['\u2019]?s the house|how is the house|"
            r"is anyone home|what['\u2019]?s (on|in) (the )?(home|house))\b",
            text,
        ):
            return Intent("home_status", tool="get_home_status")

        # --- control (turn / dim / set a device) ---------------------------
        m = re.search(
            r"\b(?P<cmd>turn on|turn off|switch on|switch off|dim|set|raise|lower)\b"
            r"\s+(?:the\s+)?(?P<dev>(?:[a-z0-9]+[ ])*?(?:lights?|fans?|plugs?|lamps?|ac|aircon|"
            r"heater|temperature|doors?|cameras?|sensors?))"
            r"(?:\s+(?:to|at)\s+(?P<lvl>\d{1,3})(?:\s*(?:percent|%))?)?",
            text,
        )
        if m:
            verb = m.group("cmd")
            if verb in ("turn off", "switch off"):
                action = "off"
            elif verb in ("turn on", "switch on"):
                action = "on"
            else:
                action = "set"
            dev = m.group("dev").strip()
            if "temperature" in dev or dev in ("ac", "aircon", "heater"):
                dev = "ac"
            level = int(m.group("lvl")) if m.group("lvl") else None
            from ..home.gateway import get_home_gateway

            tied = get_home_gateway().ambiguous_matches(dev)
            if tied:
                # The user named a category, not a device. Choosing one and
                # actuating it was the old behaviour: "turn on the light" reached
                # the kitchen light out of four, and the answer never said it had
                # chosen. A question costs one turn; a wrong actuation costs trust.
                return Intent(
                    "home_control",
                    note=(
                        f"Which {dev} did you mean -- "
                        + ", ".join(d.name for d in tied)
                        + "? Name the room and I will switch it."
                    ),
                )
            return Intent(
                "home_control",
                tool="control_home_device",
                params={"device": dev, "action": action, "value": level},
            )

        # --- temperature / sensor reading ----------------------------------
        if re.search(r"\btemperature\b", text) or re.search(r"\bhow (warm|cold) is it\b", text):
            return Intent("home_reading", tool="get_home_reading", params={"room": self._match_home_room(text)})

        # --- list devices (needs an explicit home/house keyword) -----------
        if re.search(r"\b(list|show)\b[^.]{0,14}\b(home|house)\b[^.]{0,14}\bdevices\b", text):
            return Intent("home_list", tool="list_home_devices")

        return None

    @staticmethod
    def _match_home_routine(text: str) -> Optional[str]:
        m = re.search(r"\b(study|sleep|movie|project)\s+mode\b|\bleaving home\b|\bemergency mode\b", text)
        if not m:
            return None
        matched = m.group(0)
        if matched == "leaving home":
            return "leaving"
        if matched == "emergency mode":
            return "emergency"
        return matched.split()[0]

    @staticmethod
    def _match_home_room(text: str) -> str:
        m = re.search(r"\b(?:in|of)\s+(?:the|my)\s+([a-z ]+)", text)
        if not m:
            return ""
        stop = {"the", "a", "an", "and", "today", "right", "now", "currently"}
        kept: List[str] = []
        for word in m.group(1).split():
            if word in stop:
                break
            kept.append(word)
            if len(kept) >= 2:
                break
        room = " ".join(kept).strip()
        return "" if room in ("house", "home", "inside") else room

    def _robotics(self, text: str, raw: str) -> Optional[Intent]:
        """        Robot / sensor-fleet intents (v1.6).

        Ordered between ``_home`` and ``_run_command`` so that "run today's
        experiment" is a robot routine rather than a shell command. The rule only
        claims a phrase when a robot keyword (amr / arm / gripper / experiment) is
        present. A robot *motion* phrase ("move the amr to (1, 2)") is kept out of
        the file-move rule: ``_copy_move`` detects a coordinate destination or a
        short robot reference and defers, letting it reach this rule. The global
        "stop ... robot/amr/arm/experiment" is owned by the emergency-stop rule,
        which runs earlier.
        """
        has_robot = bool(re.search(r"\b(amr|amrs|robot|robots|robot arm|\barm\b|gripper|joints)\b", text))
        has_experiment = bool(re.search(r"\bexperiments?\b", text))

        # A robot's battery / charge level is telemetry, never laptop battery.
        if has_robot and re.search(r"\bbattery\b|\bpower level\b|\bcharge level\b", text):
            return Intent(
                "robot_telemetry",
                tool="get_robot_telemetry",
                params={"robot": self._robot_ref(text)},
                confidence=0.9,
            )

        # "run / start the ... experiment" -> a named experiment routine.
        if has_experiment and re.search(r"\b(run|start|launch|execute)\b", text):
            return Intent(
                "run_experiment",
                tool="run_experiment",
                params={"experiment": self._experiment_ref(text)},
                confidence=0.8,
            )

        if not has_robot:
            return None

        # Status / overview / the whole fleet.
        if re.search(r"\b(status|state|overview|how (is|are) (the|my)|what (is|are))\b", text) \
                or re.search(r"\b(list|show)\b[^.]{0,14}\b(robots|fleet|arms)\b", text):
            if re.search(r"\b(list|show|fleet|all|both|how many)\b", text):
                return Intent("list_robots", tool="list_robots", confidence=0.85)
            return Intent("robot_status", tool="get_robot_status", confidence=0.85)

        # Telemetry: pose, position, joints, speed, map.
        if re.search(
            r"\b(telemetry|pose|position|location|where|coordinates|joint|joints|angle|angles|speed|odometer|map)\b",
            text,
        ):
            return Intent(
                "robot_telemetry",
                tool="get_robot_telemetry",
                params={"robot": self._robot_ref(text)},
                confidence=0.85,
            )

        # Motion / control (never "stop" -- that is the emergency-stop rule).
        action = self._motion_ref(text)
        if action is not None:
            return Intent(
                "robot_motion",
                tool="command_robot_motion",
                params={"robot": self._robot_ref(text) or "amr_main", "action": action[0], **action[1]},
                confidence=0.75,
            )

        # A robot is named without a clear verb -> report its status.
        return Intent("robot_status", tool="get_robot_status", confidence=0.6)

    @staticmethod
    def _robot_ref(text: str) -> str:
        """Best-guess robot id from a phrase, or "" when unclear."""
        if re.search(r"\bamr\b", text):
            return "amr_main"
        if re.search(r"\b(arm|gripper|joints)\b", text) and not re.search(r"\barmy\b", text):
            return "arm_01"
        if re.search(r"\brobots?\b", text):
            return "amr_main"
        return ""

    @staticmethod
    def _experiment_ref(text: str) -> str:
        """Map a phrase to a known experiment routine (map_lab is the default)."""
        if re.search(r"\bpick\b|\bplace\b|\bpick ?up\b", text):
            return "pick_place"
        if re.search(r"\bdock\b|\bcharging?\b", text):
            return "dock_charge"
        return "map_lab"

    @staticmethod
    def _motion_ref(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        if re.search(r"\bgripper\b", text):
            open_ = bool(re.search(r"\bopen\b", text))
            if open_ or re.search(r"\b(close|clasp|grasp)\b", text):
                return ("gripper", {"gripper": "open" if open_ else "close"})
        if re.search(r"\b(charge|charging|dock)\b", text):
            return ("charge", {})
        if re.search(r"\b(home|park)\b", text):
            return ("home", {})
        if re.search(r"\b(move|drive|go|navigate|send|take)\b", text):
            params: Dict[str, Any] = {}
            pose = RulePlanner._parse_pose(text)
            if pose:
                params["pose"] = pose
            speed = RulePlanner._parse_speed(text)
            if speed is not None:
                params["speed"] = speed
            return ("move", params)
        return None

    @staticmethod
    def _parse_pose(text: str) -> Optional[Dict[str, float]]:
        m = re.search(r"\(([-\d.]+)\s*,\s*([-\d.]+)\)", text)
        if m:
            return {"x": float(m.group(1)), "y": float(m.group(2)), "theta": 0.0}
        m = re.search(r"x=(-?[\d.]+)\D+y=(-?[\d.]+)", text)
        if m:
            return {"x": float(m.group(1)), "y": float(m.group(2)), "theta": 0.0}
        return None

    @staticmethod
    def _parse_speed(text: str) -> Optional[float]:
        m = re.search(r"\bat\s+([\d.]+)\s*m/s\b", text)
        if not m:
            m = re.search(r"\bspeed\s+(?:of\s+)?([\d.]+)", text)
        return float(m.group(1)) if m else None

    # -- laptop control center ---------------------------------------------
    def _laptop_center_prompt(self, text: str, raw: str) -> Optional[Intent]:
        """Agent-prompt builder (v2.x): "build me an agent prompt to <goal>".

        Registered *before* ``_coding`` so phrasings like "handoff prompt for
        the coding agent" reach the cockpit instead of the workspace tools.
        """
        if not re.search(
            r"\b(?:agent prompt|hand ?off prompt|handoff|prompt for (?:an? |the )?(?:coding )?agent)\b",
            text,
        ):
            return None
        goal = self._match(
            r"\b(?:agent prompt|hand ?off prompt|handoff|"
            r"prompt for (?:an? |the )?(?:coding )?agent)\b"
            r"\s*(?:to|for|that|:|-)?\s*(.*)",
            raw,
        )
        goal = (goal or "").strip()
        goal = re.sub(r"^(?:to|for|that)\s+", "", goal, flags=re.I).strip()
        goal = goal.strip(" .,:-").strip()
        return Intent(
            "laptop_center_agent_prompt",
            {"goal": goal} if goal else {},
            confidence=0.85,
            tool="build_agent_prompt" if goal else None,
            note=(
                f"Agent prompt for: {goal}"
                if goal
                else "Tell me the goal -- what should the agent do? -- and I'll build the prompt."
            ),
        )

    def _laptop_center(self, text: str, raw: str) -> Optional[Intent]:
        """Laptop Control Center (v2.x): condition, processes, projects.

        Runs after every device/coding rule and just before the ``run``
        catch-all, and only claims deliberately specific phrasings so it never
        steals an intent that an earlier, more specific rule owns (singular
        "project status" stays with ``_project``; "workspace …" stays with
        ``_coding``; "what apps …" stays with ``_list_apps``).
        """
        # 1) Process list: "what's running", "top processes", "process list".
        if re.search(
            r"\b(?:what.s running|running processes?|"
            r"top (?:processes?|apps?)|process (?:list|status|table)|"
            r"list (?:the )?(?:running )?processes?|which processes? (?:are )?running)\b",
            text,
        ):
            params: Dict[str, Any] = {}
            sort = self._match(r"\b(?:sorted? by|by) (cpu|memory|mem)\b", text)
            if sort:
                params["sort_by"] = "mem" if sort.lower() in ("memory", "mem") else "cpu"
            return Intent(
                "laptop_center_processes",
                params,
                confidence=0.8,
                tool="laptop_processes",
            )

        # 2) Open-project status. Singular "project status" is owned by
        #    ``_project`` (it runs earlier); this claims the multi-project and
        #    "open projects" phrasings that rule does not reach.
        if re.search(
            r"\b(?:projects? (?:status|state)|status of my (?:open )?projects?|"
            r"my open projects?|where (?:are|is) my (?:open )?projects?)\b",
            text,
        ):
            return Intent(
                "laptop_center_project_status",
                {},
                confidence=0.8,
                tool="laptop_project_status",
            )

        # 3) Machine-condition snapshot: "laptop condition", "how's my laptop".
        if re.search(
            r"\b(?:laptop (?:condition|health|state|check|stats|diagnostics)|"
            r"how.s (?:my )?laptop|check (?:my )?laptop|"
            r"machine (?:condition|health|state)|system snapshot)\b",
            text,
        ):
            return Intent(
                "laptop_center_snapshot",
                {},
                confidence=0.8,
                tool="laptop_snapshot",
            )

        return None

    def _run_command(self, text: str, raw: str) -> Optional[Intent]:
        match = re.search(r"\brun\b\s+([A-Za-z0-9_-]+)\s*(.*)", raw, re.I)
        if not match:
            return None
        return Intent(
            "run_command",
            tool="run_allowlisted_command",
            params={"command": match.group(1).lower(), "args": match.group(2).strip()},
            confidence=0.7,
        )
