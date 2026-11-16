"""One coordinate parser, used by every path that can move the real pointer.

A screen point reaches this system through five doors — the HTTP routes, the tool
registry, the desktop planner, Automation Studio, and the manual Desktop Control
screen — and each grew its own checks. Measured on the V16 baseline, the three
Python implementations disagreed:

* ``/desktop/move`` performed **no** bounds check at all, so ``nan`` and ``inf``
  reached the native executor;
* ``desktop_move_pointer`` and the planner each re-implemented the same
  ``0 <= x <= 32000`` test in their own words;
* and both of those rejected **every negative coordinate**, which is wrong: a
  display placed to the left of the primary one has negative global x, and its
  points are valid places to click.

The disagreement is the defect. A point is valid when it is a finite number that
falls inside a real display, which is a question only the display list can
answer — so that is what this module asks.

Two rules are non-negotiable:

1. **A malformed value is never turned into a coordinate.** ``float("...") or 0``
   is how a typo became a click at the top-left corner of the screen. Every
   failure here is a refusal with a reason a person can act on.
2. **A refusal is a value, not an exception.** Callers get a
   :class:`CoordinateError` carrying the sentence to show, so no caller has to
   decide what to say and none of them diverge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

__all__ = [
    "CoordinateError",
    "Display",
    "Screen",
    "parse_axis",
    "parse_point",
    "parse_drag",
    "bounding_box",
]

#: The widest coordinate any macOS desktop can plausibly span, in points. Used
#: only as a sanity ceiling when the display list is unavailable, never as a
#: substitute for real geometry.
MAX_COORDINATE = 32000.0


class CoordinateError(ValueError):
    """A coordinate was refused, with the sentence to show the person.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers keep
    working, and carries ``detail`` for a caller that wants the structured form.
    """

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.detail = dict(detail or {})


@dataclass(frozen=True)
class Display:
    """One display's global bounds, in points, top-left origin."""

    x: float
    y: float
    width: float
    height: float
    scale: float = 1.0

    def contains(self, x: float, y: float) -> bool:
        return (self.x <= x <= self.x + self.width
                and self.y <= y <= self.y + self.height)

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


def parse_axis(value: Any, *, name: str) -> float:
    """One coordinate component as a finite float, or :class:`CoordinateError`.

    Rejects: missing values, non-numeric strings, ``NaN``, and both infinities.
    ``bool`` is rejected too — ``True`` is not 1 here, because a caller that
    passed a boolean meant something else.
    """
    if value is None:
        raise CoordinateError(f"{name} is missing; a point needs two numbers.")
    if isinstance(value, bool):
        raise CoordinateError(f"{name} was a boolean, not a number.")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise CoordinateError(f"{name} was left empty; a point needs two numbers.")
        try:
            number = float(text)
        except ValueError:
            raise CoordinateError(
                f"{name} was {value!r}, which is not a number. A point needs two "
                "number values, such as 412 and 336."
            ) from None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise CoordinateError(f"{name} was {type(value).__name__}, not a number.")

    # `float("nan")` and `float("inf")` parse happily and then poison every
    # comparison downstream: `0 <= nan <= 32000` is False for the wrong reason,
    # and a native executor given NaN posts an event at an undefined place.
    if math.isnan(number) or math.isinf(number):
        raise CoordinateError(
            f"{name} was {value!r}, which is not a finite number. A point must be "
            "a real position on a display."
        )
    return number


