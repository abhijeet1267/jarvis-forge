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
from .models import Plan, PlannedAction, RiskLevel

log = get_logger("core.planner")

_WAKE = re.compile(r"^\s*(hey\s+|ok\s+|okay\s+)?(jarvis|vantage)[,.\s]+", re.I)
_WS = re.compile(r"\s+")

#: The only sequence markers this planner will decompose on. A bare "then" is
#: deliberately excluded -- it appears inside the content a user asks to be
#: written ("containing: first line then second line"), and splitting there would
#: both truncate that content and invent a step nobody asked for. So a sequence
#: has to be joined with an explicit conjunction.
_SEQUENCE = re.compile(r"\s+and\s+then\s+|\s*,\s*and\s+then\s+|\s*,\s+then\s+", re.I)

#: An erase verb acting on the storage itself, rather than on a file the person names. Found by
#: the Phase 69 corpus: "empty the trash and wipe free space" matched the `free space` trigger in
#: `_storage` and ran `check_storage`, answering two destructive requests with a disk report. The
#: three-word window keeps "delete some photos" a read while "clean up my disk" stays a refusal.
_ERASE_REQUEST = re.compile(
    r"\b(empty|wipe|erase|shred|purge|clear|delete|remove|clean)\b(?:\s+\w+){0,3}\s*"
    r"\b(trash|recycle\s+bin|bin|free\s+space|disk|drive)\b", re.I)

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


def n8n_mod_tool_name(key: str) -> str:
    """The tool name an n8n registry key registers under."""
    return f"n8n__{key}"

@dataclass
class Intent:
    """A parsed intent before it becomes a plan."""

    name: str
    params: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.8
    note: Optional[str] = None
    tool: Optional[str] = None
    #: Identifiers the sentence did not supply for the tool this route wanted to call.
    #: Carried through to the plan so the orchestrator can look the candidates up in the
    #: registry rather than making the user recite an id (Phase 33).
    missing_identifiers: Tuple[str, ...] = ()
    #: The thing the sentence named, if it named one. "cancel my Design Review event"
    #: names *Design Review*; an identifier lookup needs that name to find the row.
    named_thing: str = ""
    #: Is this note a question the user can answer to proceed? ``None`` means "read
    #: it from the wording", which is safe here because the wording being read is the
    #: route's own sentence, not the user's punctuation. Set it to ``False`` where a
    #: note states something unavailable instead of asking for something missing.
    clarification: Optional[bool] = None
    #: The plan's reply kind, when the route knows it.
    #:
    #: A note-only intent normally becomes a `declined` plan, which the
    #: orchestrator answers with the note and nothing else. A route that is
    #: saying "the rules did not find an action for this, but the model
    #: might" says so here, and the plan becomes `not_understood` -- the
    #: kind the orchestrator routes to the model, first for an answer and
    #: then for a proposal.
    reply_kind: Optional[str] = None
    #: A demonstrative ("read that one", "summarize the second one") points
    #: at the message the conversation selected. Carried to the orchestrator,
    #: which holds the per-session selection and turns it into a real action.
    #: ``None`` (default) means the sentence named its own target; ``""`` means
    #: the selected one; an ordinal word/number means a pick from the last list.
    referent: Optional[str] = None
    #: The body of a reply the sentence already carries ("reply to that one
    #: saying thanks"). Goes into the draft, never invented.
    referent_body: str = ""


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


_INTERROGATIVE_STARTS = (
    "what ", "which ", "who ", "whom ", "whose ", "where ", "when ", "how ",
    "why ", "would you ", "should i ", "can you ", "could you ", "do you want",
)


def _asks_for_something(note: str) -> bool:
    """Whether a route's own note is a question rather than a statement.

    Phase 19 left this split undone because classifying *the user's* sentence looked like
    guesswork. This is not that: the string is the note the route wrote for itself, so its
    shape is a property of the code, not an inference about a person. A route whose note is
    a statement -- "I cannot read that device's battery: no phone is paired" -- says so by
    setting ``Intent(clarification=False)`` rather than relying on this.
    """
    text = (note or "").strip()
    if not text:
        return False
    if text.endswith("?"):
        return True
    return text.lower().startswith(_INTERROGATIVE_STARTS)


