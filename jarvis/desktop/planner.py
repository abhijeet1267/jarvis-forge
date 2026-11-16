"""Turn a natural-language desktop instruction into typed desktop operations.

One rule shapes this module: **the planner never invents a target.** A screen
point comes from numbers in the request or from the caller's context; a UI
element comes from the label the person used. When either is missing the result
is a question, not a guess — a guessed coordinate clicks somewhere the person
did not mean, and a guessed accessibility label presses something they never
named. Both are worse than asking, and the executor cannot undo either.

The plan is data, not execution: it names registry tools and the parameters they
declare. Nothing here talks to the executor, so it stays a pure function.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..tools.desktop_native import MAX_COORDINATE, MAX_TEXT_CHARS

__all__ = ["DesktopPlan", "PlannedStep", "plan_desktop"]

Point = Tuple[float, float]

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

#: Words that may surround a pair of coordinates without changing their meaning.
_POINT_WORDS = frozenset({
    "at", "to", "the", "point", "points", "coordinates", "coordinate",
    "and", "on", "onto", "x", "y", "from",
})

#: Modifier spellings the executor accepts (see ``opKey`` in
#: ``native/desktop_executor/main.swift``), normalised to its canonical names.
_MODIFIERS = {
    "cmd": "cmd", "command": "cmd", "meta": "cmd", "⌘": "cmd",
    "shift": "shift", "⇧": "shift",
    "opt": "opt", "option": "opt", "alt": "opt", "⌥": "opt",
    "ctrl": "ctrl", "control": "ctrl", "⌃": "ctrl",
}

#: Named keys from the executor's key table. A key outside it is refused at
#: runtime, so it is refused here as a question instead.
_NAMED_KEYS = frozenset({
    "return", "enter", "tab", "space", "delete", "backspace", "escape", "esc",
    "left", "right", "down", "up", "home", "end", "pageup", "pagedown",
})

#: Words that point at a coordinate already established in the same request.
#: They never invent one: with no established point they produce a question.
_DEICTIC_POINTS = frozenset({"there", "here"})

_ELEMENT_NOUNS = r"(?:menu\s+item|button|link|tab|checkbox|item|field|cell|menu)"
_ELEMENT = re.compile(
    r"^(?P<label>.+?)\s+" + _ELEMENT_NOUNS +
    r"(?:\s+(?:in|of|on|for)\s+(?P<app>.+))?$", re.I)


@dataclass(frozen=True)
class PlannedStep:
    tool: str
    params: Dict[str, Any]
    description: str


@dataclass
class DesktopPlan:
    steps: List[PlannedStep] = field(default_factory=list)
    clarification: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def plan_desktop(request: str, *, context: Optional[Dict[str, Any]] = None) -> DesktopPlan:
    """Plan the desktop operations a request asks for, or ask what it left out.

    ``context`` may carry ``application`` (the default app for semantic actions
    and for reads) and ``point`` or ``cursor`` (a known ``(x, y)`` for actions
    whose request names no coordinates).
    """
    text = request.strip() if isinstance(request, str) else ""
    if not text:
        return _ask("What should I do on the desktop?")
    ctx = context or {}
    clauses = _split_chain(text)
    if len(clauses) <= 1:
        return _plan_one(text, ctx)
    return _plan_chain(clauses, ctx)


def _plan_one(text: str, ctx: Dict[str, Any]) -> DesktopPlan:
    for matcher in (_plan_move, _plan_drag, _plan_click, _plan_scroll,
                    _plan_type, _plan_press, _plan_open, _plan_focus,
                    _plan_inspect):
        plan = matcher(text, ctx)
        if plan is not None:
            return plan
    return _ask("I could not understand that desktop instruction. Name a point "
                "('400 300'), a key ('cmd s'), an element ('the Save button in "
                "Safari'), or an application to focus or read.")


#: Words that can join two instructions, and the multi-word ones that mean the
#: same thing. A connector only splits when what follows is a desktop
#: instruction this planner owns — "and" inside a name or a sentence is not a
#: boundary.
_CONNECTORS = frozenset({"and", "then", "also"})
_CONNECTOR_PHRASES = (("after", "that"), ("followed", "by"))

#: Verbs that begin an instruction this planner can plan. Used only to decide
#: whether a connector is a boundary; the matchers still do the parsing.
_VERBS = ("move", "drag", "scroll", "type", "press", "click", "double-click",
          "double click", "right-click", "right click", "focus", "switch",
          "bring", "activate", "read", "inspect", "show", "list", "open",
          "launch", "start")


def _plan_chain(clauses: List[str], ctx: Dict[str, Any]) -> DesktopPlan:
    """Plan every clause of a chained request, in the order it was spoken.

    All-or-nothing on purpose: if any clause is a question, the whole request is
    a question. Executing the first two clauses and stopping would leave the
    person unable to tell which part ran.

    A cursor point established by an earlier step is available to a later one —
    "move the cursor to 412 336, then click there" clicks (412, 336), because
    "there" is the point the person just named in the same plan, not an
    invention.
    """
    point = _context_point(ctx)
    steps: List[PlannedStep] = []
    for clause in clauses:
        plan = _plan_one(clause, {**ctx, "point": point})
        if plan.clarification is not None:
            return _ask(f"Nothing was planned, because one part of the request "
                        f"was unclear ({clause!r}): {plan.clarification}")
        for step in plan.steps:
            if step.tool == "desktop_move_pointer":
                point = (step.params["x"], step.params["y"])
            steps.append(step)
    return DesktopPlan(
        steps=steps,
        notes=[f"Read this request as {len(clauses)} instructions and planned "
               "them in the order they were spoken."],
    )


def _words(text: str) -> List[Tuple[str, bool, int, int]]:
    """Words with their source offsets, and whether each sits inside quotes.

    The quoted flag is what keeps "type \"hello, world and goodbye\"" one
    instruction: a comma or an "and" inside quotes is text being typed.
    """
    out: List[Tuple[str, bool, int, int]] = []
    quote: Optional[str] = None
    # A comma or semicolon ends a word whether or not a space follows it:
    # "412 336, click" must yield "336" and "," as separate tokens, or the
    # boundary scan never sees the comma at all.
    for match in re.finditer(r"[^\s'\"\u2018\u2019\u201c\u201d,;]+|['\"\u2018\u2019\u201c\u201d,;]", text):
        token = match.group()
        if token in ",\u2018\u2019\u201c\u201d'\"":
            if token in ",;":
                out.append((token, quote is not None, match.start(), match.end()))
            elif quote is None:
                quote = token
            elif quote == token or token in "\u2018\u2019\u201c\u201d":
                quote = None
            continue
        out.append((token, quote is not None, match.start(), match.end()))
    return out


def _starts_instruction(words: List[Tuple[str, bool, int, int]], at: int) -> bool:
    """True when the first instruction after a connector is one we can plan.

    Leading connectors are skipped first, so "type hi then press enter" sees
    "press enter" rather than "then press enter" — the version that did not skip
    them could not split after a comma-then sequence.
    """
    while at < len(words):
        value = words[at][0].lower()
        if value in _CONNECTORS or _is_phrase(words, at, ("after", "that")) \
                or _is_phrase(words, at, ("followed", "by")):
            at += 2 if _is_phrase(words, at, ("after", "that")) \
                or _is_phrase(words, at, ("followed", "by")) else 1
            continue
        break
    if at >= len(words):
        return False
    first = words[at][0].lower()
    if first in _VERBS:
        return True
    if " ".join(w[0].lower() for w in words[at:at + 2]) in _VERBS:
        return True
    # A click exempt from the verb list here is the deictic one: "click there"
    # is a real instruction even though it has no coordinate of its own, and the
    # chain supplies the point. Without this the boundary is rejected and
    # "move …, click there" collapses into one unplannable clause.
    return first == "click"


def _is_phrase(words: List[Tuple[str, bool, int, int]], at: int,
               phrase: Tuple[str, ...]) -> bool:
    if at + len(phrase) > len(words):
        return False
    return all(not words[at + k][1] and words[at + k][0].lower() == want
               for k, want in enumerate(phrase))


def _split_chain(text: str) -> List[str]:
    """Split a request into instruction clauses on genuine boundaries.

    A single instruction returns as one clause covering the whole text, so the
    non-chained path is untouched. Splitting happens only at a comma, semicolon,
    or connector that is followed by an instruction this planner can plan;
    everything else stays in the clause it belongs to. The clause keeps the text
    exactly as written between its boundaries, which is what lets a ``type``
    clause keep its quoted text word for word.
    """
    words = _words(text)
    if not words:
        return []
    # Each boundary is two offsets: where the previous instruction stops, and
    # where the next one starts. The connector words between them belong to
    # neither clause -- "type hi then press enter" is "type hi" and "press
    # enter", not "type hi then" (which would type the word "then").
    cuts: List[Tuple[int, int]] = []
    for i, (value, quoted, start, _) in enumerate(words):
        if quoted:
            continue
        if not (value in (",", ";") or value.lower() in _CONNECTORS
                or _is_phrase(words, i, ("after", "that"))
                or _is_phrase(words, i, ("followed", "by"))):
            continue
        nxt = i + 1
        while nxt < len(words):
            word = words[nxt][0].lower()
            if word in (",", ";") or word in _CONNECTORS:
                nxt += 1
            elif _is_phrase(words, nxt, ("after", "that")) \
                    or _is_phrase(words, nxt, ("followed", "by")):
                nxt += 2
            else:
                break
        if nxt < len(words) and _starts_instruction(words, nxt):
            cuts.append((start, words[nxt][2]))
    if not cuts:
        return [text]
    # Keep the last boundary at each instruction start: a comma followed by
    # "then" yields one cut, not two, and the comma is not left on the clause.
    seen: dict = {}
    for ends, begins in cuts:
        seen[begins] = (ends, begins)
    ordered = sorted(seen.values(), key=lambda pair: pair[1])

    clauses: List[str] = []
    offset = 0
    for ends, begins in ordered:
        clause = text[offset:ends].strip(" ,;\t\n")
        if clause:
            clauses.append(clause)
        offset = begins
    tail = text[offset:].strip(" ,;\t\n")
    if tail:
        clauses.append(tail)
    return clauses


# -- shared parsing -----------------------------------------------------------

def _ask(question: str) -> DesktopPlan:
    return DesktopPlan(steps=[], clarification=question)


def _fmt(value: float) -> str:
    return f"{value:g}"


def _coords(text: str, expect: int) -> Optional[Point]:
    """Read exactly ``expect`` numbers, rejecting anything with other words.

    This is what keeps "the top left" from becoming an invented coordinate: a
    direction is not a number, so it does not parse as a point.
    """
    numbers = [float(n) for n in _NUMBER.findall(text)]
    words = {w.lower() for w in re.findall(r"[A-Za-z]+", text)}
    if len(numbers) != expect or not words <= _POINT_WORDS:
        return None
    return tuple(numbers)  # type: ignore[return-value]


def _out_of_bounds(point: Point, subject: str) -> Optional[DesktopPlan]:
    x, y = point
    if 0 <= x <= MAX_COORDINATE and 0 <= y <= MAX_COORDINATE:
        return None
    return _ask(f"{subject} ({_fmt(x)}, {_fmt(y)}) is outside the "
                f"0-{MAX_COORDINATE} range this desktop tool accepts; give a "
                "point inside that range.")


def _clean_app(text: str) -> str:
    value = text.strip().strip(".!?,;:")
    value = re.sub(r"^(?:the\s+|an?\s+)", "", value, flags=re.I)
    value = re.sub(r"\s+(?:window|app|application)$", "", value, flags=re.I)
    return value.strip()


#: Words that begin a second instruction. An application name is a noun phrase;
#: if one of these appears inside it, the caller wrote two things, and taking the
#: whole string as an app would invent a target -- measured: "focus Safari and
#: click Save" planned a focus of an application called "Safari and click Save".
_CHAIN_WORDS = frozenset({
    "and", "then", "also", "after", "before", "plus",
    "click", "type", "press", "scroll", "drag", "move", "open", "focus",
})


def _looks_like_one_app(value: str) -> bool:
    """True when `value` is plausibly a single application name."""
    if not value or len(value) > 60:
        return False
    words = re.findall(r"[A-Za-z0-9][\w.+-]*", value.lower())
    if not words:
        return False
    return not any(word in _CHAIN_WORDS for word in words)


def _context_app(ctx: Dict[str, Any]) -> str:
    app = ctx.get("application")
    return _clean_app(app) if isinstance(app, str) else ""


def _context_point(ctx: Dict[str, Any]) -> Optional[Point]:
    raw = ctx.get("point", ctx.get("cursor"))
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = (raw.get("x"), raw.get("y"))
    try:
        x, y = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not (0 <= x <= MAX_COORDINATE and 0 <= y <= MAX_COORDINATE):
        return None
    return (x, y)


def _strip_outer_quotes(text: str) -> str:
    pairs = {"'": "'", '"': '"', "“": "”", "‘": "’"}
    if len(text) >= 2 and text[0] in pairs and text[-1] == pairs[text[0]]:
        return text[1:-1]
    return text


def _element(rest: str) -> Optional[Tuple[str, str]]:
    body = re.sub(r"^(?:the|a|an)\s+", "", rest.strip(), flags=re.I)
    match = _ELEMENT.match(body)
    if not match:
        return None
    label = match.group("label").strip().strip("'\"“”‘’")
    if not label:
        return None
    return label, _clean_app(match.group("app") or "")


def _looks_like_element(rest: str) -> bool:
    return re.fullmatch(rf"(?:the\s+|a\s+|an\s+)?{_ELEMENT_NOUNS}",
                        rest.strip(), re.I) is not None


def _press_element_plan(keyword: str, app: str, ctx: Dict[str, Any],
                        verb: str) -> DesktopPlan:
    notes: List[str] = []
    if not app:
        app = _context_app(ctx)
        if app:
            notes.append(f"Took the application from context: {app}.")
    if not app:
        return _ask(f"Which application should I {verb} the {keyword!r} element "
                    "in? Name it, for example 'the Save button in Safari'.")
    return DesktopPlan(
        steps=[PlannedStep("desktop_press_element",
                           {"application": app, "keyword": keyword},
                           f"Press {keyword!r} in {app}")],
        notes=notes,
    )


def _key_plan(rest: str) -> DesktopPlan:
    tokens = [t for t in re.split(r"[\s+]+", rest.strip()) if t]
    if tokens and tokens[0].lower() == "the":
        tokens = tokens[1:]
    if tokens and tokens[-1].lower() in {"key", "bar"}:
        tokens = tokens[:-1]
    modifiers: List[str] = []
    keys: List[str] = []
    for token in tokens:
        low = token.lower()
        if low in _MODIFIERS:
            modifiers.append(_MODIFIERS[low])
        else:
            keys.append(token)
    if len(keys) != 1:
        return _ask("Which single key should I press? For example 'press cmd s', "
                    "'press command shift 4', or 'press enter'.")
    key = keys[0]
    if not (key.lower() in _NAMED_KEYS or
            (len(key) == 1 and key.isascii() and key.isalnum())):
        return _ask(f"{key!r} is not a key this build sends. Name a letter, a "
                    "digit, or one of: enter, tab, space, escape, delete, or an "
                    "arrow key.")
    label = "+".join([*modifiers, key])
    tool = "desktop_shortcut" if modifiers else "desktop_press_key"
    return DesktopPlan(steps=[PlannedStep(tool, {"key": key, "modifiers": modifiers},
                                          f"Press {label}")])


# -- utterance shapes ---------------------------------------------------------

def _plan_move(text: str, ctx: Dict[str, Any]) -> Optional[DesktopPlan]:
    match = re.match(r"(?:please\s+)?move\s+(?:the\s+|my\s+)?"
                     r"(?:cursor|pointer|mouse)(?:\s+(?:cursor|pointer))?"
                     r"\s+(?:to|onto)\s+(?P<target>.+?)\s*$", text, re.I)
    if not match:
        return None
    point = _coords(match.group("target"), 2)
    if point is None:
        return _ask("Which point should the pointer move to? A corner such as "
                    "'top left' is not a screen position; name coordinates such "
                    "as '400 300'.")
    refusal = _out_of_bounds(point, "That pointer position")
    if refusal is not None:
        return refusal
    x, y = point
    return DesktopPlan(steps=[PlannedStep(
        "desktop_move_pointer", {"x": x, "y": y},
        f"Move the pointer to ({_fmt(x)}, {_fmt(y)})")])


def _plan_drag(text: str, ctx: Dict[str, Any]) -> Optional[DesktopPlan]:
    match = re.match(r"drag\s+(?:from\s+)?(?P<points>.+?)\s*$", text, re.I)
    if not match:
        return None
    points = _coords(match.group("points"), 4)
    if points is None:
        return _ask("A drag needs two points: 'drag from 100 100 to 300 300'.")
    refusal = _out_of_bounds(points[:2], "The drag's start")
    if refusal is not None:
        return refusal
    refusal = _out_of_bounds(points[2:], "The drag's end")
    if refusal is not None:
        return refusal
    from_x, from_y, to_x, to_y = points
    return DesktopPlan(steps=[PlannedStep(
        "desktop_drag",
        {"from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y},
        f"Drag from ({_fmt(from_x)}, {_fmt(from_y)}) to "
        f"({_fmt(to_x)}, {_fmt(to_y)})")])


def _plan_click(text: str, ctx: Dict[str, Any]) -> Optional[DesktopPlan]:
    match = re.match(r"(?P<verb>double[\s-]?click|right[\s-]?click|click)\b"
                     r"(?P<rest>.*)$", text, re.I)
    if not match:
        return None
    verb = match.group("verb").lower().replace(" ", "-")
    tool = {"click": "desktop_click",
            "double-click": "desktop_double_click",
            "right-click": "desktop_right_click"}[verb]
    rest = match.group("rest").strip().rstrip(".!?")
    if not rest:
        return _ask("Where should I click? Name a point such as '400 300', or a "
                    "labelled element such as 'the Save button in Safari'.")
    # "click there" / "click here" names the point an earlier step of the same
    # plan moved to. It is a reference to a coordinate the person gave, not an
    # invented one -- but with no such step it is a question.
    if rest.lower() in _DEICTIC_POINTS:
        anchor = _context_point(ctx)
        if anchor is None:
            return _ask("Where should I click? 'there' does not name a point yet; "
                        "give coordinates such as '400 300', or move the cursor "
                        "first in the same request.")
        x, y = anchor
        return DesktopPlan(steps=[PlannedStep(
            tool, {"x": x, "y": y},
            f"{verb.replace('-', ' ').capitalize()} at ({_fmt(x)}, {_fmt(y)}), "
            f"the point this request just named")])
    point = _coords(rest, 2)
    if point is not None:
        refusal = _out_of_bounds(point, "That click position")
        if refusal is not None:
            return refusal
        x, y = point
        return DesktopPlan(steps=[PlannedStep(
            tool, {"x": x, "y": y},
            f"{verb.replace('-', ' ').capitalize()} at ({_fmt(x)}, {_fmt(y)})")])
    element = _element(rest)
    if element is not None:
        if verb != "click":
            return _ask(f"I can only {verb.replace('-', ' ')} at a point; name "
                        "coordinates such as '400 300'. A labelled element is "
                        "pressed, not double- or right-clicked.")
        keyword, app = element
        return _press_element_plan(keyword, app, ctx, verb="click")
    return _ask("Where should I click? Name a point such as '400 300', or a "
                "labelled element such as 'the Save button in Safari'.")


def _plan_scroll(text: str, ctx: Dict[str, Any]) -> Optional[DesktopPlan]:
    match = re.match(r"scroll\s+(?P<direction>up|down|left|right)"
                     r"(?:\s+(?:by\s+)?(?P<count>\d+))?"
                     r"(?:\s*(?:lines?|notches?|ticks?))?"
                     r"(?:\s+(?:at|on|in)\s+(?P<point>.+))?\s*$", text, re.I)
    if not match:
        return None
    direction = match.group("direction").lower()
    count = int(match.group("count")) if match.group("count") else 3
    if count == 0:
        return _ask("A scroll of zero lines would do nothing; name a line count.")
    if count > 50:
        return _ask("One scroll call carries at most 50 lines; name a smaller "
                    f"count (you asked for {count}).")
    point = _coords(match.group("point"), 2) if match.group("point") else None
    if point is None:
        point = _context_point(ctx)
    if point is None:
        return _ask("Where should I scroll? Name a point, for example "
                    "'scroll down 5 lines at 400 300'.")
    refusal = _out_of_bounds(point, "That scroll position")
    if refusal is not None:
        return refusal
    horizontal = direction in ("left", "right")
    lines = count if direction in ("up", "right") else -count
    x, y = point
    return DesktopPlan(
        steps=[PlannedStep("desktop_scroll",
                           {"x": x, "y": y, "lines": lines, "horizontal": horizontal},
                           f"Scroll {direction} {count} line(s) at "
                           f"({_fmt(x)}, {_fmt(y)})")],
        notes=["A positive line count scrolls up; a downward scroll is sent as "
               "the matching negative count."],
    )


def _plan_type(text: str, ctx: Dict[str, Any]) -> Optional[DesktopPlan]:
    match = re.match(r"type\s+(?P<text>.*)$", text, re.I | re.S)
    if not match:
        return None
    typed = _strip_outer_quotes(match.group("text").strip())
    if not typed:
        return _ask("What should I type?")
    if len(typed) > MAX_TEXT_CHARS:
        return _ask(f"That text is {len(typed)} characters; one call types at "
                    f"most {MAX_TEXT_CHARS}.")
    return DesktopPlan(steps=[PlannedStep(
        "desktop_type_text", {"text": typed},
        f"Type {len(typed)} character(s)")])


def _plan_press(text: str, ctx: Dict[str, Any]) -> Optional[DesktopPlan]:
    match = re.match(r"press\s+(?P<rest>.+?)\s*$", text, re.I)
    if not match:
        return None
    rest = match.group("rest").strip().rstrip(".!?")
    element = _element(rest)
    if element is not None:
        keyword, app = element
        return _press_element_plan(keyword, app, ctx, verb="press")
    if _looks_like_element(rest):
        return _ask("Which element should I press? Name its label, for example "
                    "'the Save button in Safari'.")
    return _key_plan(rest)


def _plan_open(text: str, ctx: Dict[str, Any]) -> Optional[DesktopPlan]:
    """Launch an application by name.

    This is a real desktop precondition, not a web request: the step a person
    needs before any pointer work is the application being open. It goes through
    the same `open_application` tool the rest of the system uses, so it carries
    that tool's risk and confirmation rather than a second path.
    """
    match = re.match(r"(?:open|launch|start)\s+(?:the\s+|a\s+|an\s+)?"
                     r"(?P<app>.+?)\s*$", text, re.I)
    if not match:
        return None
    app = _clean_app(match.group("app"))
    if not app:
        return _ask("Which application should I open?")
    if not _looks_like_one_app(app):
        return _ask(f"I read {app!r} as a single application name, and that looks "
                    "like two instructions. Which application should I open?")
    return DesktopPlan(steps=[PlannedStep(
        "open_application", {"app": app}, f"Open {app}")])


def _plan_focus(text: str, ctx: Dict[str, Any]) -> Optional[DesktopPlan]:
    match = re.match(r"(?:focus|switch\s+to|bring|activate)\s+(?P<rest>.+?)"
                     r"(?:\s+to\s+the\s+front)?\s*$", text, re.I)
    if not match:
        return None
    app = _clean_app(match.group("rest"))
    if not app:
        return _ask("Which application should I bring to the front?")
    if not _looks_like_one_app(app):
        return _ask(f"I read {app!r} as a single application name, and that looks "
                    "like two instructions. Which one should I bring to the front, "
                    "and what should I do there?")
    return DesktopPlan(steps=[PlannedStep(
        "desktop_focus_window", {"application": app},
        f"Bring {app} to the front")])


def _plan_inspect(text: str, ctx: Dict[str, Any]) -> Optional[DesktopPlan]:
    match = re.match(r"(?:read|inspect|show\s+me|show|list)\s+(?:the\s+)?"
                     r"(?:ui|interface|accessibility\s+tree|elements|controls)"
                     r"(?:\s+(?:in|of|on|for)\s+(?P<app>.+))?\s*$", text, re.I)
    if not match:
        return None
    app = _clean_app(match.group("app") or "")
    notes: List[str] = []
    if app and not _looks_like_one_app(app):
        return _ask(f"I read {app!r} as a single application name, and that looks "
                    "like two instructions. Which application should I read?")
    if not app:
        app = _context_app(ctx)
        notes.append("No application was named, so this reads the front "
                     "application." if not app else
                     f"Took the application from context: {app}.")
    return DesktopPlan(steps=[PlannedStep(
        "desktop_inspect_ui", {"application": app},
        f"Read the Accessibility tree of {app or 'the front application'}")],
        notes=notes)