@dataclass(frozen=True)
class Screen:
    """The displays available right now, as the executor reported them."""

    displays: Tuple[Display, ...]

    @classmethod
    def from_bounds(cls, raw: Optional[Iterable[Dict[str, Any]]]) -> "Screen":
        """Build from the executor's ``displays`` payload.

        An absent or unreadable list yields an empty :class:`Screen`, which is
        treated as "geometry unknown" rather than "nothing is on screen".
        """
        displays = []
        for entry in raw or []:
            try:
                displays.append(Display(
                    x=float(entry.get("x", 0)), y=float(entry.get("y", 0)),
                    width=float(entry.get("width", 0)),
                    height=float(entry.get("height", 0)),
                    scale=float(entry.get("scale", 1.0) or 1.0),
                ))
            except (TypeError, ValueError):
                continue
        return cls(tuple(displays))

    @property
    def known(self) -> bool:
        return bool(self.displays)

    def containing(self, x: float, y: float) -> Optional[Display]:
        for display in self.displays:
            if display.contains(x, y):
                return display
        return None

    def describe(self) -> str:
        if not self.known:
            return "no display geometry was available"
        return "; ".join(
            f"#{index} {d.width:g}x{d.height:g} at ({d.x:g}, {d.y:g})"
            for index, d in enumerate(self.displays))

    def bounding_box(self) -> Optional[Display]:
        if not self.known:
            return None
        left = min(d.x for d in self.displays)
        top = min(d.y for d in self.displays)
        right = max(d.right for d in self.displays)
        bottom = max(d.bottom for d in self.displays)
        return Display(x=left, y=top, width=right - left, height=bottom - top)


def bounding_box(displays: Optional[Iterable[Dict[str, Any]]]) -> Optional[Dict[str, float]]:
    """The union of every display, as ``{x, y, width, height}``, or None."""
    box = Screen.from_bounds(displays).bounding_box()
    if box is None:
        return None
    return {"x": box.x, "y": box.y, "width": box.width, "height": box.height}


def parse_point(x: Any, y: Any, *, screen: Optional[Screen] = None,
                subject: str = "That point") -> Tuple[float, float]:
    """Validate one screen point.

    The display check is what makes a negative coordinate legal when a monitor
    sits left of the primary one, and illegal when it falls off the edge of the
    desktop. When ``screen`` is unknown the only check is the sanity ceiling —
    and the caller is told the geometry was unavailable rather than being given
    a false precision.
    """
    px = parse_axis(x, name="x")
    py = parse_axis(y, name="y")

    if screen is not None and screen.known:
        if screen.containing(px, py) is None:
            raise CoordinateError(
                f"{subject} ({px:g}, {py:g}) is not on any display this Mac is "
                f"using ({screen.describe()}). Give a point inside one of them.",
                detail={"x": px, "y": py, "displays": [d.__dict__ for d in screen.displays]},
            )
        return (px, py)

    # No geometry: fall back to the sanity ceiling, and refuse only what cannot
    # possibly be a screen point.
    if not (-MAX_COORDINATE <= px <= MAX_COORDINATE
            and -MAX_COORDINATE <= py <= MAX_COORDINATE):
        raise CoordinateError(
            f"{subject} ({px:g}, {py:g}) is implausibly far off any screen.",
            detail={"x": px, "y": py},
        )
    return (px, py)


def parse_drag(from_x: Any, from_y: Any, to_x: Any, to_y: Any, *,
               screen: Optional[Screen] = None) -> Tuple[float, float, float, float]:
    """Validate both ends of a drag, naming which end was wrong."""
    start = parse_point(from_x, from_y, screen=screen, subject="The drag's start")
    end = parse_point(to_x, to_y, screen=screen, subject="The drag's end")
    if start == end:
        raise CoordinateError(
            "The drag's start and end are the same point, so it would do nothing. "
            "Give a different end point."
        )
    return (*start, *end)


def parse_sequence(values: Sequence[Any], *, count: int, screen: Optional[Screen] = None,
                   subject: str = "That point") -> list:
    """Validate ``count`` points given as a flat ``[x, y, x, y, ...]`` sequence."""
    if len(values) != count * 2:
        raise CoordinateError(f"A drag needs {count} points: two numbers each.")
    return [parse_point(values[i], values[i + 1], screen=screen, subject=subject)
            for i in range(0, len(values), 2)]