class RulePlanner:
    """Deterministic natural-language -> plan mapper."""

    def __init__(self, tools: ToolRegistry, apps: Optional[AppRegistry] = None) -> None:
        self.tools = tools
        self.apps = apps or AppRegistry()
        self._rules: List[RuleFn] = [
            # First, because the sentence says "workflow": a rule that shares a
            # word with it ("daily agenda", "repository maintenance") would
            # otherwise claim it and answer a different question. The rule is
            # narrow enough -- it needs the word workflow/n8n/automation -- that
            # it cannot steal a sentence that is not about automation.
            self._automation,
            self._emergency_stop,
            self._help,
            self._vantage_self,
            self._list_apps,
            self._frontmost_app,
            self._screenshot,
            self._car,
            self._location,
            self._watch,
            self._battery,
            self._storage,
            self._machine_snapshot,
            self._system_status,
            self._agent_status,
            self._time,
            self._memory_controls,
            self._recall,
            self._remember,
            self._explain_failure,
            self._coding_agent,
            self._project,
            self._list_directory,
            self._compress,
            # Ahead of the file rules: "delete the task to call Sam" names a
            # planner object, and `_delete` would otherwise propose deleting a
            # file called "the task to call Sam".
            self._cancel_task,
            self._delete,
            self._begin_work,
            self._running_apps,
            self._review_downloads,
            self._reschedule_unfinished,
            # Ahead of the file rules: "copy hello to the clipboard" was read as a
            # copy_file from a file called "hello".
            self._clipboard,
            self._copy_move,
            self._write_file,
            self._append_file,
            self._close_app,
            self._find_and_open,
            self._open_standard_folder,
            self._open_url,
            self._page_actions,
            self._read_page,
            self._focus_app,
            self._open_app,
            self._research,
            self._vision,
            self._read_file,
            self._web_search,
            self._search_files,
            self._design,
            self._laptop_center_prompt,
            self._schedule_coding_block,
            self._coding,
            self._device_management,
            self._transfer,
            self._task_from_email,
            # A sentence carrying a cadence is a *schedule*, and the scheduler must
            # see it before any single-action rule claims the verb inside it. "every
            # morning at 8 check the battery" was answering the battery question now
            # and silently dropping the "every morning".
            self._scheduler,
            self._planner,
            self._email,
            self._availability,
            self._calendar,
            self._home,
            self._robotics,
            self._laptop_center,
            self._run_command,
            self._smalltalk,
            self._ask_question,
            self._single_word_capability,
            self._capability_fallback,
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
        if intent is None:
            # Phase 70: a second read for wording the chain matched nowhere -- a transcription
            # comma, a one-edit typo -- bounded to capabilities a tidy sentence could already reach
            # unaided at READ/LOW tier. Nothing here runs when the first read already found something.
            intent = self._tolerant_intent(lowered, raw)
        plan = self._to_plan(text, intent) if intent else self._unrecognised(text)
        plan.unhandled = dropped
        return plan

    def _unrecognised(self, text: str) -> Plan:
        return Plan(
            goal=text,
            intent="unknown",
            reply_kind="not_understood",
            direct_reply=(
                "I understood the words but not the goal yet. With an AI provider "
                "configured (Settings → AI Providers) I can answer any question "
                "conversationally. Without one, things I can do right now include: "
                '"what is a train", "open github.com", "check storage", '
                '"open my project", "what is my battery", "find my report", '
                '"remember that ...".'
            ),
            planner="rules",
        )

    _CADENCE = re.compile(
        r"\b(?:every|each)\s+(?:\d+\s*)?(?:day|morning|evening|night|afternoon|weekday|"
        r"week|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"\d+\s*(?:minutes?|mins?|hours?|hrs?))\b", re.I)

    def _first_intent(self, text: str, raw: str) -> Optional[Intent]:
        """Run the rule chain and return the first intent that matches."""
        # A cadence anywhere in the sentence makes it a schedule. Answered before
        # the chain, because several single-action rules ("check the battery",
        # "show my running applications") match the verb inside a scheduled
        # sentence and would answer *now*, silently dropping the "every morning".
        if self._CADENCE.search(raw):
            scheduled = self._scheduler(text, raw)
            if scheduled is not None and scheduled.tool:
                return scheduled

        for rule in self._rules:
            try:
                intent = rule(text, raw)
            except Exception as exc:  # pragma: no cover - defensive
                # Named, and at warning: a rule that crashes every single call is
                # indistinguishable from one that simply does not match, and the
                # answer looks like "I understood the words but not the goal".
                # Phase 28 found its own bug this way -- a broken append route
                # surfaced as an unrouted sentence, because the failure was
                # logged at info under a default-quiet logger.
                log.warning(
                    "planner rule raised",
                    extra_fields={"rule": getattr(rule, "__name__", str(rule)),
                                  "error": f"{type(exc).__name__}: {exc}"},
                )
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
            ask = intent.clarification if intent.clarification is not None \
                else _asks_for_something(intent.note)
            plan.direct_reply = intent.note
            plan.missing_identifiers = list(intent.missing_identifiers)
            plan.named_thing = intent.named_thing
            plan.referent = intent.referent
            plan.referent_body = intent.referent_body
            if intent.reply_kind:
                # The route knows what kind of note this is -- a request the
                # model may be able to act on, rather than a refusal.
                plan.reply_kind = intent.reply_kind
                if intent.reply_kind == "not_understood":
                    plan.needs_clarification = False
            elif ask:
                plan.reply_kind = "clarification"
                plan.needs_clarification = True
                plan.clarification_question = intent.note
            elif intent.name == "smalltalk":
                # A greeting is neither a refusal nor a question — it is the one note
                # whose whole point is that the user is being conversational. Mark it
                # "smalltalk" (which the orchestrator routes to the configured model) so
                # a plain "hello" is answered conversationally when a model is available,
                # instead of the hardcoded "add an API key" line. Before this, the
                # fallthrough labelled it "declined", which is not in the orchestrator's
                # conversational set and so never reached the model.
                plan.reply_kind = "smalltalk"
            else:
                plan.reply_kind = "declined"
            return plan

        if not intent.tool:
            plan.needs_clarification = True
            plan.reply_kind = "clarification"
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

    #: Words that say *which* mailbox to read, not *what to search for*. The search
    #: term is what is left once the medium ("email", "inbox"), the ask ("find",
    #: "show me") and the connectives ("my", "the", "for", "about") are gone. A term
    #: made only of these words means the person asked for the mailbox, not for a
    #: message about a thing -- and asking for the mailbox is a search for nothing,
    #: not a search for the words they said.
    _EMAIL_FILTER_WORDS = frozenset({
        "email", "emails", "mail", "mails", "message", "messages", "inbox", "post",
        "find", "show", "search", "check", "read", "look", "see", "list", "get",
        "me", "my", "our", "your", "the", "a", "an", "of", "for", "about", "from",
        "on", "in", "is", "are", "any", "new", "latest", "recent", "unread",
    })

    @classmethod
    def _email_search_term(cls, raw: str) -> str:
        """The thing a mailbox request searches for, or ``""`` when it names none.

        "find emails about the invoice" searches for *invoice*, not for the words
        used to ask. Without this the route sent ``query=""`` and the provider
        answered with the most recent messages -- a search that does not search,
        and the first step of the email workflow was the one that never ran.
        """
        text = re.sub(r"[^A-Za-z0-9 _'-]", " ", raw or "")
        words = text.split()
        kept = [w for w in words if cls._stem(w.lower()) not in cls._EMAIL_FILTER_WORDS]
        term = " ".join(kept).strip(" ,'-")
        return term if 1 <= len(term) <= 120 else ""

    #: A read-family verb acting on a message the conversation already dealt
    #: with. Strong verbs (read / open / summarise / print) act on a message by
    #: their own nature, so a bare "read it" is a referent even when the
    #: sentence names no email word. Weak verbs (show / tell / give ...) are
    #: general-language and may only consume a referent when the sentence is
    #: email-flavoured -- otherwise "show me the one you built" would be
    #: routed to a mail read. Reply verbs are strong too: "reply to that one"
    #: points at a message by its nature, and the reply branch is the one that
    #: owns that shape, so it must carry the reference here as well.
    _EMAIL_REFERENT_STRONG_VERBS = re.compile(
        r"\b(?:read|open|summar\w*|print|repl\w*|answer|respond|write back)\b", re.I)
    _EMAIL_REFERENT_WEAK_VERBS = re.compile(
        r"\b(?:show|look|tell|give|play)\b"
        r"|\bwhat\s+(?:did|does|do)\s+(?:they|it|he|she|the\s+\w+)\s+sa(?:y|id)\b"
        r"|\b(?:what\s+)?(?:more\s+)?about\b",
        re.I,
    )

    #: A message named by pointing at what the conversation just showed, in the
    #: person's own words. ``that email`` / ``it`` / ``the one you found`` are
    #: referents. ``that invoice email`` is **not**: a name after the
    #: demonstrative is the message itself, and the lookup must search for it
    #: rather than reuse the last read -- so ``that``/``this`` count only when
    #: nothing names the message after them.
    _EMAIL_REFERENT_OBJECT = re.compile(
        r"\bit\b"
        r"|\b(?:that|this)\s+(?:one|email|e-mail|message)\b"
        r"|\bthe\s+one\b"
        r"|\b(?:that|this)\s*[,.!?\s]*$"
        r"|\bthe\s+(?:email|e-mail|message|one)\s+you\s+(?:found|showed|sent|matched)\b",
        re.I,
    )

    #: Ordinal words and numbers a person uses to pick from a list.
    _EMAIL_REFERENT_ORDINAL = re.compile(
        r"\b(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last)\b"
        r"(?:\s+(?:one|email|e-mail|message|it))?"
        r"|\bnumber\s+(\d+)\b"
        r"|\b(?:the\s+)?(\d+)(?:st|nd|rd|th)?\s+(?:one|email|e-mail|message)\b",
        re.I,
    )

    #: What makes a weak-verb sentence ("show me ...") an email request: a
    #: medium word, or an object that names a mailbox relationship on its own
    #: ("that email", "the one you found"). A bare "show me it" has neither,
    #: and must not become a mail read.
    _EMAIL_REFERENT_EMAILISH = re.compile(
        r"\b(?:emails?|e-?mails?|mail|message)\b"
        r"|\byou\s+(?:found|showed|sent|matched)\b",
        re.I,
    )

    def _email_referent(self, text: str, has_email_noun: bool) -> Optional[str]:
        """What a demonstrative ("read that one", "the second one") points at.

        Returns ``""`` for the message the conversation selected, the ordinal
        word or number when the person picked from a list, and ``None`` when the
        sentence names its own target (or is not a referent at all). The planner
        has no memory of the conversation, so it only reports the reference; the
        orchestrator, which holds the per-session selection, resolves it to a
        real uid -- or says honestly that there is nothing to point at.

        A demonstrative, by construction, usually names no email word -- that
        is what "that one" means. Strong read verbs (read / open / summarise /
        print) may therefore carry a referent without a signal: the
        orchestrator is fail-closed, so the worst a referent with no
        selection behind it does is say "there is no email to point at".
        Weak verbs are general-language, so they need an email signal -- a
        medium word or an object that names a mailbox relationship ("that
        email", "the one you found") -- or they do not become references.
        """
        # Making a task from a message is a read of it, not a new operation on the
        # world, so the same rule applies: a demonstrative names the selection.
        if re.search(r"\b(?:task|todo|to-do|reminder)\b", text, re.I):
            return "" if self._EMAIL_REFERENT_OBJECT.search(text) else None
        if not self._EMAIL_REFERENT_STRONG_VERBS.search(text):
            if not (self._EMAIL_REFERENT_WEAK_VERBS.search(text)
                    and (has_email_noun or self._EMAIL_REFERENT_EMAILISH.search(text))):
                return None
        ordinal = self._EMAIL_REFERENT_ORDINAL.search(text)
        if ordinal:
            word, number, suffix = ordinal.group(1), ordinal.group(2), ordinal.group(3)
            return (number or suffix or word).lower()
        if self._EMAIL_REFERENT_OBJECT.search(text):
            return ""
        return None

    @staticmethod
    def _email_reply_body(raw: str) -> str:
        """The words a reply already carries, or ``""`` when it does not.

        "reply to that one saying thanks for the invoice" carries *thanks for
        the invoice*. The lead-in ("reply to that one", "saying") is the
        request, not the body, so it is stripped; what is left is drafted
        verbatim. A reply with no "saying ..." clause gets an empty body, not
        an invented one -- drafting an empty reply the person can edit is the
        honest step.
        """
        match = re.search(
            r"\b(?:saying|that\s+say|with\s+the\s+(?:body|text))\b\s*(.+)$",
            raw, re.I)
        if not match:
            return ""
        return RulePlanner._arg(match.group(1))

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

    #: Which verb class *leads* the sentence decides whether an artifact route may
    #: author anything (Phase 28). Nouns are not consent to write.
    _AUTHOR_VERBS = r"\b(create|make|design|draw|write|build|generate|produce|prepare|add|draft|author|update|extend|revise|document|documented)\b"
    _CONSUME_VERBS = r"\b(read|show|open|get|list|display|print|cat|what|which|tell|find|search|say|says|summarise|summarize|explain|describe|check|review|look)\b"
    _READ_FILE = re.compile(
        r"(?:\b(?:read|open|show|print|display|cat)\b"
        r"(?:\s+(?:me|the|my|a|an|file|files|contents?|of|to))*\s+"
        r"|\bwhat\s+does\s+(?:the\s+|my\s+|this\s+)?(?:file\s+)?)"
        r"[\"'“]?(?P<path>(?:[\w.~+-]*/[\w.~/+-]+)|[\w~+-]+\.\w{1,6}|readme)[\"'”]?",
        re.I,
    )

    def _authoring_request(self, text: str) -> bool:
        """True when the sentence asks for something to be created.

        The earliest verb wins, so ``document the parser`` (authoring first) and
        ``what does the document say`` (consumption first) split correctly. With
        no verb at all the route does not author -- "readme" appearing in a
        sentence is not a request to write one.
        """
        author = re.search(self._AUTHOR_VERBS, text)
        consume = re.search(self._CONSUME_VERBS, text)
        if author is None:
            return False
        if consume is None:
            return True
        return author.start() <= consume.start()

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

    def _read_file(self, text: str, raw: str) -> Optional[Intent]:
        """Read a text file (Phase 28).

        ``read_file`` has been registered since v0.1 and had no route until now:
        one of 29 capabilities no typed sentence could reach, while the noun-only
        design route happily *wrote* a file for the same sentence.

        A bare name -- or a relative path, with or without slashes -- is anchored
        inside the file workspace, so "read notes.txt" cannot quietly mean "read
        whatever sits next to the process". An absolute path, or one starting at
        the home directory, is taken as typed and handed to the engine's
        filesystem guard, which is the single authority on what may be read.
        Reads are not workspace-only the way creation is: refusing ``/etc/hosts``
        here with a write-shaped message would be its own untruth.
        """
        found = self._READ_FILE.search(raw)
        if not found:
            return None
        name = (found.group("path") or "").strip().strip("\"'”“")
        if not name:
            return None
        if name.lower() == "readme":
            name = "README.md"

        typed = Path(os.path.expanduser(name))
        if typed.is_absolute():
            path = str(typed.resolve())
        else:
            workspace = Path(os.path.expanduser(get_settings().file_workspace)).resolve()
            path = str((workspace / name).resolve())
        return Intent("read_file", tool="read_file", params={"path": path}, confidence=0.9)

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
            # "capabilities" and "commands" are also words inside tool names, so this
            # rule fired on "get the device capabilities of phone_main" and answered a
            # capability request with the 127-name listing. Asking what JARVIS can do
            # names nothing; naming a capability is not asking for the catalogue.
            if self._capability_matches(text):
                return None
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
            return Intent("list_apps", tool="get_running_applications")
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
        # The catch-all reads every vehicle word as "status". "list car metrics" is a
        # different request -- a registered capability of its own -- and an advertising
        # default yields to a sentence that names a capability.
        if self._capability_matches(text):
            return None
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
            if not title:
                # The tool requires a title, so emitting the action with none
                # handed the user "missing required parameter 'title'" -- the
                # gate's vocabulary as a conversational answer. Ask instead.
                return Intent("watch_notify", note="What should the notification say?")
            return Intent(
                "watch_notify",
                tool="send_watch_notification",
                params={"title": title},
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

    #: Where an inline list of parts begins: "with a, b", "steps: a, b",
    #: "of a, b and c", or after a colon.
    _PARTS_MARKER = (r"\b(?:with|having|containing|using|composed\s+of|made\s+(?:up\s+)?of"
                     r"|steps?|stages?|components?|parts)\b\s*(?:are|is|as follows)?[:\s]+")
    _PARTS_SPLIT = r"\s*(?:,|;|\band\b|\bthen\b|\bplus\b|&)\s*"

    @classmethod
    def _listed_parts(cls, raw: str) -> str:
        """The two-or-more-item list a sentence names, or "" when it names none.

        A diagram needs components and a flowchart needs steps, and until Phase 29
        both routes asked for neither -- they sent a title alone and the permission
        gate answered the user with ``missing required parameter 'steps'``, even when
        the sentence itself listed the steps. What a sentence states is taken from it;
        what it does not, the route asks for rather than guessing.
        """
        tails: list[str] = []
        if ":" in raw:
            tails.append(raw.split(":", 1)[1])
        found = re.search(cls._PARTS_MARKER + r"(.+)$", raw, re.I)
        if found:
            tails.append(found.group(1))
        of_clause = re.search(r"\b(?:of|for)\s+(.+)$", raw, re.I)
        if of_clause:
            tails.append(of_clause.group(1))
        for tail in tails:
            items = [t.strip(" .!?\"'”“") for t in re.split(cls._PARTS_SPLIT, tail)]
            items = [t for t in items if t]
            if not items or any(len(t) < 2 or len(t.split()) > 4 for t in items):
                # All or nothing. Taking the two items that happened to look
                # tidy and dropping "approve with review as a decision" would
                # draw a two-step chart from a three-step request -- a partial
                # answer with nothing saying so, which is the defect this
                # program has been closing since Phase 19. Asking costs the
                # user one turn; that one costs them a wrong artifact.
                continue
            if len(items) >= 2:
                return ", ".join(items)
        return ""

    @classmethod
    def _parts_stem(cls, raw: str) -> str:
        """``raw`` with an inline parts list removed, so a title stays a title."""
        stem = re.split(cls._PARTS_MARKER, raw, maxsplit=1, flags=re.I)[0]
        return stem.split(":", 1)[0].strip().rstrip(",; ")

    @staticmethod
    def _diagram_args(parts: str) -> tuple:
        """Split a sentence's parts into the builder's ``components`` and ``links``.

        ``parse_components`` wants ``id:label`` pairs, and a sentence that says
        "api > worker, worker > db" names both boxes *and* the arrow between
        them. Feeding such a list in as components alone would have drawn two
        boxes and silently dropped every connection the user described -- the
        partial answer with nothing saying so.
        """
        components: list[str] = []
        links: list[str] = []
        seen: list[str] = []

        def slug(name: str) -> str:
            return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or f"part{len(seen) + 1}"

        def register(name: str) -> str:
            name = name.strip()
            if name.lower() not in [s.lower() for s in seen]:
                seen.append(name)
                components.append(f"{slug(name)}:{name}")
            return slug(name)

        for item in (p.strip() for p in parts.split(",")):
            if not item:
                continue
            if ">" in item:
                left, _, right = item.partition(">")
                if left.strip() and right.strip():
                    links.append(f"{register(left)}>{register(right)}")
                    continue
            if ":" in item:
                components.append(item)
                continue
            register(item)
        return ", ".join(components), ", ".join(links)

    def _location(self, text: str, raw: str) -> Optional[Intent]:
        if re.search(r"\b(location summary|where (have|did) i|places i|travel (summary|history))\b", text):
            date = "yesterday" if "yesterday" in text else "today"
            return Intent("location_summary", tool="get_location_summary", params={"date": date})
        return None

    def _storage(self, text: str, raw: str) -> Optional[Intent]:
        if not re.search(r"\bstorage\b|\bdisk (space|full|usage)\b|\bfree space\b"
                         r"|\bspace (?:is )?(?:left|free|available)\b", text):
            return None
        if _ERASE_REQUEST.search(text):
            return Intent(
                "storage_erase",
                clarification=False,
                note=(
                    "I can read your disk, but I will not empty the trash or wipe free space from "
                    "a sentence -- that is removing data, and it needs to be something you do "
                    "deliberately yourself. Nothing was run. Say \"check storage\" if you wanted "
                    "the number."
                ),
            )
        if re.search(r"\b(phone|mobile|android)\b", text):
            return Intent(
                "phone_storage",
                note=f"I cannot read that device's storage: {UNAVAILABLE_REASONS['phone']}.",
            )
        return Intent("check_storage", tool="check_storage")

    def _battery(self, text: str, raw: str) -> Optional[Intent]:
        if not re.search(r"\bbattery\b|\bpower level\b|\bcharge level\b|\bcharg(?:e|ing)\b", text):
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
        return Intent("laptop_battery", tool="get_battery_status", confidence=0.95)

    #: App names a person is likely to ask about, and the words that make the question
    #: a status check rather than a launch request. The probe itself takes any name;
    #: this only decides which sentences are allowed to reach it.
    _AGENT_STATUS = re.compile(
        r"\b(?:is|are|check (?:if )?|see (?:if )?)\s+"
        r"(?P<app>[A-Za-z][A-Za-z0-9 ._-]{1,30}?)\s+"
        r"(?:still\s+)?\b(working|running|up|alive|responding|installed)\b",
        re.I,
    )

    def _agent_status(self, text: str, raw: str) -> Optional[Intent]:
        """"is qoder working" -> check_agent_status.

        Ordered next to the system-status rule because it reads the same way to a
        person and resolves to a different tool: one asks about this machine, the
        other about an app on it. Read-only, so it asks nothing of the user and is
        never gated.
        """
        found = self._AGENT_STATUS.search(raw)
        if not found:
            return None
        name = " ".join(found.group("app").split()).strip(" .")
        if not name:
            return Intent("check_agent_status", note="Which app should I check?")
        return Intent(
            "check_agent_status",
            tool="check_agent_status",
            params={"app": name[0].upper() + name[1:]},
            confidence=0.85,
        )

    def _machine_snapshot(self, text: str, raw: str) -> Optional[Intent]:
        """The laptop's overall condition, asked about as a whole machine.

        Kept apart from ``_system_status`` deliberately: that one answers a question about
        a resource (cpu, memory, disk, load), this one answers "how is the machine". The
        registry had no sentence that reached the snapshot at all before this -- a tool
        that exists and cannot be asked for is the Phase 28 defect, and renaming it did
        not make it reachable.
        """
        if re.search(r"\b(how is my (mac|macbook|laptop|computer)|"
                     r"machine (status|snapshot|condition)|"
                     r"computer (status|snapshot)|laptop (status|snapshot|condition)|"
                     r"(host|machine) ?name|what is my computer called)\b",
                     text):
            return Intent("machine_snapshot", tool="get_machine_snapshot")
        return None

    def _system_status(self, text: str, raw: str) -> Optional[Intent]:
        if re.search(r"\b(system status|system health|cpu|memory usage|disk usage|load average)\b", text):
            return Intent("system_status", tool="system_status")
        return None

    def _time(self, text: str, raw: str) -> Optional[Intent]:
        #: "what date is it" is §6's wording and was not matched: the tool answers with the
        #: date, so the sentence had a capability and no route. The date question has to be
        #: about the calendar day -- "what is the date of my meeting" is a search, not a clock.
        if re.search(r"\b(what time|the time|current time|what.s the date|today.s date|"
                     r"what date is it|what.s today|what day is it)\b", text):
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
            # "open vscode with my-project" is an editor launch whose folder happens to
            # contain the word "project". Naming an installed app makes that reading
            # certain, and declining here made such folders impossible to open.
            named = [
                re.search(
                    r"\b(?:open|launch|start|run)\s+(?P<app>[\w .+-]+?)\s+(?:with|in|to|at|for)\b",
                    raw, re.I),
                # "open folder ~/code/my-project in vscode" names the app last.
                re.search(r"\s+in\s+(?P<app>[\w .+-]+?)\s*$", raw, re.I),
            ]
            for match in named:
                if match and self.apps.resolve(match.group("app").strip()) is not None:
                    return None
            # This used to be declined with a note about missing agents. The build has
            # them, and has had the folder-open since the phase before, so the sentence
            # is now an action on whatever project the user named -- or the one open.
            name = self._match(r"\b(?:my|the)\s+([A-Za-z0-9 _-]+?)\s+project\b", raw)
            params = {"project": name.strip()} if name else {}
            return Intent("continue_project", tool="open_project_folder", params=params)
        return None

    def _web_search(self, text: str, raw: str) -> Optional[Intent]:
        """"search Google for X" -- a search, not a navigation.

        Narrow on purpose. The object of the verb has to be a search service or nothing at all,
        because "search files for my report" and "search my email for the invoice" already
        belong to other capabilities, and a new route that swallows them is a regression wearing
        a feature. It is deliberately *not* aliased onto ``open_url``: opening google.com answers
        a different question than the one was asked.
        """
        # Matched on the sentence as spoken, not the lowercased one: the query is what the
        # person said, and "FastAPI" and "fastapi" are different strings to search for.
        m = re.search(r"\bsearch\s+(?:google|the web|the internet|duckduckgo|bing)\s+for\s+"
                      r"(?P<what>[\w\s,.:'?()\-]+?)\s*$", raw, re.I)
        if m is None:
            m = re.search(r"\bsearch\s+for\s+(?P<what>[\w\s,.:'?()\-]+?)\s*$", raw, re.I)
        if m is None:
            return None
        what = m.group("what").strip()
        if not what:
            return None
        return Intent("web_search", tool="search_web", params={"query": what}, confidence=0.9)

    def _search_files(self, text: str, raw: str) -> Optional[Intent]:
        # "find emails about..." / "search emails for..." belongs to the email
        # route, not to file search. Without this gate, "find emails about the
        # project proposal" was caught here (type_word "projects?" + find verb)
        # and routed to search_files instead of email.
        if re.search(r"\b(emails?|messages?|mail|inbox)\b", text):
            return None

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
        # "archive that event" / "archive my standup meeting" / "archive that
        # schedule" is a calendar or managed-domain request, not a file to
        # compress. The transfer rule runs before those rules, so it must defer
        # (return None) -- otherwise "archive my standup meeting" becomes a
        # proposal to compress a file literally named "my standup meeting". A
        # file or folder whose name contains a domain word still wins (it looks
        # like a file / names a container).
        if self._names_a_domain_thing(path) and not self._looks_like_a_file(path):
            return None
        # "archive that email" is not a file to compress. A real folder called
        # "emails/" still passes (it looks like a file); a mail message does not.
        if self._names_an_email_thing(path) and not self._looks_like_a_file(path):
            return self._email_not_supported("archive or compress")
        if not path or path.lower() in {"this", "that", "it"}:
            return Intent("compress", note="Which file or folder should I compress?")
        return Intent("compress", tool="compress_path", params={"path": path})

    def _delete(self, text: str, raw: str) -> Optional[Intent]:
        captured = self._match(r"\b(?:delete|remove|trash|get rid of)\b\s+(.+)", raw)
        if captured is None:
            return None
        path = self._arg(captured)
        # "delete my Design Review event" / "delete the paper about X" /
        # "delete that schedule" is a request to remove a calendar or
        # managed-domain entry, not to delete a file. Defer to the domain rule
        # (calendar routes to cancel_event, behind its own confirmation) --
        # otherwise this becomes a proposal to delete a file literally named
        # after the entry. A file or folder whose name contains a domain word
        # still wins.
        if self._names_a_domain_thing(path) and not self._looks_like_a_file(path):
            return None
        # "delete that email" is a request to remove a mail message, not a file.
        # The backend has no such operation, so decline honestly rather than
        # deleting a file literally named "my invoice email".
        if self._names_an_email_thing(path) and not self._looks_like_a_file(path):
            return self._email_not_supported("delete")
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
            src = self._arg(copy_match.group(1))
            # "copy that event to notes" / "copy that schedule to a file" is a
            # calendar or managed-domain request, not a file to copy. Defer; a
            # file or folder named after a domain entry still wins.
            if self._names_a_domain_thing(src) and not self._looks_like_a_file(src):
                return None
            # "copy that email to notes" is not a file to copy. A folder really
            # called "emails/" still passes (it looks like a file).
            if self._names_an_email_thing(src) and not self._looks_like_a_file(src):
                return self._email_not_supported("copy or save aside")
            return Intent(
                "copy",
                tool="copy_file",
                params={
                    "source": src,
                    "destination": self._arg(copy_match.group(2)),
                },
            )
        move_match = re.search(r"\b(?:move|rename)\b\s+(.+?)\s+to\s+(.+)", raw, re.I)
        if move_match:
            src = move_match.group(1).strip()
            dst = move_match.group(2).strip()
            # "move my invoice email to the trash" is a request to refile a mail
            # message, not a file. Same guard shape as the calendar deferral
            # below: on the subject, and only when the subject is not itself a
            # file. The backend has no such operation, so decline honestly.
            if self._names_an_email_thing(src) and not self._looks_like_a_file(src):
                return self._email_not_supported("move or refile")
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
            # "move my Design Review event to tomorrow at 3pm" is a calendar
            # request, not a file rename. The transfer rule runs before the
            # calendar rule, so it has to defer here -- otherwise a request
            # to *move an event* becomes a proposal to rename a file, and
            # the person approves it.
            #
            # The deferral is on the *subject* (the thing being moved) and
            # not on the destination, because the destination of a calendar
            # move is a time ("tomorrow at 3pm") and the destination of a
            # file move is a path.
            #
            # A subject that *is* a file wins over the domain words: a file
            # called "event-log.txt" is a file, and refusing to move it
            # because its name contains a domain noun is the same kind of
            # wrong-domain reading this deferral exists to prevent. Covers
            # calendar entries and the other managed domains.
            if self._names_a_domain_thing(src) and not self._looks_like_a_file(src):
                return None
            # A move whose *destination* is a clock time is a calendar move, not a
            # file rename: "reschedule Move probe sync to 5 PM" and "move the design
            # review to 4 PM" both became `move_file` proposals, and the person was
            # asked to approve a filesystem rename of a meeting. The destination of
            # a file move is a path; the destination of a calendar move is a time.
            is_clock_destination = bool(re.search(
                r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b|"
                r"\b(?:today|tomorrow|tonight|next\s+\w+|this\s+\w+)\b|"
                r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                dst, re.I))
            if is_clock_destination and not self._looks_like_a_file(src):
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

    #: The words that name a calendar thing, used to defer the file-move
    #: rule. Deliberately the *nouns* only: "move" and "to" are the rule's
    #: own words, and a title that happens to contain one of them ("move
    #: the budget to 2027") must not read as a calendar request.
    _CALENDAR_SUBJECT = re.compile(
        r"\b(calendar|event|events|meeting|meetings|appointment|appointments|"
        r"invite|invitation|invitations|reschedule|rebook|attendee|"
        r"availability|"
        # Recurring-meeting names are calendar objects too, and without them
        # "archive the standup" became a proposal to zip a file of that name.
        r"standup|stand-up|sync|syncs|1:1|one-on-one|retro|retrospective|"
        r"kickoff|kick-off|huddle|scrum|review|interview)\b", re.I)

    def _names_a_calendar_thing(self, subject: str) -> bool:
        """Whether the thing named is a calendar entry, not a file/folder.

        The check is on the subject and not on the whole sentence: "move my
        Design Review event to tomorrow" names an event, while "move the
        report to the archive" does not. A subject that names a *container*
        (a folder/directory/file) is a filesystem target, not a calendar
        entry, so "compress the events folder" compresses a folder called
        "events", not a calendar request.
        """
        subject = subject or ""
        if self._SUBJECT_IS_A_CONTAINER.search(subject):
            return False
        return bool(self._CALENDAR_SUBJECT.search(subject))

    #: The words that name an email message, used to keep the file routes from
    #: treating a mail request as a file operation. The same cross-domain
    #: misroute the calendar deferral guards, one verb family over: "archive
    #: that email" must not become `compress_path("that email")`, and "move my
    #: invoice email to the trash" must not become a `move_file`. These are the
    #: *nouns* only, for the same reason as the calendar set.
    _EMAIL_SUBJECT = re.compile(
        r"\b(emails?|e-?mails?|mail|message|messages|inbox|mailbox)\b", re.I)

    #: Container nouns: a subject that names one is a file/directory the person
    # wants to manipulate, not a message or a calendar entry. "compress the
    # events folder" and "move the emails folder to backup" target a folder
    # that happens to be called "events"/"emails", and must not be read as a
    # calendar or mail request. Shared by the calendar and email deferrals.
    _SUBJECT_IS_A_CONTAINER = re.compile(
        r"\b(folders?|directories?|dirs?|files?|paths?)\b", re.I)

    def _names_an_email_thing(self, subject: str) -> bool:
        """Whether the thing named is an email message, not a file.

        Mirrors `_names_a_calendar_thing`: the check is on the subject, and the
        caller additionally confirms it does not *look* like a file (a path or
        extension). A subject that also names a container ("the emails folder")
        is a directory, not a message, so it is not treated as mail either.
        """
        subject = subject or ""
        if self._SUBJECT_IS_A_CONTAINER.search(subject):
            return False
        return bool(self._EMAIL_SUBJECT.search(subject))

    #: Nouns that name an entry in one of the other managed domains, used to keep
    # the file-mutating routes from treating a domain request as a file op. Same
    # wrong-target-mutation class as the calendar and email deferrals: "delete the
    # paper about growing agents" must not become `delete_file("the paper about
    # growing agents")`. Deliberately the *nouns* only, and a conservative set --
    # only the domain nouns audited to be misrouted (asset / paper / schedule /
    # briefing / workspace). Broader words (note, project, report) route to the
    # memory/file routes by design and are not added here.
    _DOMAIN_SUBJECT = re.compile(
        r"\b(asset|assets|paper|papers|schedule|schedules|briefing|briefings|"
        r"workspace|workspaces)\b", re.I)

    def _names_a_domain_thing(self, subject: str) -> bool:
        """Whether the subject names a calendar or other managed-domain entry,
        not a file -- the condition under which a file-mutating route must defer.

        Superset of `_names_a_calendar_thing` (calendar nouns + the other
        managed domains). Excludes subjects that name a *container* (a folder
        or file, by extension or by container noun), so "compress the events
        folder" still compresses a folder. The caller confirms the subject is
        not a file; this gives the noun condition.
        """
        subject = subject or ""
        if self._SUBJECT_IS_A_CONTAINER.search(subject):
            return False
        return bool(self._CALENDAR_SUBJECT.search(subject)
                    or self._DOMAIN_SUBJECT.search(subject))

    def _email_not_supported(self, action: str) -> Intent:
        """An honest decline for an organize operation the mail backend lacks.

        The mail providers (local maildir, IMAP/SMTP) implement search / read /
        summarise / draft / send and nothing else. "archive that email", "move
        my invoice email to work", "delete that email" have no real operation
        to call, so a file route must not turn them into a file mutation and
        the planner must not invent one. This says what the backend actually
        does instead of pretending it can do the organize step.
        """
        return Intent(
            "email",
            note=(
                f"I can't {action} a message — this mail backend has no such "
                "operation. I can search for it, read it, summarise it, or "
                "draft a reply."
            ),
            clarification=False,
        )

    @staticmethod
    def _looks_like_a_file(subject: str) -> bool:
        """Whether the subject names a file rather than a thing.

        A path separator or a file extension is what makes it a file. The
        sentence "move my Design Review event" has neither, so it is not one.
        """
        text = (subject or "").strip()
        if not text:
            return False
        if "/" in text:
            return True
        return bool(re.search(r"\.[a-z0-9]{1,8}$", text, re.I))

    def _focus_app(self, text: str, raw: str) -> Optional[Intent]:
        """Bring a running application forward: "switch to Mail", "show me Finder".

        Deliberately narrower than the open rule and checked before it, so asking
        to switch to something does not launch a second copy.
        """
        captured = re.match(
            r"\s*(?:switch|jump|bring|flip|move|go)\s+(?:me\s+)?(?:back\s+)?(?:to|over\s+to|into)\s+"
            r"(?:the\s+|my\s+)?(?P<app>.+?)\s*$", raw, re.I)
        if captured is None:
            # "bring Finder to front" / "bring Finder forward" -- the app is named
            # before the phrase, so the verb-first patterns above never see it.
            captured = re.match(
                r"\s*(?:bring|pull|put)\s+(?:the\s+|my\s+)?(?P<app>.+?)\s+"
                r"(?:to\s+(?:the\s+)?)?(?:front|forward)\s*$", raw, re.I)
        if captured is None:
            captured = re.match(
                r"\s*(?:focus|activate|bring\s+up)\s+(?:the\s+|my\s+)?(?P<app>.+?)\s*$",
                raw, re.I)
        if captured is None:
            # "show me the mail app" is a switch; "show me that email" is not.
            # Only a subject that names an *application* qualifies, which the
            # resolver check below enforces -- the noun rules here keep the
            # email/calendar routes from being stolen by a switch.
            shown = re.match(
                r"\s*show\s+me\s+(?:the\s+|my\s+)?(?P<app>.+?)\s*$", raw, re.I)
            if shown and not re.search(
                    r"\b(emails?|e-?mails?|mail|messages?|inbox|events?|calendar|"
                    r"meeting|appointments?|files?|documents?)\b",
                    shown.group("app"), re.I):
                captured = shown
        if captured is None:
            return None
        app = self.apps.resolve(self._arg(captured.group("app")))
        if app is None:
            return None
        return Intent("focus_app", tool="focus_application", params={"app": app.key})

    def _close_app(self, text: str, raw: str) -> Optional[Intent]:
        # "close the page you opened" / "close the browser tab" is Vantage's own
        # browser session, not an application. `_close_app` would resolve "the
        # page you opened" loosely enough to attempt quitting Chrome.
        if re.search(r"\b(?:page|tab|browsing)\b|\byou\s+(?:opened|were)\b",
                     text, re.I):
            return None
        captured = self._match(r"\b(?:close|quit|exit)\b\s+(.+)", raw)
        if captured is None:
            return None
        app = self.apps.resolve(self._arg(captured))
        if app is None:
            return None
        return Intent("close_app", tool="close_app", params={"app": app.key})

    _AGENT_NAMES = r"(?:claude(?:\s+code)?|the\s+coding\s+agent|cursor\s+agent)"


    def _begin_work(self, text: str, raw: str) -> Optional[Intent]:
        """Act on the plan: open the next task's project and offer the agent."""
        import re as _re

        if _re.match(r"\s*(?:begin|start)\s+(?:my\s+)?work\s*$", raw, _re.I) or _re.search(
                r"\b(?:begin|start)\s+work\s+on\s+(?:the\s+)?next\b", text, _re.I):
            return Intent("begin_work", tool="begin_work", params={})
        return None

    def _running_apps(self, text: str, raw: str) -> Optional[Intent]:
        """What is running right now — installed-and-running, not the catalogue."""
        import re as _re

        # "every day at 6 PM show me my running applications" is a request to
        # *schedule* the question, not to answer it now. The scheduler rule owns
        # anything with a cadence; this one would otherwise answer immediately and
        # the "every day" would be silently dropped.
        if _re.search(r"\b(?:every|each)\b", text, _re.I):
            return None

        # Only the explicit "running applications" phrasing belongs here. Plain
        # "what's running" is the laptop cockpit's question, which reports real
        # processes rather than the launch catalogue.
        if _re.search(r"\b(?:running|open|active)\s+applications?\b", text, _re.I):
            return Intent("get_app_status", tool="get_app_status", params={})
        return None

    def _review_downloads(self, text: str, raw: str) -> Optional[Intent]:
        """List what is in Downloads. It reviews; it never deletes.

        Deliberately reads only — "clean up my downloads" was previously
        unreachable, and the safe answer is to show what is there rather than to
        offer a delete it cannot undo.
        """
        import re as _re

        if not _re.search(r"\bdownloads\b", text, _re.I):
            return None
        if _re.search(r"\b(?:clean|clear|empty|purge|delete|remove|tidy)\b", text, _re.I):
            return Intent(
                "review_downloads",
                note=("I will not delete anything from Downloads. Say \"show me my "
                      "downloads\" and I will list what is there for you to decide."),
                clarification=True,
            )
        if _re.search(r"\b(?:show|list|review|what(?:'s|\s+is)|see)\b", text, _re.I):
            return Intent("review_downloads", tool="review_downloads", params={})
        return None

    def _page_actions(self, text: str, raw: str) -> Optional[Intent]:
        """Read the page Vantage opened, and open one to read.

        The browser tools are real (Playwright against an isolated session) but
        the *read* was unreachable from a sentence, so a person could open a page
        and then had no way to ask what was on it. Reading is a question and gets
        a route; click, type and save deliberately do not -- Phase 28's line, kept
        and now protected by two tests.

        No session id is carried here: the tools fall back to the session this
        conversation owns.
        """
        import re as _re

        # "browse to <url>" opens Vantage's own controllable session, which is
        # what read/click/type below act on. Plain "open <url>" keeps meaning the
        # person's own browser -- it is logged in, and steering it would surprise.
        browse = _re.match(
            r"\s*(?:browse|navigate)\s+(?:to\s+)?(?P<url>\S+)\s*$", raw, _re.I)
        if browse:
            return Intent("browser_open", tool="browser_open",
                          params={"url": browse.group("url")})

        # "close the page you opened" / "stop browsing" ends Vantage's own
        # session, not the person's browser. "close the browser" was reaching
        # `close_app` and attempting to *quit Chrome* — a destructive act on the
        # wrong thing, and unverifiable besides. `browser_close` was registered
        # and unreachable.
        if re.search(r"\b(?:close|stop|end|exit)\b.*\b(?:brows(?:er|ing)|page|tab)\b",
                     text, _re.I) and re.search(
                r"\b(?:page|tab|browsing)\b|\byou\s+(?:opened|were)\b", text, _re.I):
            return Intent("browser_close", tool="browser_close", params={})

        if _re.search(r"\b(?:read|what\s+does|summari[sz]e|what(?:'s|\s+is)\s+on)\b.*\b(?:page|site|website)\b", text, _re.I) or _re.fullmatch(
                r"\s*(?:read|refresh)\s+(?:the\s+)?page\s*[.!]?\s*", text, _re.I):
            return Intent("browser_read", tool="browser_read", params={})

        # Deliberately NO rule for click/type/save. Phase 28 drew this line and two
        # tests protect it: a sentence must not be able to *act* on a page. Those
        # tools stay reachable as confirmable proposals, never as a typed route --
        # a misheard "click buy" is not undoable. Reading is a question and is
        # routed; acting is not.
        return None

    def _availability(self, text: str, raw: str) -> Optional[Intent]:
        """Whether a named time is free — "am I free at 3 PM tomorrow".

        Distinct from `find_free_time`, which answers "when is my next gap". This
        is the question asked before agreeing to something.
        """
        import re as _re

        # "do I have time at 3 tomorrow" is the same question as "am I free at 3",
        # and it did not reach this rule at all — the vocabulary was "free /
        # available / busy" only.
        if not _re.search(r"\b(?:free|available|busy|have\s+time|any\s+time)\b",
                          text, _re.I):
            return None
        # "find free time" asks for the next gap, which `find_free_time` answers;
        # it is a different question from "am I free at 3".
        if _re.search(r"\b(?:find|next|when\s+(?:is|am))\b.*\b(?:free\s+time|free\s+slot)\b",
                      text, _re.I):
            return None
        # Capture everything after the preposition, then keep the longest prefix
        # that parses as a time. A single regex kept truncating at the first
        # boundary ("at 3 tomorrow" -> "3"), which is the right clock on the wrong
        # day -- worse than not matching.
        from ..integrations.calendar_provider import parse_datetime as _parse_when

        # Scan the *whole sentence*, not just what follows a preposition: a time
        # can sit on either side of one ("at 3 pm tomorrow", "tomorrow at 3 pm",
        # "tomorrow 3 pm"). Prefix-trimming from a fixed anchor kept one half and
        # dropped the other, which is how "tomorrow 3 pm" became the day's default
        # hour and "tomorrow at 3 pm" lost its day.
        stripped = _re.sub(
            r"^\s*(?:am|are)\s+i\s+|\b(?:do\s+i\s+have\s+time|free|available|busy)\b",
            " ", raw, flags=_re.I)
        when_text = ""
        words = stripped.split()
        clock = _re.search(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}:\d{2}", stripped, _re.I)
        clock_token = (clock.group(0).split()[0].lower() if clock else "")
        for take in range(len(words), 0, -1):
            trial = " ".join(words[:take])
            if _parse_when(trial) is None:
                continue
            # The hour must survive: "3 pm tomorrow" trimmed to "tomorrow" parses
            # fine and answers about the wrong hour.
            if clock_token and clock_token not in trial.lower():
                continue
            when_text = trial
            break
        if not when_text:
            # "am I free friday afternoon" names a day-part, not a clock. The day
            # is what decides which calendar to read; the part narrows it, and the
            # answer is honest about covering the whole day when it must.
            daypart = _re.search(
                r"\b(?P<when>(?:monday|tuesday|wednesday|thursday|friday|saturday|"
                r"sunday|mon|tue|wed|thu|fri|sat|sun)"
                r"(?:\s+(?:morning|afternoon|evening))?"
                r"|(?:today|tomorrow)(?:\s+(?:morning|afternoon|evening))?)\b",
                raw, _re.I)
            if daypart:
                return Intent("check_availability", tool="check_availability",
                              params={"when": daypart.group("when").strip()})
        if when_text:
            return Intent("check_availability", tool="check_availability",
                          params={"when": when_text})
        # "am I free tomorrow" / "am I free next week" without a clock.
        day = _re.search(r"\b(today|tomorrow|tonight|next\s+\w+|this\s+\w+)\b", raw, _re.I)
        if day:
            return Intent("check_availability", tool="check_availability",
                          params={"when": day.group(1).lower()})
        return None

    def _clipboard(self, text: str, raw: str) -> Optional[Intent]:
        """Read the clipboard, or copy text to it.

        Reading is a question and gets a route. Writing destroys what was there,
        so it stays confirmation-gated -- the tool carries that flag; the route
        only ever names *what* to copy, never decides for the person.
        """
        import re as _re

        # "the clipboard" and "my clipboard" are the same object, and people say
        # the second one more often. Only the bare "the" form was accepted, so
        # "what's on my clipboard" fell past this rule (to "understood the words
        # but not the goal") while "what's on the clipboard" reached the tool.
        # "whats" without the apostrophe is the same question. "in" is not
        # accepted: "what's in my clipboard" was never a phrasing the tool
        # answered, and widening the route for it is not this fix.
        if _re.search(r"\b(?:what(?:'s|\s+is|s)?\s+(?:on|in)\s+(?:(?:the|my|your)\s+)?clipboard|"
                      r"read\s+(?:(?:the|my|your)\s+)?clipboard|paste\s+it)\b", text, _re.I):
            return Intent("read_clipboard", tool="read_clipboard", params={})

        copy = _re.search(r"\b(?:copy|put)\s+(?P<what>.+?)\s+(?:to|onto|into|on)\s+"
                          r"(?:(?:the|my|your)\s+)?clipboard\b",
                          raw, _re.I)
        if copy:
            what = copy.group("what").strip()
            if what:
                return Intent("write_clipboard", tool="write_clipboard",
                              params={"text": what})
        return None

    #: Phrases that name a schedulable action, in the order they win. Each maps to
    #: a registered READ/LOW tool the scheduler may run unattended.
    _SCHEDULED_ACTIONS: tuple = (
        (r"\b(?:check|run)\s+(?:the\s+)?build\b|\bcheck build\b", "check_build"),
        (r"\b(?:run|check)\s+(?:the\s+)?(?:test|tests|test suite)\b", "run_tests"),
        (r"\bsystem status\b|\bcheck (?:the )?system\b", "system_status"),
        (r"\brunning applications?\b|\bwhat(?:'s|\s+is)\s+running\b", "get_app_status"),
        (r"\b(?:show|review|list)\s+(?:me\s+)?(?:my\s+)?downloads?\b", "review_downloads"),
        (r"\b(?:check|read)\s+(?:the\s+)?clipboard\b", "read_clipboard"),
        (r"\bcheck (?:the )?battery\b|\bbattery status\b", "get_battery_status"),
        (r"\bcheck (?:the )?storage\b|\bdisk space\b", "check_storage"),
        (r"\bprepare (?:the )?meeting\b|\bmeeting brief\b", "brief_meeting"),
        (r"\b(?:plan|start) (?:my )?day\b", "plan_day"),
        (r"\bweekly report\b", "weekly_report"),
    )

    def _scheduled_action(self, text: str, raw: str):
        """(tool, params) for the action a scheduled sentence names, or None.

        A schedule whose action is "reminder" and whose message is empty is a job
        that fires and says nothing — which is what "every morning at 8 check the
        build" used to create. The action comes from the phrase *after* the
        cadence, so "every morning" cannot be read as the thing to do.
        """
        tail = raw
        cadence = re.search(
            r"\b(?:every|each)\s+[\w\s]{0,24}?(?:\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?\b",
            raw, re.I)
        if cadence:
            tail = raw[cadence.end():]
        for pattern, tool in self._SCHEDULED_ACTIONS:
            if not re.search(pattern, tail, re.I):
                continue
            # Only a tool the registry actually has can be scheduled. Two of these
            # phrases name *workflow templates* (check build, run tests), not tools,
            # and a schedule whose action does not exist fails at approval time with
            # "I do not know an action called 'check_build'". Deferring to the
            # reminder path is the honest fallback; inventing the name was not.
            if self.tools.has(tool):
                return (tool, {})
            return None
        return None

    def _task_from_email(self, text: str, raw: str) -> Optional[Intent]:
        """Turn a message into a planner task.

        Runs before `_planner` (which read "create a task from that email" as a
        task titled "from that email") and before `_email` (which read it as a
        search). Both halves of the sentence are real: the referent belongs to the
        email, the task belongs to the planner.
        """
        task_from_mail = re.search(
            r"\b(?:task|todo|to-do|reminder)\b.*\b(?:from|for|about)\b.*"
            r"\b(?:email|e-mail|message|inbox|it)\b", text, re.I) or re.search(
            r"\b(?:email|message)\b.*\b(?:into|as)\s+(?:a\s+)?(?:task|todo|to-do)\b",
            text, re.I)
        if not task_from_mail:
            return None
        ref = self._email_referent(text, True)
        if ref is not None:
            return Intent("task_from_email", note="Linking the email to the task.",
                          clarification=False, referent=ref, confidence=0.75)
        named = re.search(
            r"\b(?:from|about)\s+(?:the\s+)?(?:email|message)\s+(?:about|on|for)\s+"
            r"(?P<q>.+?)\s*$", raw, re.I)
        if named:
            query = re.sub(r"^(?:the|a|an|my)\s+", "", named.group("q").strip(), flags=re.I)
            return Intent("make_task_from_email", tool="task_from_email_named",
                          params={"query": query.strip()})
        return None

    def _cancel_task(self, text: str, raw: str) -> Optional[Intent]:
        """Cancel a planner task. Checked before the file rules.

        "delete the task to call Sam" was a `delete_file` proposal on a file named
        after the sentence -- there was no task-cancel tool at all, so the only
        rule that matched was the filesystem one. A task object belongs to the
        planner whatever verb is used.
        """
        import re as _re

        if not _re.search(r"\b(?:task|todo|to-do)\b", text, _re.I):
            return None
        if not _re.search(r"\b(?:delete|remove|cancel|drop|scrap|get\s+rid\s+of)\b",
                          text, _re.I):
            return None
        named = _re.search(
            r"\b(?:task|todo|to-do)\s+(?:to\s+|called\s+|named\s+|about\s+)?"
            r"(?P<t>.+?)\s*$", raw, _re.I)
        title = self._arg(named.group("t")) if named else ""
        return Intent("cancel_task", tool="cancel_task", params={"title": title or ""})

    def _reschedule_unfinished(self, text: str, raw: str) -> Optional[Intent]:
        """Move timed, unfinished work into real free time.

        Checked before the file rules, which read "move X to Y" as a `move_file`
        — "move unfinished tasks to available times" became a file move on a file
        called "unfinished tasks".
        """
        import re as _re

        if _re.search(r"\b(?:move|reschedule|push|shift|re-?arrange)\b", text, _re.I) and _re.search(
                r"\b(?:unfinished|uncompleted|left\s*over|remaining|incomplete|overdue)\b",
                text, _re.I):
            return Intent("reschedule_unfinished", tool="reschedule_unfinished",
                          params={"days": 5})
        return None

    def _schedule_coding_block(self, text: str, raw: str) -> Optional[Intent]:
        """Book coding time into a real free slot.

        Checked before `_coding`, which sees the word "coding" and asks for a
        workspace id -- the wrong question for "arrange my coding time". Two
        shapes are accepted, because people write both: a coding noun with an
        optional duration ("arrange my coding time", "block 2 hours for the
        engine") and a duration followed by its purpose ("block out 90 minutes
        for the operator").
        """
        import re as _re

        lead = r"\b(?:block(?:\s+out)?|arrange|schedule|book|set\s+aside)\s+"
        duration = r"(?:(?P<dur>\d+)\s*(?P<unit>minutes?|mins?|hours?|hrs?)\s*(?:of\s+)?)?"
        coding_noun = r"(?:coding|development|dev|focus)\s*(?:time|work|block|session)?"
        purpose = r"(?:(?:for|on|to)\s+(?P<goal>.+?))?"

        match = _re.search(
            lead + r"(?:me\s+)?(?:(?:some|a|my)\s+)?" + duration
            + r"(?:(?:coding|development|dev|focus)\s*(?:time|work|block|session)?\s*)"
            + purpose + r"$", raw, _re.I)
        if match is None:
            match = _re.search(
                lead + r"(?:me\s+)?(?:some\s+|a\s+)?" + duration +
                r"(?:for|on|to)\s+(?P<goal2>.+?)\s*$", raw, _re.I)
            if match is None or not match.group("dur"):
                return None

        goal = (self._arg(match.groupdict().get("goal")
                          or match.groupdict().get("goal2") or "")
                or "Coding time")
        minutes = 90
        if match.group("dur"):
            minutes = int(match.group("dur"))
            if _re.match(r"h", match.group("unit") or "", _re.I):
                minutes *= 60
        return Intent("schedule_coding", tool="schedule_coding",
                      params={"goal": goal, "duration_minutes": minutes})

    def _coding_agent(self, text: str, raw: str) -> Optional[Intent]:
        """Ask the coding agent to do something, in a workspace.

        "continue in claude code" and "ask claude to fix the tests" were both unanswered:
        the first fell to the project rule, which declines, and the second to nothing at
        all. The prompt is everything after the verb, because that is what the user typed
        and rewriting it would hide what the agent was actually told.
        """
        resume_only = re.search(
            rf"\b(?:continue|resume|carry\s+on)\b[\s\w]*?\b(?:in\s+)?{self._AGENT_NAMES}\b",
            raw, re.I)
        if resume_only and not re.search(r"\b(?:to|for)\s+\w", raw[resume_only.end():], re.I):
            return Intent("run_coding_agent", tool="run_coding_agent",
                          params={"prompt": "Continue where you left off.", "resume": True},
                          confidence=0.85)

        asked = re.search(
            rf"\b(?:ask|tell|have|make|get)\s+{self._AGENT_NAMES}\s+(?:to\s+)?(.+)$", raw, re.I)
        if asked is None:
            asked = re.search(rf"\bin\s+{self._AGENT_NAMES}\s+(.+)$", raw, re.I)
        if asked is None:
            return None
        prompt = asked.group(1).strip(" .!").strip()
        if not prompt:
            return Intent("run_coding_agent", note="What should I ask the coding agent to do?")
        return Intent("run_coding_agent", tool="run_coding_agent",
                      params={"prompt": prompt}, confidence=0.85)

    def _vantage_self(self, text: str, raw: str) -> Optional[Intent]:
        """Questions about Vantage itself, answered by Vantage rather than by a proxy.

        Measured before this rule existed: "are you running" reached the application probe for
        an installed app literally named "You", and "what is the current task" reached the
        general-knowledge question tool. Both produced a confident answer to the wrong
        question, which is worse than the silence the other four sentences got.

        The boundary kept on purpose: "is Vantage running?" stays with the application probe,
        because that sentence asks whether the program is up and the probe proves it from the
        bundle and the process table. "is Vantage healthy" asks for operating state, which only
        the running process can report.
        """
        if re.search(r"\b(stop|cancel|abort|kill)\s+(the\s+|my\s+)?(current|this|running|"
                     r"active)\s+(task|job|operation)\b", text):
            return Intent("stop_task", tool="stop_current_task", confidence=0.9)
        if re.search(r"\b(stop|cancel)\s+what\s+you(?:'?re|\s+are)\s+doing\b", text):
            return Intent("stop_task", tool="stop_current_task", confidence=0.9)

        if re.search(r"\b(pending|awaiting|waiting|outstanding)\s+"
                     r"(confirmation|confirmations|approval|approvals|request|requests)\b", text) \
                or re.search(r"\b(confirmations?|approvals?|requests?)\s+(?:are\s+)?"
                             r"(waiting|pending)\b",
                             text) \
                or re.search(r"\bwhat\s+(needs|actions?\s+need|require[s]?)\s+my\s+"
                             r"(confirmation|approval)\b", text) \
                or re.search(r"\bdo\s+i\s+have\s+any\s+(pending|approvals|confirmations)\b",
                             text):
            return Intent("pending_confirmations", tool="get_pending_confirmations",
                          confidence=0.9)

        if re.search(r"\b(install|add|enable|turn\s+on|set\s+up)\s+(the\s+|a\s+)?"
                     r"login\s+item\b", text) \
                or re.search(r"\b(install|enable)\s+(vantage|it)\s+at\s+login\b", text):
            # The unambiguous command. It is confirmation-gated in the registry, so this route
            # cannot execute anything until a person approves the card -- which is the same
            # control every other gated capability uses.
            return Intent("install_persistence", tool="enable_host_persistence", confidence=0.9)

        if re.search(r"\b(remove|uninstall|disable|delete|turn\s+off)\s+(the\s+|a\s+)?"
                     r"login\s+item\b", text) \
                or re.search(r"\b(remove|uninstall)\s+(vantage|it)\s+from\s+login\b", text):
            return Intent("remove_persistence", tool="disable_host_persistence", confidence=0.9)

        if re.search(r"\b(login|log\s+in|boot|startup|start[\s-]+up)\s+item\b", text) \
                or re.search(r"\b(start|starts|starting|launch|launches|run|runs)\b[^?!.]*\b"
                             r"(at|on)\s+(login|log\s+in|boot|startup)\b", text) \
                or re.search(r"\bwhen\s+i\s+(close|quit)\s+(the\s+)?(window|app|laptop)\b", text) \
                or re.search(r"\bpersist(?:ence|ent)?\b", text):
            # Only ever the read. "start vantage on login" reads like an instruction and still
            # gets a state report, because a mis-parsed sentence must not be able to install
            # something that runs when nobody is watching -- that change needs a person pressing
            # the control and confirming it.
            return Intent("host_persistence", tool="get_host_persistence_status", confidence=0.9)

        if re.search(r"\b(can\s+you|are\s+you\s+able\s+to|do\s+you)\b[^?!.]*"
                     r"\b(control|drive|operate|click|use|reach)\b[^?!.]*"
                     r"\b(computer|desktop|screen|laptop|mouse|trackpad|keyboard)\b", text) \
                or re.search(r"\b(can\s+you|are\s+you\s+able\s+to)\b[^?!.]*"
                     r"\b(type|click)\b[^?!.]*\b(screen|desktop|computer|laptop|for\s+me)\b", text) \
                or re.search(r"\b(desktop|accessibility)\s+(control|access|permission|state)\b", text) \
                or re.search(r"\b(is|are)\s+desktop\s+control\b[^?!.]*"
                     r"\b(available|enabled|on|working|granted)\b", text):
            # Only ever the read. "Can you click my save button" is a question about whether this
            # is possible, and the answer is a permission state -- the click itself needs a person
            # approving a card, because its target is whatever happens to be focused on their
            # screen. Spec 26.2: model and sentence output is data, never authority.
            return Intent("desktop_control_state", tool="get_desktop_control_state",
                          confidence=0.9)

        if re.search(r"\bwhat\s+(is|are)\s+your\s+(status|health|state|condition|mode)\b", text) \
                or re.search(r"\bwhat\s+mode\s+are\s+you\s+in\b", text) \
                or re.search(r"\bare\s+you\s+(running|up|alive|ok|okay|healthy|there|"
                             r"connected|online)\b", text) \
                or re.search(r"\b(vantage|jarvis)\s+(status|health|state)\b", text) \
                or re.search(r"\bis\s+(vantage|jarvis)\s+(healthy|up|ok|okay|working)\b", text) \
                or re.search(r"\bhow\s+is\s+(vantage|jarvis)\s+doing\b", text):
            return Intent("vantage_status", tool="get_vantage_status", confidence=0.9)

        if re.search(r"\b(current|active|running)\s+(task|job)\b", text) \
                or re.search(r"\bwhat\s+are\s+you\s+(doing|working\s+on)\b", text) \
                or re.search(r"\bany\s+task\s+(running|in\s+progress)\b", text):
            return Intent("current_task", tool="get_current_task", confidence=0.9)

        # A bare follow-up to the thing that just ran. "cancel that" and
        # "did it work" name no application and no file, so without these they
        # fell through to `unknown` -- which, after a voice turn, is a dead end
        # for the user. They map onto the WINDOW-INDEPENDENT read tool that
        # already exists; they do not execute anything, and stopping a mission
        # still goes through `stop_task` above.
        if re.fullmatch(r"(?:please\s+)?(?:cancel|stop|abort)\s+(?:that|this|it)\.?",
                        text):
            return Intent("stop_task", tool="stop_current_task", confidence=0.85)
        if re.search(r"\b(?:did\s+it\s+work|did\s+that\s+work|was\s+it\s+done)\b", text) \
                or re.search(r"\bwhat\s+happened\s+(?:in\s+)?(?:my\s+)?last\s+task\b", text) \
                or re.search(r"\bshow\s+(?:me\s+)?the\s+(?:result|receipt)\b", text):
            return Intent("current_task", tool="get_current_task", confidence=0.85)

        # "why didn't it work" asks for a cause, which only the mission record
        # can answer. Routed to the diagnosis tool rather than the model so the
        # reply is read from recorded steps instead of being generated.
        if re.search(r"\bwhy\s+(?:did|didn'?t|doesn'?t|isn'?t|wasn'?t)\b.*\b(?:work|fail|"
                     r"save|open|close|run|stop)\b", text) \
                or re.search(r"\bwhy\s+(?:are|were)\s+you\s+(?:waiting|stuck|blocked)\b", text) \
                or re.search(r"\bwhat\s+(?:went\s+wrong|failed)\b", text):
            return Intent("explain_failure", tool="explain_last_failure", confidence=0.85)

        if re.search(r"\brecent\s+(actions?|activity|history|commands?)\b", text) \
                or re.search(r"\bwhat\s+did\s+you\s+(just\s+)?(do|last\s+do)\b", text) \
                or re.search(r"\bwhat\s+happened\s+(recently|lately)\b", text) \
                or re.search(r"\bwhat\s+was\s+the\s+last\s+(command|action|thing)\b", text) \
                or re.search(r"\b(show|list)\s+(me\s+)?(the\s+)?(last|recent)\s+"
                             r"(actions?|commands?|activity)\b", text):
            return Intent("recent_activity", tool="get_recent_activity", confidence=0.9)
        return None

    def _frontmost_app(self, text: str, raw: str) -> Optional[Intent]:
        """"show the current application" -- one application, not the list of them.

        ``get_running_applications`` answers "what applications do you know about", which is a
        different question, and §6 asks this one. Kept apart so a person asking about the
        window in front does not get a catalogue back.
        """
        if re.search(r"\b(current|front|frontmost|active)\s+(application|app|window)\b", text):
            return Intent("frontmost_app", tool="get_frontmost_application")
        if re.search(r"\b(which|what)\s+(app|application)\s+(is|’s|')?\s*(in front|frontmost|"
                     r"active|open now)\b", text):
            return Intent("frontmost_app", tool="get_frontmost_application")
        return None

    def _open_standard_folder(self, text: str, raw: str) -> Optional[Intent]:
        """§6's four folders, through an allowlist rather than a path.

        The folder word has to be the whole object of the sentence: matching ``downloads``
        anywhere in the text would turn "open downloads.zip" into a folder request. This is
        also why it is not an alias of ``open_project_folder`` -- one reveals a directory in
        Finder, the other hands a workspace to an editor, and Phase 49a refused to blur them.
        """
        m = re.search(r"\b(?:open|show|go to|reveal)\s+(?:my\s+|the\s+)?"
                      r"(?P<folder>desktop|downloads?|documents?|pictures?|home folder|home)"
                      r"\s*(?:folder)?\s*$", text)
        if not m:
            return None
        folder = m.group("folder")
        #: The parameter space is the tool's allowlist, so the canonical key is built here
        #: rather than passed through and normalised later -- "my home folder" and "home"
        #: must not become two different values in a stored action.
        for filler in (" folder", " directory"):
            if folder.endswith(filler):
                folder = folder[: -len(filler)].strip()
        key = {"download": "downloads", "document": "documents", "picture": "pictures"}.get(
            folder, folder)
        return Intent("open_standard_folder", tool="open_user_folder",
                      params={"folder": key}, confidence=0.9)

    def _open_url(self, text: str, raw: str) -> Optional[Intent]:
        """A site to open, as opposed to a file or an app.

        Before this rule existed, `open github.com` was read as a file and the planner
        built `<home>/github.com`. A domain-shaped token is the signal; a known name plus
        the word "site" is the other one. Anything else is left alone -- guessing a
        domain points a logged-in browser at a stranger's address.
        """
        from ..tools.browser import NAMED_SITES, normalise

        #: Suffixes that look like a domain but are a document on disk.
        extensions = {"pdf", "txt", "md", "doc", "docx", "xls", "xlsx", "csv", "pptx",
                      "png", "jpg", "jpeg", "gif", "heic", "zip", "tar", "gz", "json",
                      "yaml", "yml", "py", "js", "ts", "rb", "go", "rs", "mp4", "mov",
                      "mp3", "wav", "log", "ini", "env", "sql", "ipynb", "tsx"}
        if not re.search(r"\b(?:open|go to|navigate to|visit|bring up|pull up)\b", text):
            return None

        browser = ""
        named = re.search(r"\bin\s+(chrome|safari|firefox|edge|brave|browser|"
                          r"web browser|google chrome)\b", text)
        if named:
            browser = named.group(1)

        candidates = re.findall(r"(?:[\w-]+\.)+[a-z]{2,}(?:[/?#][\w./?%&=#-]*)?", raw, re.I)
        for token in candidates:
            head = token.split("/")[0]
            suffix = head.rsplit(".", 1)[-1].lower()
            if suffix in extensions:
                continue
            #: `.app` is a bundle, not a gTLD. `open Mail.app` reached this rule, saw a
            #: domain-shaped token, and built https://Mail.app -- an address this project's
            #: own browser module says never to invent. Leave it to the application rule.
            if suffix == "app":
                return None
            url, problem = normalise(token)
            if url:
                params = {"url": url}
                if browser:
                    params["browser"] = browser
                return Intent("open_url", tool="open_url", params=params)

        site = re.search(r"\b(?:the\s+)?(?P<name>[\w ]+?)\s+(?:site|website|webpage|page)\b",
                         text)
        if site:
            url, _problem = normalise(site.group("name"))
            if url:
                params = {"url": url}
                if browser:
                    params["browser"] = browser
                return Intent("open_url", tool="open_url", params=params)

        #: Longest name first. Iterating the dict in insertion order let "open Google
        #: Calendar" match "google" and return https://www.google.com: the brand-qualified
        #: name lost the half that identified which Google thing was asked for.
        web_signal = re.search(r"\b(?:web|website|site|online|browser|chrome|safari|firefox|"
                               r"brave|edge|google|gmail)\b", text)
        for name in sorted(NAMED_SITES, key=len, reverse=True):
            if not re.search(rf"\b{re.escape(name)}\b", text):
                continue
            #: An installed application wins over a site of the same word, unless the sentence
            #: asks for the web. "open Calendar" means the app on the laptop; "open the
            #: calendar site" and "open Google Calendar" mean the page. The lookup is exact --
            #: a fuzzy one would resolve "gmail" inside the Mail app and take gmail away.
            if self.apps.get(name) is not None and not web_signal:
                return None
            params = {"url": NAMED_SITES[name]}
            if browser:
                params["browser"] = browser
            return Intent("open_url", tool="open_url", params=params)
        return None

    def _read_page(self, text: str, raw: str) -> Optional[Intent]:
        """"What's on this page?" -- a read the browser could do but had no route to.

        ``open_url`` opens a page in the person's own browser and reports the
        URL it landed on. It never reads the page: a browser opened on a
        logged-in session is the user's, and reading it would be reaching into
        a session the person did not offer. The controlled browser is
        different -- it is a session Vantage opened, with no cookies of the
        user's and no access to anything they are signed into.

        So "read the page", "what does this page say" and "summarise this
        page" go to the controlled browser, and the answer is its text. The
        text is the page's words, and the tool marks them as such.

        Only the *read* is routed. Clicking and typing are confirmation-gated
        and carry no route, for the same reason ``click_desktop`` has none: a
        sentence must not be able to make the machine act. A model may
        *propose* a click (R2.3), and a person presses the button.
        """
        if not re.search(r"\b(read|scrape|extract|what does|summar\w+|contents? of|"
                         r"text on|page says)\b", text):
            return None

        from ..tools.browser import NAMED_SITES, normalise

        #: Suffixes that look like a domain but are a document on disk. "read
        #: notes.txt" is a request to read a file, not to open a web page --
        #: and the file route is the one that can do it.
        extensions = {"pdf", "txt", "md", "doc", "docx", "xls", "xlsx", "csv", "pptx",
                      "png", "jpg", "jpeg", "gif", "heic", "zip", "tar", "gz", "json",
                      "yaml", "yml", "py", "js", "ts", "rb", "go", "rs", "mp4", "mov",
                      "mp3", "wav", "log", "ini", "env", "sql", "ipynb", "tsx"}

        candidates = re.findall(r"(?:[\w-]+\.)+[a-z]{2,}(?:[/?#][\w./?%&=#-]*)?", raw, re.I)
        domain_candidates = []
        for token in candidates:
            head = token.split("/")[0]
            if head.rsplit(".", 1)[-1].lower() in extensions:
                continue  # a document on disk, not a page on the web
            url, problem = normalise(token)
            if url:
                domain_candidates.append(url)

        # A page-word is required only when there is no domain to go on. A
        # sentence that names a domain is asking about *that* page, whatever
        # verb it uses -- "what does example.com say" is a request to read a
        # page, not a request to read a file called "example.com" (which is
        # what the file route built, inside a workspace that does not exist).
        if not domain_candidates and not re.search(
                r"\b(page|site|website|web|article|post|link|html)\b", text):
            return None

        for url in domain_candidates:
            return Intent("browser_open", tool="browser_open", params={"url": url})

        web_signal = re.search(r"\b(?:web|website|site|online)\b", text)
        for name in sorted(NAMED_SITES, key=len, reverse=True):
            if not re.search(rf"\b{re.escape(name)}\b", text):
                continue
            if self.apps.get(name) is not None and not web_signal:
                return None
            return Intent("browser_open", tool="browser_open",
                          params={"url": NAMED_SITES[name]})
        return None

    def _find_and_open(self, text: str, raw: str) -> Optional[Intent]:
        """"Find my resume and open it" -- two capabilities the build had, and no route.

        Both verbs must be present, so a plain search still reaches `search_files` and a
        plain open still reaches the file and app rules.
        """
        if not re.search(r"\b(?:find|locate|search(?:\s+for)?|look for)\b", text):
            return None
        if not re.search(r"\b(?:open|show)\b", text):
            return None
        got = re.search(
            r"\b(?:find|locate|search(?:\s+for)?|look\s+for)\s+(?:me\s+)?(?:my|the)?\s*"
            r"(?P<what>[\w .,-]+?)\s+(?:and\s+|then\s+)?(?:open|show)(?:\s+it)?\b",
            raw, re.I)
        query = got.group("what").strip() if got else ""
        if not query:
            return None
        return Intent("find_and_open", tool="find_and_open", params={"query": query})

    def _open_app(self, text: str, raw: str) -> Optional[Intent]:
        # "open that email" is a request to read the message, not to launch Mail.
        # The app resolver matches loosely enough that "that email" found the Mail
        # application, so the app rule claimed a mail request -- the same
        # cross-domain theft the calendar and file routes already guard against.
        # A demonstrative plus a mail noun never names an application.
        if re.search(r"\b(?:that|this|the)\s+(?:email|e-mail|message)\b", raw, re.I):
            return None

        # "open the readme in TextEdit" / "open report.pdf in Preview" -- a *document*
        # opened in a named app. Checked before the folder branch, which only passes a
        # folder to an editor and would otherwise let this fall through to read_file.
        doc = re.search(
            r"\bopen\b\s+(?:the\s+|my\s+)?(?P<doc>.+?)\s+(?:in|with)\s+(?P<app>.+?)\s*$",
            raw, re.I)
        if doc and doc.group("doc").strip():
            app_name = self._arg(doc.group("app"))
            app_spec = self.apps.resolve(app_name)
            document = self._arg(doc.group("doc"))
            if document and not Path(os.path.expanduser(document)).is_dir():
                looks_like_doc = bool(
                    re.search(r"\.[A-Za-z0-9]{1,8}$", document)
                    or re.search(r"\b(file|document|readme|pdf|report|note)\b", text, re.I))
                # A named app that is registered resolves to its key; a name that is
                # not registered still goes to open_document, which fails honestly
                # ("I could not find an application matching …") instead of the
                # sentence being silently read as a file read.
                if looks_like_doc and app_name:
                    return Intent("open_document", tool="open_document",
                                  params={"path": document,
                                          "app": app_spec.key if app_spec else app_name},
                                  confidence=0.85)

        # "open folder <path> in vscode" / "open <path> in vscode"
        m = re.search(
            r"\bopen\b\s+(?:the\s+)?(?:folder|directory)?\s*(?P<path>.+?)\s+in\s+(?P<app>.+?)\s*$",
            raw, re.I)
        if m and m.group("path").strip():
            app = self.apps.resolve(self._arg(m.group("app")))
            if app is not None and self._folder_worth_passing(app, m.group("path")):
                return Intent("open_app", tool="open_application",
                              params={"app": app.key, "path": m.group("path").strip()},
                              confidence=0.9)

        # "open vscode with/in/to <folder>"
        m = re.search(
            rf"\b(?:{self._launch_pattern()})\b\s+(?P<app>.+?)\s+(?:with|in|on|to|at|for)\s+"
            r"(?P<path>.+?)\s*$", raw, re.I)
        if m:
            app = self.apps.resolve(self._arg(m.group("app")))
            if app is not None and self._folder_worth_passing(app, m.group("path")):
                return Intent("open_app", tool="open_application",
                              params={"app": app.key, "path": m.group("path").strip()},
                              confidence=0.9)

        captured = self._match(rf"\b(?:{self._launch_pattern()})\b\s+(.+)", raw)
        if captured is None:
            return None
        #: A path is not an application. `open ~/notes/vantage-notes.txt` resolved to the
        #: Notes app because "notes" is one of its aliases and appears inside the filename.
        spoken = captured.strip()
        if spoken.startswith(("/", "~", "./", "../")) or Path(spoken).exists():
            #: A folder is not a document. `open /tmp/my-project` reaching read_file
            #: answered "File not found" about a directory that exists; the editor route
            #: is the one that can actually open it, and verify that it did.
            if Path(spoken).expanduser().is_dir():
                return Intent("open_folder", tool="open_project_folder",
                              params={"project": spoken}, confidence=0.9)
            return None
        app = self.apps.resolve(self._arg(captured))
        if app is None:
            return None
        return Intent("open_app", tool="open_application", params={"app": app.key}, confidence=0.9)

    @staticmethod
    def _folder_worth_passing(app, candidate: str) -> bool:
        """Only treat the second half of the sentence as a folder when it plausibly is one.

        "open chrome with google.com" names a page, and handing that to open_app as a
        directory would turn a working launch into a refusal. An editor is the case where
        the user means a folder; a path prefix is unambiguous either way.
        """
        value = str(candidate).strip()
        if not value:
            return False
        if value.startswith(("/", "~", "./", "../")):
            return True
        return getattr(app, "category", "") == "development"

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

        # "research vector databases" is the plain verb, and it was landing on
        # arxiv: the word "research" alone is not a request for academic papers.
        # Only a *paper* word (papers, literature, arxiv, citations) means the
        # paper index; the bare verb means look it up on the web.
        paper_word = re.search(
            r"\b(papers?|literature|citation|citations|cite|references?|arxiv|"
            r"related work|state of the art)\b", text)
        if not paper_word and re.search(r"\b(research|look\s+up|find\s+out)\b", text):
            query = re.sub(r"^\s*(?:please\s+)?(?:research|look\s+up|find\s+out\s+about)\s+",
                           "", raw, flags=re.I).strip(" ?.!")
            if not query:
                return None
            return Intent("search_web", tool="search_web", params={"query": query})

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
        # A screen reference is a *screen* noun -- or a "what's on my screen"
        # question that names one. "what's on my" on its own used to count, and
        # this rule runs before every rule below it in the chain, so "what's on my
        # calendar", "what's on my plate" and "what's on my clipboard" all took a
        # screenshot and answered with screen text instead of the calendar, the
        # plan, or the clipboard. The phrase only means "look at the screen" when
        # the sentence says which surface, so the surface has to be named.
        screen = re.search(r"\b(screen|screenshot|display)\b", text) or re.search(
            r"\bwhat(?:'s|\s+is)?\s+(?:on|in)\s+(?:the\s+|my\s+|this\s+)?"
            r"(?:screen|display|monitor|desktop)\b",
            text,
        )
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

        def _title(source: Optional[str] = None) -> str:
            """The request's subject, used as the artifact title.

            Simpler and more robust than keyword-specific capture: drop the
            leading imperative verb and any article, and keep the rest.
            "draw a diagram of the JARVIS pipeline" -> "diagram of the JARVIS
            pipeline"; "design a dashboard mockup" -> "dashboard mockup".

            ``source`` is the sentence with any inline parts list removed, so a
            flowchart titled from "… for releases with draft, build, publish" is
            "flowchart for releases" and not its own steps.
            """
            subject = re.sub(
                r"^\s*(?:please\s+)?(?:create|make|design|draw|write|build|generate|"
                r"mock up|produce|prepare)\s+", "", raw if source is None else source,
                count=1, flags=re.I,
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

        # --- authoring guard (Phase 28) --------------------------------------
        # Everything below this line *creates an artifact*. Until now the noun
        # alone decided: the regex matched "readme", "guide", "document", so
        # "read the readme", "show me the readme" and "open README.md" each wrote
        # a new file and reported it as done. A sentence now has to be asking to
        # author, judged by which verb leads it, so "document the parser" still
        # authors while "what does the document say" does not.
        if not self._authoring_request(text):
            return None

        # The sentence with any inline parts list cut off, so an artifact title
        # is the subject and not the whole request.
        stemmed = self._parts_stem(raw)

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
            title = _title(stemmed)
            if not title:
                return Intent("create_flowchart", note="What should I call the flowchart?")
            steps = self._listed_parts(raw)
            if not steps:
                return Intent("create_flowchart", note=(
                    "Which steps does the flowchart show, in order? "
                    "Like: request, review, approve."))
            return Intent(
                "create_flowchart", tool="create_flowchart",
                params={"title": title, "steps": steps},
            )

        # --- ui ------------------------------------------------------------
        if ui:
            title = _title()
            if not title:
                return Intent("create_ui_concept", note="What should I call the concept?")
            return Intent("create_ui_concept", tool="create_ui_concept", params={"title": title})

        # --- architecture diagram -----------------------------------------
        title = _title(stemmed)
        if not title:
            return Intent(
                "create_architecture_diagram", note="What should I call the diagram?"
            )
        components, links = self._diagram_args(parts) if (parts := self._listed_parts(raw)) else ("", "")
        if not components:
            return Intent("create_architecture_diagram", note=(
                "Which components should the diagram show? "
                "Like: api, worker, database."))
        params = {"title": title, "components": components}
        if links:
            params["links"] = links
        return Intent(
            "create_architecture_diagram",
            tool="create_architecture_diagram",
            params=params,
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
        open_request = bool(
            re.search(r"\bopen\b", text) and re.search(r"\b(workspace|repo|repository)\b", text)
        )
        # Every action below except opening a directory is scoped to a workspace id, and
        # most sentences do not carry one. Emitting ``workspace_id=""`` used to hand the
        # user the gate's vocabulary -- measured, "list the workspace" was answered
        # ``I cannot do that: missing required parameter 'workspace_id'`` -- which is
        # 29-B arriving through a route that built its dict in a variable, the exact
        # blind spot 29-C admitted about the static sweep.
        if not workspace and not open_request:
            return Intent(
                "coding",
                note="Which workspace? Give me its id (WS-…).",
                clarification=True,
                missing_identifiers=("workspace_id",),
            )

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
        if open_request:
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

        # The default branch is an inspect, and "workspace" appears in sentences that
        # asked for something else entirely: measured, "close the workspace WS-…" returned
        # "tmp16ycb2dy: 0 source file(s)…" and recorded a success. A route that falls
        # through to a default yields to a sentence that names a specific capability --
        # the rule Phase 31 wrote for advertising routes, applied here to a catch-all.
        if any(spec.name != "inspect_workspace" for spec in self._capability_matches(text)):
            return None

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
        # --- weekly report --------------------------------------------------
        # Ahead of the scheduling branch: "prepare my weekly report" was being
        # read as a request to *schedule* something and answered "When should I
        # do that?".
        if re.search(r"\b(weekly|week\s+in\s+review)\s+report\b", text, re.I) or re.search(
            r"\breport\s+(?:on\s+)?(?:my|the)\s+(?:week|progress)\b", text, re.I
        ) or re.search(r"\bhow\s+did\s+(?:my|the)\s+week\s+go\b", text, re.I):
            m = re.search(r"\b(\d+)\s+days?\b", text)
            return Intent("weekly_report", tool="weekly_report",
                          params={"days": int(m.group(1)) if m else 7})

        # --- the briefing itself ------------------------------------------
        # "every"/"each" makes it a schedule; "daily briefing" on its own is a
        # request to produce one now.
        if re.search(r"\b(briefing|brief me|catch me up)\b", text) and not re.search(
            r"\bevery\b|\beach\b", text
        ):
            # "brief me on the Roadmap sync" is a meeting briefing, not the day's.
            # This rule runs before the calendar rules, so without this check the
            # named meeting was silently answered with the daily briefing.
            on = re.search(r"\bbrief\s+me\s+(?:on|about|for)\s+(?:the\s+|my\s+)?(?P<what>.+?)\s*$",
                           text, re.I)
            if on:
                what = self._arg(on.group("what"))
                if re.search(r"\b(meeting|sync|call|standup|stand-up|1:1|review|interview)\b",
                             what, re.I):
                    return Intent("brief_meeting", tool="brief_meeting",
                                  params={"meeting": what})
            return Intent("daily_briefing", tool="daily_briefing", params={})

        # --- create a schedule ---------------------------------------------
        # "schedule two hours for project work tomorrow" is a request to *make*
        # one. The word "schedule" alone used to fall into the manage branch
        # above, so it listed the (empty) schedule set instead, answered
        # "You have nothing scheduled", and recorded success -- measured in the
        # Phase 15 acceptance sweep, the user was shown a VERIFIED chip for a
        # block that was never created.
        # "schedule" + a noun that is explicitly a calendar concept (meeting,
        # event, appointment) is a calendar request, not a scheduler one. Without
        # this, "schedule a meeting with Alice at 3pm" was caught here and
        # turned into a schedule_task intent instead of reaching the _calendar
        # route that actually maps it to create_event.
        is_calendar_noun = bool(re.search(r"\b(meeting|event|appointment)\b", text))
        create_requested = bool(
            re.match(r"(?:can you |could you |please )?schedule\s+\S", text)
            and not is_calendar_noun
        )

        # --- manage existing schedules -------------------------------------
        if not create_requested and not is_calendar_noun and (
            re.search(r"\b(scheduled|schedules?|recurring|repeating)\b", text)
            or re.search(r"\bwhat.*(scheduled|recurring)\b", text)
        ):
            schedule_id = self._match(r"\b([A-Z]+-[0-9a-f]{6,})", raw) or ""
            # Cancelling, pausing and resuming all name one schedule, and a sentence
            # that does not carry its id used to emit ``schedule_id=""`` -- the gate
            # then answered the user with "missing required parameter 'schedule_id'",
            # which is 29-B through a route that built its params in a variable. The
            # id is offered rather than guessed because listing them is one sentence
            # away, and that sentence is reachable.
            if not schedule_id and re.search(r"\b(cancel|delete|remove|stop|pause|resume|unpause|restart)\b", text):
                return Intent(
                    "schedule_needs_id",
                    note=(
                        "Which schedule? Give me its id (SCHED-…), or say "
                        '"list my schedules" to see them.'
                    ),
                    clarification=True,
                    missing_identifiers=("schedule_id",),
                )
            if re.search(r"\b(cancel|delete|remove|stop)\b", text):
                return Intent(
                    "cancel_schedule",
                    tool="cancel_schedule",
                    params={"schedule_id": schedule_id},
                )
            if re.search(r"\bpause\b", text):
                return Intent(
                    "pause_schedule",
                    tool="pause_schedule",
                    params={"schedule_id": schedule_id},
                )
            if re.search(r"\b(resume|unpause|restart)\b", text):
                return Intent(
                    "resume_schedule",
                    tool="resume_schedule",
                    params={"schedule_id": schedule_id},
                )
            return Intent("list_schedules", tool="list_schedules", params={})

        # --- create a schedule ---------------------------------------------
        recurring = re.search(r"\b(every|each|daily|weekly)\b", text)
        # "remind me" is not the only way people ask for one. "set a reminder",
        # "notify me at 8 PM" and "alert me" all name the same capability and all
        # fell through to "I understood the words but not the goal".
        reminder = re.search(
            r"\bremind\s+me\b|\b(?:set|create|add|make)\s+(?:me\s+)?(?:a\s+)?reminder\b|"
            r"\b(?:notify|alert|ping)\s+me\b|\breminder\s+(?:for|to|at|about)\b",
            text)

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
            # A one-off reminder is the commonest request there is, and "in 20
            # minutes" was not a shape this looked for -- so a sentence that said
            # exactly when got asked to say when.
            schedule_text = self._match(
                r"\b(in\s+(?:\d+|a|an|half\s+an)\s*(?:minutes?|mins?|hours?|hrs?))", raw)

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
        # "every morning at 8 check the build" schedules the *action the sentence
        # names*, not a reminder about nothing. Only the phrase after the time is
        # scanned, so the cadence words cannot be mistaken for the action.
        scheduled_action = self._scheduled_action(text, raw)
        if scheduled_action is not None:
            action, params = scheduled_action
        elif re.search(r"\b(briefing|brief me)\b", text):
            action = "briefing"
        elif re.search(r"\bproject (progress|status|summary)\b", text):
            action = "project_summary"
        elif re.search(r"\bcalendar\b", text):
            action = "list_events"
            params = {"date": "today"}
        elif reminder:
            message = self._match(r"\bremind me\b.*?\bto\s+(.+)$", raw)
            if message:
                # The sentence names both what and when; `to (.*)` swallows both,
                # so "remind me to stretch in 20 minutes" filed a reminder whose
                # text was "stretch in 20 minutes". The time phrase is stripped
                # from the message, not from the schedule.
                text_part = re.sub(
                    r"\s*\b(?:in\s+(?:\d+|a|an|half\s+an)\s*(?:minutes?|mins?|hours?|hrs?)|"
                    r"at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|"
                    r"tomorrow(?:\s+morning|\s+afternoon|\s+evening)?|"
                    r"every\s+\w+(?:\s+\w+)?)\s*$",
                    "", message, flags=re.I).strip()
                if text_part:
                    params = {"message": text_part}

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
                    # An instruction, not a question -- but the missing thing is a draft,
                    # and the user can supply it, so it is a clarification either way.
                    clarification=True,
                )
            return Intent("send_email", tool="send_email", params={"draft_id": pending["draft_id"]})

        has_email_noun = re.search(r"\b(e-?mails?|mail|inbox|unread)\b", text)
        # "mark that email as read" / "mark the invoice email as unread" ask to
        # change a message's read state -- an operation the mail backend has no
        # method for. Without this guard, "as read" is stolen by the read-
        # referent branch (which READS the email and reports its contents, as if
        # the mark happened) and "as unread" by the unread-search branch (which
        # returns a search result that sounds like the mark took effect). Placed
        # before both, and gated on an email noun so a non-email "mark ... as
        # read" is not intercepted.
        if has_email_noun and re.search(
                r"\bmark\b.*\bas\b\s*(?:read|unread)\b", text, re.I):
            return self._email_not_supported("mark as read or unread")
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
                    clarification=True,
                )

        # --- a reply to the message the conversation just dealt with -------
        # "reply to that one", "answer the second one", "write back" continue
        # the workflow with a message already identified: the recipient is the
        # sender the selection recorded, and the result is a DRAFT. The send
        # rule is untouched -- a reply still needs the person's explicit send
        # and confirmation; this only names who it should go to.
        if re.search(r"\b(?:reply|answer|respond|write back)\b", text):
            referent = self._email_referent(text, True)
            if referent is not None:
                return Intent(
                    "draft_email",
                    note="Resolving who to reply to.",
                    clarification=False,
                    referent=referent,
                    referent_body=self._email_reply_body(raw),
                    confidence=0.6,
                )

        # --- a reference to the message the conversation just dealt with ----
        # "read that one", "summarize it", "the second one", "what did they
        # say in that message?" name no message and -- in most phrasings -- no
        # email word either: the words point at the last read. The planner has
        # no memory of the conversation, so it carries the reference and the
        # orchestrator, which holds the per-session selection, turns it into a
        # read of a real uid. Checked before the noun gate ("summarize it"
        # names no medium) and before the send/draft branches (a read verb
        # never means send, so nothing here can authorise one).
        referent = self._email_referent(text, bool(has_email_noun))
        if referent is not None:
            return Intent(
                "read_email",
                note="Resolving the message you referred to.",
                clarification=False,
                referent=referent,
                confidence=0.6,
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

        # --- read one message by the name the person gave ------------------
        # "read the invoice email" names *the invoice*, not a message number,
        # and a read needs a uid. So this is a clarification that carries the
        # name: the orchestrator's identifier lookup searches the mailbox for
        # that name, and when exactly one message matches, the read is completed
        # with that message's id. Several matches stay a question, and a miss is
        # reported honestly rather than a guess being read. Reusing the identifier
        # resolution the calendar cancel path already uses, not a second search.
        read_named = re.search(
            r"\bread\s+(?:my\s+|the\s+|an?\s+)?(.+?)\s+(?:email|mail|message)\b",
            raw, re.I,
        )
        if read_named:
            named = self._email_search_term(read_named.group(1))
            if named:
                spec = self.tools.get("read_email")
                note = self._capability_question(spec, named) if spec else (
                    f"Which {named!r} message should I read?"
                )
                return Intent(
                    "read_email",
                    note=note,
                    clarification=True,
                    missing_identifiers=("uid",),
                    named_thing=named,
                    confidence=0.6,
                )

        # --- unread / new / important -------------------------------------
        # A recency filter is its own route: "what new emails did I get" names no
        # search verb, so it must not fall through to the decline below. It lists
        # the unread messages, exactly as before this route learned to carry a term.
        if re.search(r"\b(unread|new|important)\b", text):
            return Intent(
                "search_email",
                tool="search_email",
                params={"query": "", "unread_only": True, "limit": 10},
            )

        from_match = self._match(r"\bfrom\s+([A-Za-z][A-Za-z0-9 ._-]{1,40})$", raw)
        if from_match:
            # "from the billing team" names *billing team*; the article is how
            # the person talked, not part of the name -- and a search needle
            # with "the" in it would miss "billing-team@example.com".
            from_name = re.sub(r"^(?:my|our|the)\s+", "", from_match.strip(), flags=re.I)
            return Intent(
                "search_email",
                tool="search_email",
                params={"query": from_name, "limit": 10},
            )

        if re.search(r"\b(check|read|show|any|list|search|find|look)\b", text):
            # The thing the sentence searches for, if it names one. Without this
            # the mailbox was read as "recent messages" for every search phrase,
            # so "find emails about the invoice" answered with the last few
            # messages whatever they were about.
            return Intent(
                "search_email",
                tool="search_email",
                params={"query": self._email_search_term(raw), "limit": 10},
            )

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

    def _automation(self, text: str, raw: str) -> Optional[Intent]:
        """n8n automation: list approved workflows, or run one by name.

        Narrow on purpose. The words that trigger it are "n8n" and "workflow"
        (plus "automation" when it is paired with "run"); everything else keeps
        its existing behaviour, because "run" alone means "open an application"
        elsewhere in this chain and an automation rule that claimed it would
        break one of the most common sentences.

        The workflow is resolved against Vantage's OWN registry here, and the
        registry key -- never a name or id the model produced -- is what the tool
        receives. A sentence naming a workflow that is not registered is answered
        honestly with the list, rather than being passed to n8n, whose admin API
        this integration deliberately cannot reach.
        """
        from ..integrations import n8n as n8n_mod

        mentions_n8n = "n8n" in text
        mentions_workflow = "workflow" in text or "automation" in text
        if not (mentions_n8n or mentions_workflow):
            return None

        # --- list ---------------------------------------------------------
        if re.search(r"\b(list|show|what|which|available|approved|my)\b", text) \
                and not re.search(r"\b(run|start|execute|trigger|fire)\b", text):
            return Intent("list_n8n_workflows", tool="list_n8n_workflows", params={})

        # --- run ----------------------------------------------------------
        # A bare workflow name is a request to run it ("n8n echo", "echo
        # workflow"), which is how every registered tool must be reachable by
        # its own name. The verb is optional; what is *not* optional is that a
        # registry workflow is named, which the loop below enforces.
        verb = re.search(r"\b(run|start|execute|trigger|fire)\b", text) is not None

        # Match against the registry's keys and titles. Longest first, so
        # "daily agenda" wins over a bare "agenda".
        candidates = sorted(
            ((spec.key, spec.title.lower(), spec) for spec in n8n_mod.REGISTRY),
            key=lambda item: max(len(item[0]), len(item[1])), reverse=True)
        for key, title, spec in candidates:
            key_phrase = key.replace("_", " ")
            if re.search(rf"\b{re.escape(key)}\b", text) \
                    or re.search(rf"\b{re.escape(key_phrase)}\b", text) \
                    or (len(title) > 3 and title in text):
                params: Dict[str, Any] = {}
                # Pull only the inputs the sentence actually supplies. Anything
                # missing is the adapter's to refuse with a reason, not something
                # to invent here.
                if key == "echo":
                    quoted = re.search(r"[\"“']([^\"”']{1,500})[\"”']", raw)
                    if quoted:
                        params["message"] = quoted.group(1)
                    else:
                        # "run my echo workflow saying bananas": the text after
                        # "saying/with/containing" is the message. Without that
                        # cue there is no message in the sentence, and the
                        # adapter asks for one rather than echoing the
                        # workflow's own name back at the person.
                        said = re.search(
                            r"\b(?:saying|with(?:\s+the\s+text)?|containing|message)\b"
                            r"\s*[:\-]?\s*[\"“']?(.+?)[\"”']?\s*$", raw, re.I)
                        if said and said.group(1).strip():
                            params["message"] = said.group(1).strip(" .:;-")
                if key == "notify":
                    quoted = re.search(r"[\"“']([^\"”']{1,500})[\"”']", raw)
                    if quoted:
                        params["message"] = quoted.group(1)
                # The tool name is written out for each registry entry, not
                # only derived, so a reader of this file can see which
                # capabilities an automation sentence can reach. The registry
                # is closed (``n8n.REGISTRY``), and this list is the same set;
                # a key that is not here falls through to the honest
                # "no such workflow" answer below.
                tool_name = {
                    "echo": "n8n__echo",
                    "daily_agenda": "n8n__daily_agenda",
                    "notify": "n8n__notify",
                }.get(spec.key)
                if tool_name is None:
                    break
                return Intent(spec.key, tool=tool_name, params=params)

        # The sentence named a workflow Vantage does not have. Answer with what
        # it does have rather than letting the model guess an identifier — but
        # only when it asked to run one: "what is automation" is a question,
        # not a bad workflow name.
        if verb and (mentions_n8n or mentions_workflow):
            known = ", ".join(spec.title for spec in n8n_mod.REGISTRY)
            return Intent(
                "unknown_workflow",
                note=f"I do not have an approved workflow by that name. "
                     f"The approved ones are: {known}.",
                clarification=True,
            )
        return None

    def _planner(self, text: str, raw: str) -> Optional[Intent]:
        """The user's own tasks and their day (Epic 1).

        Runs *before* the calendar rule: "plan my day" is a question about tasks
        and time together, not a calendar listing, and the calendar rule would
        claim it and answer with events only.

        Deliberately narrow -- it names the planner's own vocabulary ("plan my
        day", "next task", "my tasks", "add a task", "mark ... done"). A generic
        "schedule a meeting" still goes to the calendar.
        """
        # --- Google Tasks: only when the sentence says "Google" ---------------
        #
        # These run first because they are the most explicit thing a person can
        # say: naming *which* task list. Without them, "add a task to Google
        # Tasks" fell into the local ``add_task`` rule below and wrote into the
        # Vantage database -- a duplicate of a record the person meant to keep
        # in Google, and not visible where they look for it.
        #
        # The trigger is the word "google" and nothing less. A sentence that
        # omits it keeps the local behaviour exactly as it was: two task lists
        # that silently swap on a phrasing change would be worse than the gap
        # this closes.
        if "google" in text:
            # "google" may come before OR after "task": "add a google task
            # to buy milk" and "add a task to Google Tasks buy milk" name the
            # same destination, and only the second used to match. The first
            # fell through to the registry's capability fallback, which found
            # *both* add_task and add_google_task and asked an ambiguity
            # question the person had already answered. The same gap made
            # "create a google task to call the dentist" reach nothing at all.
            google_add = (
                re.match(
                    r"^(?:please\s+)?(?:add|create|new|remember)\b(?:\s+(?:a|an|the))?\s+"
                    r"(?:google\s+)?task\b(?:\s+(?:to|in|for))?\s*(?:my\s+)?google\s+tasks?\b"
                    r"(?:\s+(?:called|named|to be))?\s*[:\-]?\s*(.+)$", raw, re.I)
                or re.match(
                    r"^(?:please\s+)?(?:add|create|new|remember)\b(?:\s+(?:a|an|the))?\s+"
                    r"google\s+task\b(?:\s+(?:to|in|for))?\s*[:\-]?\s*(.+)$", raw, re.I)
            )
            if google_add:
                title = self._arg(google_add.group(1))
                due = self._planner_due(raw)
                title = re.sub(r"\b(by|before|due)\s+\S+.*$", "", title, flags=re.I).strip(" ,.-")
                title = re.sub(r"\b(today|tomorrow|tonight)\b.*$", "", title, flags=re.I).strip(" ,.-")
                if not title:
                    return Intent("add_google_task", note="What should the Google task say?",
                                  clarification=True)
                return Intent("add_google_task", tool="add_google_task",
                              params={"title": title, "due": due or ""})

            # "find my overdue tasks", "what google tasks are overdue" --
            # answered from Google only when Google is named, and reported as a
            # *failed read* when the list cannot be read. The previous behaviour
            # answered a generic "find my overdue tasks" with "which app do you
            # keep tasks in?", which is a question rather than a failure but is
            # equally unhelpful when the connected account is right there.
            if re.search(r"\boverdue\b", text) \
                    and re.search(r"\bgoogle\s+tasks?\b|\bgoogle\b", text):
                return Intent("list_google_tasks", tool="list_google_tasks",
                              params={"show_completed": False, "overdue_only": True})

            if re.search(r"\b(list|show|what(?:'s| is| are)|my)\b.*\bgoogle\s+tasks?\b", text) \
                    and not re.search(r"\b(add|create|new|complete|finish|done)\b", text):
                return Intent("list_google_tasks", tool="list_google_tasks",
                              params={"show_completed": bool(
                                  re.search(r"\b(completed|done|all)\b", text))})

            if re.search(r"\b(mark|complete|finish|did|done with)\b", text) \
                    and re.search(r"\bgoogle\s+tasks?\b", text):
                # The reference is the sentence less the verbs that describe the
                # action; the task adapter matches it against the live list.
                ref = re.sub(
                    r"^(?:please\s+)?(?:mark|complete|finish|did|done with)\b", "",
                    raw, flags=re.I)
                ref = re.sub(r"\b(?:as\s+)?(?:done|complete|completed)\b", "", ref, flags=re.I)
                ref = re.sub(r"\b(?:in|on)\s+(?:my\s+)?google\s+tasks?\b", "", ref, flags=re.I)
                ref = ref.strip(" :-.,")
                if not ref:
                    return Intent("complete_google_task",
                                  note="Which Google task should I complete?",
                                  clarification=True)
                return Intent("complete_google_task", tool="complete_google_task",
                              params={"task_ref": ref})
        # --- start my day: the combined workflow ---------------------------
        if re.search(r"\b(start|begin)\s+(my|the)\s+day\b|\bstart the day\b", text):
            return Intent("start_day", tool="start_day", params={})

        # --- the day's plan -------------------------------------------------
        if re.search(r"\bplan\b.*\b(my|the)\s+day\b|\bplan my day\b|"
                     r"\bwhat should i (work on|do)\b.*\b(next|now)\b|"
                     r"\bwhat('s| is) (next|my next task|on my plate)\b|"
                     r"\bmy (daily )?plan\b", text):
            date = "tomorrow" if "tomorrow" in text else ("today")
            return Intent("plan_day", tool="plan_day", params={"date": date})

        # --- list tasks -----------------------------------------------------
        if re.search(r"\b(my|the|list|show|what are my)\s+tasks?\b|"
                     r"\btask list\b|\bto-?do list\b|\bmy to-?dos?\b", text) \
                and not re.search(r"\b(add|create|new|complete|finish|done)\b", text):
            return Intent("list_tasks", tool="list_tasks",
                          params={"include_done": bool(re.search(r"\b(completed|done|all)\b", text))})

        # --- add a task -----------------------------------------------------
        add = re.match(
            r"^(?:please\s+)?(?:add|create|new|remember)\b(?:\s+(?:a|an|the))?\s+task\b"
            r"(?:\s+(?:to|for|called|named))?\s*[:\-]?\s*(.+)$", raw, re.I)
        if add:
            title = self._arg(add.group(1))
            when = self._planner_due(raw)
            title = re.sub(r"\b(by|before|due)\s+\S+.*$", "", title, flags=re.I).strip(" ,.-")
            title = re.sub(r"\b(today|tomorrow|tonight)\b.*$", "", title, flags=re.I).strip(" ,.-")
            if not title:
                return Intent("add_task", note="What is the task?", clarification=True)
            return Intent("add_task", tool="add_task",
                          params={"title": title, "due": when, "priority": self._planner_priority(text)})

        # --- complete a task ------------------------------------------------
        if re.search(r"\b(mark|complete|finish|did|done with)\b", text) and re.search(r"\btask\b", text):
            return Intent("complete_task", tool="complete_task", params={})
        return None

    @staticmethod
    def _planner_priority(text: str) -> str:
        if re.search(r"\b(urgent|asap|critical)\b", text):
            return "urgent"
        if re.search(r"\b(high|important)\b", text):
            return "high"
        if re.search(r"\b(low|later|someday)\b", text):
            return "low"
        return "normal"

    def _planner_due(self, raw: str) -> str:
        """A due expression from the sentence, in the format the tool reads."""
        explicit = self._match(r"\b(?:by|before|due)\s+(.+?)(?:\s+with\b|\s*$)", raw)
        if explicit:
            when = self._parse_when(explicit)
            if when:
                return when
        if re.search(r"\btomorrow\b", raw, re.I):
            return self._parse_when("tomorrow") or ""
        if re.search(r"\btoday\b", raw, re.I):
            return self._parse_when("today") or ""
        return ""

    def _calendar(self, text: str, raw: str) -> Optional[Intent]:
        """Calendar actions (v0.7)."""
        # The plural forms matter: `\bmeeting\b` does not match "meetings", so
        # "list my meetings tomorrow" named a calendar thing and the gate did not
        # see it. Same for events, appointments and schedules.
        calendar_words = re.search(
            r"\b(calendars?|schedules?|meetings?|events?|appointments?|"
            r"free time|availability|plan my day|remind|book)\b",
            text,
        )
        # "what do I have tomorrow" names no calendar noun, but a time word plus
        # a question is a schedule request in practice.
        schedule_question = re.search(
            r"\b(what|anything|what's|whats)\b.*\b(have|on|scheduled|planned)\b", text
        ) and re.search(
            r"\b(today|tomorrow|tonight|this week|next week|morning|afternoon|"
            # "what do I have on monday" names a day and no calendar noun, which is
            # exactly the shape this second gate exists to catch.
            r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
            r"mon|tue|wed|thu|fri|sat|sun)\b", text)

        if not calendar_words and not schedule_question:
            return None

        # --- create -------------------------------------------------------
        create = re.search(r"\b(schedule|book|create|add|set up|arrange)\b", text)
        if create and not re.search(r"\b(find|when|what|show|list|do i have)\b", text):
            # What follows the verb, then the *request* is removed and the name
            # kept. Order matters: the time phrases go first, because a time word
            # can sit between the noun and the name ("project meeting tomorrow at
            # 5 PM") and stripping the noun first would leave "tomorrow at 5 PM"
            # as the title. `_match` returns one group, so one alternation regex
            # cannot express "noun optional" here.
            body = self._match(
                r"\b(?:schedule|book|create|add|set up|arrange)\b\s+(?:an?\s+)?(.*)",
                raw,
            ) or ""
            # 1. the time: from the first clock or day word to the end.
            body = re.sub(
                r"\s*(?:\b(?:at|on|by)\s+)?"
                # An ISO date ("on 2026-12-05 09:00") is a time too; \d{1,2}
                # matched the year's first two digits and then failed the colon.
                r"(?:\b\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2})?\b|"
                r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b|"
                r"\b(?:today|tomorrow|tonight|this\s+\w+|next\s+\w+)\b|"
                r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                r"mon|tue|wed|thu|fri|sat|sun)\b).*$",
                "", body, flags=re.I)
            # 2. the noun phrase, with at most a short modifier before it.
            body = re.sub(
                r"^\s*(?:[\w'-]+\s+){0,2}?(?:calendar\s+)?"
                r"(?:meeting|event|appointment)\b\s*(?:called|named|titled|for)?\s*",
                "", body, flags=re.I)
            title = body
            when = self._parse_when(raw)
            title = re.sub(r"^\s*with\s+", "", title or "", flags=re.I)
            title = self._arg(title)
            if when is None:
                return Intent(
                    "create_event",
                    note=(
                        "When should I schedule that? For example: "
                        '"schedule a project meeting tomorrow at 5 PM".'
                    ),
                )
            if not title:
                # No name was given. Inventing one -- "Meeting" -- would put an
                # event in the calendar under a name nobody chose, and the
                # approval prompt would show that invented name as if the
                # person had asked for it. A missing name is a question.
                return Intent(
                    "create_event",
                    note=(
                        "What should I call it? For example: "
                        '"schedule a project review tomorrow at 5 PM".'
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

        # --- prepare for a meeting ----------------------------------------
        # Checked before the plain "next meeting" branch: asking to be *prepared*
        # wants the briefing, not the start time. The same words on the planner
        # page produced a full brief, so answering with just the time was the
        # chat path quietly doing less than the page.
        prep = re.search(
            r"\b(?:prepare|prep|brief)\s+(?:me\s+)?(?:for|on)\b\s*(?:the\s+|my\s+)?"
            r"(?P<what>.+?)\s*$", text)
        if prep:
            what = self._arg(prep.group("what"))
            if re.search(r"\b(next|upcoming|coming)\b|\bmeeting\b|\b1:1\b|\bcall\b", what, re.I):
                # "the next meeting" names no meeting; anything else does.
                named = "" if re.search(
                    r"^(?:next|upcoming|coming)\s+(?:meeting|event|call|appointment|1:1)$",
                    what, re.I) else what
                return Intent("brief_meeting", tool="brief_meeting",
                              params={"meeting": named})

        # --- next meeting -------------------------------------------------
        if re.search(r"\b(next meeting|next event|next appointment|when is my next)\b", text):
            return Intent("next_event", tool="next_event", params={})

        # --- plan my day --------------------------------------------------
        if re.search(r"\bplan my day\b", text):
            date = "tomorrow" if "tomorrow" in text else "today"
            return Intent("daily_schedule", tool="daily_schedule", params={"date": date})

        # A sentence that names a specific capability -- "cancel my Browser Check
        # event", "delete the design review" -- must defer before this rule answers
        # with a listing. Widening the gate below made the calendar rule reach
        # sentences it used to skip, and a listing is not an answer to a cancel.
        if self._capability_matches(text):
            return None

        # --- list / what do I have ---------------------------------------
        # "check the calendar for Friday" and "list my meetings tomorrow" are the
        # plainest calendar questions there are, and both fell through: the verb
        # list had no "check", and the date was chosen only from today/tomorrow/
        # yesterday, so a weekday name was silently dropped and the answer would
        # have been about the wrong day.
        if re.search(r"\b(what|anything|show|list|summarize|read|check)\b", text):
            named_day = re.search(
                r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                r"mon|tue|wed|thu|fri|sat|sun)\b", text, re.I)
            if named_day:
                date = named_day.group(1).lower()
            elif "tomorrow" in text:
                date = "tomorrow"
            elif "yesterday" in text:
                date = "yesterday"
            else:
                date = "today"
            return Intent("list_events", tool="list_events", params={"date": date, "days": 1})

        # The decline below advertises cancellation and update while this rule emits
        # neither -- `cancel_event` and `update_event` were registered and unreachable,
        # so "cancel event 12" was answered with a promise and no action. A route that
        # lists what it can do yields to a sentence that names one of them.
        if self._capability_matches(text):
            return None

        # A calendar request the rules could not turn into an action is
        # **not a refusal** -- it is a request the model may be able to act
        # on, and the model is the only thing that can find the event by
        # name and resolve a relative time.
        #
        # So this declines with `not_understood` rather than `declined`:
        # the orchestrator routes `not_understood` to the model, first for
        # an answer and then for a proposal. Before this, "cancel my
        # Design Review event" was answered with a list of examples and no
        # action, while the model — shown only the calendar tools — picked
        # `search_events` for it, which is the right first step.
        return Intent(
            "calendar",
            reply_kind="not_understood",
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
        # Derived from the gateway's own vocabulary, not from a list kept here. The
        # eleven words it replaced are why "turn on the power" -- "power" being the
        # gateway's synonym for a plug -- was answered as nonsense: two lists for one
        # request, and no test able to watch them drift further apart. Sorted longest
        # first so "air conditioner" is not matched as the fragment "air".
        from ..home.gateway import device_vocabulary

        words = "|".join(re.escape(w).replace("\\ ", r"[\s_-]?") for w in device_vocabulary())
        m = re.search(
            r"\b(?P<cmd>turn on|turn off|switch on|switch off|dim|set|raise|lower)\b"
            r"\s+(?:the\s+)?(?P<dev>(?:[a-z0-9]+[ ])*?(?:" + words + r"))"
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
            level = int(m.group("lvl")) if m.group("lvl") else None
            # The captured words go to the gateway unchanged. This line used to read
            # ``if "temperature" in dev or dev in ("ac", "aircon", "heater"): dev = "ac"``,
            # which decided that a word the user said meant a device they did not name --
            # the second half of the heater bug. The gateway is the only thing allowed to
            # decide which device a word refers to, and the permission engine refuses an
            # actuation whose word matches nothing, so the rule exists in one place.
            from ..home.gateway import get_home_gateway

            target, described = get_home_gateway().actuation_target(dev)
            if target is None and described:
                # The user named a category, not a device. Choosing one and
                # actuating it was the old behaviour: "turn on the light" reached
                # the kitchen light out of four, and the answer never said it had
                # chosen. A question costs one turn; a wrong actuation costs trust.
                return Intent(
                    "home_control",
                    note=(
                        f"Which {dev} did you mean -- "
                        + ", ".join(d.name for d in described)
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
        # "AMR" is the owner's *project* name as well as the robot class, so the
        # bare token sent "what do I know about the AMR demo project" to robot
        # status. A robot request needs a robotics word, or AMR next to one.
        has_robot = bool(re.search(
            r"\b(robots?|robot\s+arm|gripper|joints|fleet|telemetry|waypoints?)\b", text)) or bool(
            re.search(r"\bamrs?\b", text)
            # "status" alone is too weak: "the status of the AMR demo" is about a
            # project. A word that is only ever a robot word qualifies.
            # "move the amr to (1,2)" and "where is the amr" name no robot noun,
            # so the robot-intent words count too -- motion, position, state.
            and re.search(r"\b(robot|robots|battery|charge|pose|waypoint|navigate|"
                          r"telemetry|fleet|dock|gripper|joints?|"
                          r"move|moves|go|stop|where|position|location|speed|map)\b",
                          text)
            and not re.search(r"\b(project|demo|deck|report|task|notes?|plan|"
                              r"proposal|client|meeting)\b", text))
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
                tool="get_machine_snapshot",
            )

        return None

    def _run_command(self, text: str, raw: str) -> Optional[Intent]:
        # Imperative and anchored. This used to match `run` anywhere in the sentence, so
        # "what trains run from mumbai to pune" became a request to execute a program
        # called `from`, and "how do I run a marathon" one called `a`. The allowlist gate
        # is what kept that harmless; a question should never reach an executor at all.
        match = re.match(
            r"^\s*(?:please\s+|can you\s+|could you\s+|now\s+)?run\b\s+([A-Za-z0-9_.-]+)\s*(.*)$",
            text, re.I)
        if not match:
            return None
        command = match.group(1).lower()
        if command in self._NAME_STOP:
            return None
        return Intent(
            "run_command",
            tool="run_allowlisted_command",
            params={"command": command, "args": match.group(2).strip()},
            confidence=0.7,
        )

    _SMALLTALK = {
        "hello": "Hello! I am Vantage. Add an OpenAI or Gemini API key in AI Providers to enable a fully conversational assistant, or ask me things like \"what time is it\", \"what is my battery\", or \"find my report\".",
        "hi": "Hi! For ChatGPT-style conversation, add an API key in AI Providers. Right now I can help with: battery, files, calendar, projects, and more — just ask.",
        "hey": "Hey. What should I do?",
        "thanks": "Anytime.",
        "thank you": "Anytime.",
        "ok": "Right. Say the word.",
        "good morning": "Good morning. Battery, files, calendar -- what do you need?",
    }

    def _smalltalk(self, text: str, raw: str) -> Optional[Intent]:
        """Greeting a greeting instead of reporting a failure to parse it.

        "hello" reaching the not-understood sentence is the most obvious way a system built
        out of capability lists announces that nobody thought about being talked to.
        """
        words = text.strip().strip(".!?,").lower()
        if not words or len(words.split()) > 3:
            return None
        reply = self._SMALLTALK.get(words)
        if reply is None:
            return None
        return Intent("smalltalk", note=reply)

    #: Question shapes whose subject is a topic to look up, in the order they are tried.
    #: Deliberately few: a sentence that is not one of these is not an encyclopedia query.
    _QUESTION_FORMS = (
        r"^\s*(?:can|could|would|will)\s+you\s+(?:tell|explain)\s+me\s+(?:about\s+)?(.+)$",
        r"^\s*(?:please\s+)?(?:explain|describe)\s+(?:about\s+)?(.+)$",
        r"^\s*tell\s+me\s+(?:about\s+)?(?:the\s+)?(.+)$",
        r"^\s*what\s+(?:is|are|was|were)\s+(?:the\s+|a\s+|an\s+)?(.+)$",
        r"^\s*who\s+(?:is|was|are|were)\s+(?:the\s+|a\s+|an\s+)?(.+)$",
        r"^\s*(?:define|meaning\s+of)\s+(?:the\s+|a\s+|an\s+)?(.+)$",
    )
    #: A subject with these words in it is about this machine, not about the world, and
    #: the specific routes above already answered it. Anything left is a lookup.
    _NOT_A_TOPIC = re.compile(r"\b(my|i|me|we|our|project|file|battery|calendar|email|"
                              r"workspace|robot|car|home|phone|watch|laptop|today|"
                              r"yesterday|tomorrow|schedule|memory)\b", re.I)

    def _ask_question(self, text: str, raw: str) -> Optional[Intent]:
        """A factual question, answered by looking it up rather than by guessing.

        The shape is narrow on purpose: *what is X*, *who is X*, *explain X*, *tell me
        about X*. Anything else stays with the specific routes, the registry fallback or
        the model, and an empty subject is never sent off to be looked up.

        When a configured model is enabled, a general question is the model's job, not
        the Wikipedia tool's: routing "what is the capital of France" to ``ask_question``
        would make a permission-revoked lookup tool swallow a question the assistant is
        meant to answer conversationally. So the tool is the *offline* fallback — it only
        claims the sentence when no model is available.
        """
        try:
            llm_enabled = bool(get_settings().llm_enabled)
        except Exception:
            llm_enabled = False
        if llm_enabled:
            return None
        stripped = re.sub(r"^\s*(?:hey|ok|hi|hello|vantage|jarvis)[,.!\s]+", "", text,
                          flags=re.I).strip()
        for form in self._QUESTION_FORMS:
            got = re.match(form, stripped, re.I)
            if not got:
                continue
            topic = got.group(1).strip().strip(".!?").strip()
            if len(topic) < 2 or len(topic.split()) > 8:
                return None
            if self._NOT_A_TOPIC.search(topic):
                return None
            return Intent("ask_question", tool="ask_question", params={"topic": topic},
                          confidence=0.75)
        return None

    # -- registry-derived capability fallback --------------------------------
    #
    # Last in the chain. The specific routes above do better when they fit, and this
    # one exists only so that "the sentence names a capability JARVIS has" stops being
    # indistinguishable from "JARVIS cannot do that".
    #: Words that are grammar rather than part of a capability's name. Verbs stay in:
    #: ``list_workspaces`` is reached by "list the workspace", and dropping "list"
    #: would leave one token to match on, which is a guess not an identification.
    _NAME_STOP = frozenset({
        "the", "a", "an", "my", "me", "our", "of", "to", "for", "on", "in", "is", "was",
        "it", "this", "that", "these", "those", "do", "does", "did", "and", "or", "then",
        "please", "you", "your", "their", "are", "be", "into", "with", "at", "as", "from",
    })

    #: The shapes of value a sentence can supply without the router inventing one:
    #: a quoted phrase, an id the system itself issues (ASSET-1a2b3c), a device id
    #: (phone_main), or a bare number. Anything else is asked for.
    _VALUE_SHAPES = (
        re.compile(r"[\"'“‘]([^\"'”’]+)[\"'”’]"),
        re.compile(r"\b([A-Z]{2,}-[0-9A-Za-z]+)\b"),
        re.compile(r"\b([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b"),
        re.compile(r"\b(\d{1,6})\b"),
    )

    @staticmethod
    def _stem(word: str) -> str:
        """``workspaces`` -> ``workspace``, ``memories`` -> ``memory``.

        Singular and plural reach the same capability; an alias dictionary would have
        to be maintained per tool, which is the thing this route exists to avoid.
        """
        w = word.lower()
        if len(w) > 4 and w.endswith("ies"):
            return w[:-3] + "y"
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            return w[:-1]
        return w

    # -- wording tolerance (Phase 70) ----------------------------------------
    #
    # Phase 69's corpus counted the gap: `opne safari`, `batery`, `screnshot`, `serch`, `open, safari`
    # and "can you bring up contacts" all name capabilities that are registered and reachable from
    # the tidy wording, and route to nothing from the untidy wording. The answer a person gets -- "I
    # understood the words but not the goal" -- is true of the router and false about the product.
    #
    # Tolerance is admitted under one constraint: the repaired sentence may only produce an action on
    # a tool at READ or LOW tier that nobody has to approve. `delete` is a token of `delete_file`
    # (DESTRUCTIVE, gated) and `send` reaches `send_to_device` (EXTERNAL, gated), so a typo of either
    # word is repaired by the distance function and then *refused by the acceptance rule* -- the
    # tolerant pass can never hand the router a reach the exact wording did not already have.

    #: Verbs a person uses for "start this program". Kept explicit rather than derived, because the
    #: only way to discover that "bring up" means "open" is to be told.
    _LAUNCH_VERBS = ("open", "launch", "start", "run", "bring up", "fire up")

    @classmethod
    def _launch_pattern(cls) -> str:
        """The alternation the launch routes use, built from `_LAUNCH_VERBS` so a verb added to the
        vocabulary cannot silently miss one of the three patterns that need it."""
        return "|".join(cls._LAUNCH_VERBS)

    @staticmethod
    def _edit_distance(left: str, right: str) -> int:
        """Damerau-Levenshtein: an adjacent transposition counts as one edit.

        `opne` -> `open` is a swap of two keys, the single most common typo shape on a physical
        keyboard. Plain Levenshtein scores it 2, which would leave the commonest real error unfixed
        while rarer insertions were handled.
        """
        if left == right:
            return 0
        width, height = len(left) + 1, len(right) + 1
        grid = [[0] * height for _ in range(width)]
        for i in range(width):
            grid[i][0] = i
        for j in range(height):
            grid[0][j] = j
        for i in range(1, width):
            for j in range(1, height):
                cost = 0 if left[i - 1] == right[j - 1] else 1
                grid[i][j] = min(grid[i - 1][j] + 1, grid[i][j - 1] + 1, grid[i - 1][j - 1] + cost)
                if i > 1 and j > 1 and left[i - 1] == right[j - 2] and left[i - 2] == right[j - 1]:
                    grid[i][j] = min(grid[i][j], grid[i - 2][j - 2] + 1)
        return grid[-1][-1]

    def _repair_cues(self) -> frozenset:
        """The only words a repair may land on: capability name tokens, application names, launch verbs.

        Derived from the registry rather than hand-listed, so it covers the tools that get added next
        month -- the same reasoning Phase 30's fallback was written for.
        """
        cached = getattr(self, "_cues", None)
        if cached is not None:
            return cached
        cues = {self._stem(v) for v in self._LAUNCH_VERBS}
        for spec in self.tools.all():
            cues |= {self._stem(t) for t in spec.name.split("_")} - self._NAME_STOP
        for app in self.apps.all():
            for name in (app.key, getattr(app, "name", ""), *(getattr(app, "aliases", ()) or ())):
                for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", str(name).lower()):
                    cues.add(self._stem(token))
        cues = frozenset(c for c in cues if len(c) >= 4)
        self._cues = cues
        return cues

    @staticmethod
    def _is_transposition(word: str, cue: str) -> bool:
        """Exactly one pair of adjacent characters swapped: `opne` / `open`."""
        if len(word) != len(cue):
            return False
        diff = [i for i, (a, b) in enumerate(zip(word, cue)) if a != b]
        return (len(diff) == 2 and diff[1] == diff[0] + 1
                and word[diff[0]] == cue[diff[1]] and word[diff[1]] == cue[diff[0]])

    def _repair(self, word: str) -> str:
        """Rewrite one unknown token only when exactly one cue is one edit away.

        Two cues at the same distance means the typing is genuinely ambiguous, and the answer is to
        leave the word alone rather than take whichever candidate the set yielded first.

        A cue shorter than six characters is only accepted as a key-swap, and that boundary was set
        by a false positive this rule produced on its first run: `root` is one insertion away from
        `robot`, so "run this command as root" was repaired into a robot-status query the person
        never asked for. Short English words are cue words too often for an insertion to be safe; a
        transposition is, because it cannot make a word out of a different word.
        """
        stem = self._stem(word)
        cues = self._repair_cues()
        if len(word) < 4 or stem in cues:
            return word
        near = sorted(c for c in cues if self._edit_distance(word, c) == 1
                      and (len(c) >= 6 or self._is_transposition(word, c)))
        return near[0] if len(near) == 1 else word

    def _repair_text(self, text: str) -> str:
        return " ".join(self._repair(token) for token in text.split())

    @staticmethod
    def _pause_normalised(text: str) -> str:
        """`open, safari` -> `open safari`. Measured: the comma version reaches nothing.

        A comma between a verb and its object is what a thumb on a phone keyboard or a
        transcription engine leaves behind; it carries no meaning the router needs to keep.
        """
        return re.sub(r"(?<=[A-Za-z0-9]),\s*(?=[A-Za-z0-9])", " ", text)

    def _tolerable(self, tool: str) -> bool:
        """Can the tolerant pass act on this tool at all? READ/LOW and ungated, or nothing."""
        spec = self.tools.get(tool) if tool else None
        return bool(spec) and not spec.requires_confirmation and spec.risk <= RiskLevel.LOW

    def _tolerant_intent(self, lowered: str, raw: str) -> Optional[Intent]:
        """Re-read a sentence the chain found nothing in. Only ever called after a miss."""
        pause = self._pause_normalised(lowered)
        pause_raw = self._pause_normalised(raw)
        repaired = self._repair_text(pause)
        tried = set()
        for candidate, argument_source in ((pause, pause_raw),
                                           (repaired, self._repair_text(pause_raw))):
            if candidate == lowered or candidate in tried:
                continue
            tried.add(candidate)
            intent = self._first_intent(candidate, argument_source)
            if intent and intent.tool and self._tolerable(intent.tool):
                return intent
        return None

    def _single_word_capability(self, text: str, raw: str) -> Optional[Intent]:
        """One shouted word at a laptop: route it if it names exactly one capability.

        Measured before deciding this, because the decision is a registry fact and not a preference:
        `status` is the last token of **13** registered tools and `workspaces` of **5**, so guessing
        `system_status` for "status" would be a guess wearing a confidence score. Those get the
        registry's own ambiguity question instead of "I understood the words but not the goal" --
        which is a capability list the product already has, not an invention.

        The utterance must be *literally* one word. Phase 30 registers `widget_gizmo` and refuses to
        plan anything for "the gizmo", and it is right: a noun phrase with an article is not a
        command. Stripping stop words here would have quietly overridden that decision, so "the gizmo"
        stays two words and reaches nothing.
        """
        words = re.findall(r"[A-Za-z][A-Za-z0-9]*", text)
        if len(words) != 1:
            return None
        wanted = self._stem(words[0])
        fits = [s for s in self.tools.all() if self._stem(s.name.split("_")[-1]) == wanted]
        if not fits:
            return None
        if len(fits) == 1:
            spec = fits[0]
            params = self._sentence_values(raw, tuple(spec.required_parameters))
            return Intent(spec.name, tool=spec.name, params=params, confidence=0.6)
        named = "; ".join((s.description or s.name).rstrip(".") for s in fits[:3])
        return Intent("capability_ambiguous",
                      note=f"I know {len(fits)} capabilities that fit {words[0]!r}: {named}. "
                           "Which did you mean?",
                      clarification=True, confidence=0.5)

    @classmethod
    def _sentence_values(cls, raw: str, needed: Tuple[str, ...]) -> Dict[str, Any]:
        """Fill ``needed`` from the sentence's value-shaped tokens, in order.

        Values are taken by position and assigned to the required parameters in the
        order the tool declares them, so a sentence that names one thing for a tool
        that needs two fills nothing -- it is left incomplete and asked about.
        """
        if not needed:
            return {}
        found: List[tuple] = []
        for pattern in cls._VALUE_SHAPES:
            for match in pattern.finditer(raw):
                found.append((match.start(), match.end(), match.group(1)))
        found.sort()
        values: List[str] = []
        taken_until = -1
        for start, end, value in found:
            if start < taken_until:
                continue  # already inside a quoted phrase, not a second argument
            taken_until = end
            values.append(value.strip())
        return {name: values[i] for i, name in enumerate(needed) if i < len(values)}

    @staticmethod
    def _capability_question(spec, named: str = "") -> str:
        """Ask for a missing argument in the registry's own words.

        The ``description`` is human language the tool already carries; the parameter
        name is not, and 29-B's lesson is that showing it to a user is showing them an
        internal field. So this never says ``uid`` or ``workspace_id``.

        When the sentence **did** name the thing -- "cancel my Design Review
        event" -- the question says so, because "your sentence does not say
        which one" is false there, and a person who *did* name it is told
        they did not.
        """
        what = (spec.description or "").rstrip(".").strip()
        what = f"{what[0].lower()}{what[1:]}" if what else spec.name.replace("_", " ")
        if named:
            return (
                f'I can {what} — but I do not know which one you mean by '
                f"{named!r}. Give me the name or the id, like "
                f'"{spec.name.replace("_", " ")} <which one>"?'
            )
        return (
            f"I can {what} — your sentence does not say which one. "
            f'Say it with the name or id, like "{spec.name.replace("_", " ")} <which one>"?'
        )

    def _capability_matches(self, text: str) -> List:
        """Registered tools whose own name words the sentence contains.

        Two rules can ask this and need the same answer: the fallback below, and any
        route that *lists or advertises* capabilities. An advertising route is the one
        that must yield to a sentence naming a specific capability -- it is describing
        the system, not doing anything. A route that refuses or that already has a tool
        does not yield, which is why this is called at three named sites and not from
        the dispatch loop: silently preferring a keyword match over a refusal would be
        a way to talk past the permission engine.
        """
        words = {self._stem(w) for w in re.findall(r"[A-Za-z][A-Za-z0-9_]*", text)}
        matches = []
        for spec in self.tools.all():
            tokens = {self._stem(t) for t in spec.name.split("_")} - self._NAME_STOP
            if len(tokens) >= 2 and tokens <= words:
                matches.append(spec)
        return matches

    def _capability_fallback(self, text: str, raw: str) -> Optional[Intent]:
        """Reach a registered capability whose own name words the sentence contains.

        Measured when this was written: 127 tools registered, 28 named nowhere in this
        file, so no typed sentence could ask for any of them and the answer was "I
        understood the words but not the goal" -- true of the router, heard as "I can't".
        Deriving the route from the registry rather than hand-writing one regex per tool
        is also what covers next month's tools, and the action is validated through
        ``ToolRegistry.validate_params`` before it is emitted, so this route structurally
        cannot repeat Phase 29's defect of claiming an action it cannot fill.
        """
        matches = self._capability_matches(text)
        if not matches:
            return None
        if len(matches) > 1:
            named = "; ".join((s.description or s.name).rstrip(".") for s in matches[:3])
            return Intent(
                "capability_ambiguous",
                note=f"I know {len(matches)} capabilities that fit: {named}. Which did you mean?",
                clarification=True,
                confidence=0.5,
            )
        spec = matches[0]
        params = self._sentence_values(raw, tuple(spec.required_parameters))
        if not self.tools.validate_params(spec.name, params):
            return Intent(spec.name, tool=spec.name, params=params, confidence=0.55)
        missing = tuple(p for p in spec.required_parameters if p not in params)
        named = self._named_thing(raw, spec)
        return Intent(spec.name, note=self._capability_question(spec, named),
                      clarification=True, confidence=0.55, missing_identifiers=missing,
                      named_thing=named)

    def _named_thing(self, raw: str, spec) -> str:
        """The thing the sentence named, if it named one.

        "cancel my Design Review event" names *Design Review*. The words
        that matched the capability are "cancel" and "event"; what is left
        between them is the name the person gave, and it belongs in the
        question -- otherwise the question says "your sentence does not say
        which one" about a sentence that did.
        """
        text = re.sub(r"[^A-Za-z0-9 _'-]", " ", raw or "")
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""
        # Remove the capability's own name words, and the connective words
        # around them.
        tokens = {self._stem(t) for t in spec.name.split("_")}
        for token in ("my", "the", "a", "an", "our", "your", "please", "that",
                      "this", "one", "of"):
            tokens.add(token)
        kept = [w for w in text.split()
                if self._stem(w) not in tokens]
        named = " ".join(kept).strip(" ,'-")
        return named if 1 <= len(named) <= 80 else ""


