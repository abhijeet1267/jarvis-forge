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
    for matcher in (_plan_move, _plan_drag, _plan_click, _plan_scroll,
                    _plan_type, _plan_press, _plan_focus, _plan_inspect):
        plan = matcher(text, ctx)
        if plan is not None:
            return plan
    return _ask("I could not understand that desktop instruction. Name a point "
                "('400 300'), a key ('cmd s'), an element ('the Save button in "
                "Safari'), or an application to focus or read.")


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