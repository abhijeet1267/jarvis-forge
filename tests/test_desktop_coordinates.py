"""The coordinate parser, and the guarantee that a bad one moves nothing.

V16 §7 asks for malformed input to be refused *without a native cursor event*.
That second half is the part that matters: a validator that returns an error
after the pointer has already moved is not a guard. These tests therefore read
the pointer position before and after every refusal and compare.

The measured baseline this replaced: `/desktop/move` performed no bounds check
at all, so `nan` and `inf` reached the native executor; and the two Python copies
of the check both rejected every negative coordinate, which is a valid point on a
display placed left of the primary one.
"""
from __future__ import annotations

import pytest

from jarvis.desktop import executor
from jarvis.desktop.coordinates import (
    CoordinateError, Screen, bounding_box, parse_axis, parse_drag, parse_point,
)

ONE_DISPLAY = Screen.from_bounds([
    {"x": 0, "y": 0, "width": 1352, "height": 878, "scale": 1}])


# -- the parser itself --------------------------------------------------------

@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_a_non_finite_number_is_refused_not_turned_into_a_coordinate(value):
    """`float("nan")` parses, then poisons every comparison downstream."""
    with pytest.raises(CoordinateError):
        parse_axis(value, name="x")


@pytest.mark.parametrize("value", [None, "", "  ", "not_a_number", "12abc", True, False])
def test_a_non_numeric_value_is_refused(value):
    with pytest.raises(CoordinateError):
        parse_axis(value, name="x")


@pytest.mark.parametrize("value,expected", [
    (412, 412.0), ("412", 412.0), (412.5, 412.5), ("-1", -1.0), (-1, -1.0)])
def test_a_real_number_parses(value, expected):
    assert parse_axis(value, name="x") == expected


def test_a_point_on_the_display_is_accepted():
    assert parse_point(412, 336, screen=ONE_DISPLAY) == (412.0, 336.0)


@pytest.mark.parametrize("x,y", [(2000, 300), (-100, 300), (400, 5000)])
def test_a_point_off_every_display_is_refused(x, y):
    with pytest.raises(CoordinateError) as caught:
        parse_point(x, y, screen=ONE_DISPLAY)
    assert "display" in str(caught.value).lower()


def test_a_negative_point_is_valid_when_a_display_sits_to_the_left():
    """This is the case the old blind `0 <= x` check got wrong.

    A monitor left of the primary one has negative global x. Refusing it made a
    perfectly ordinary click on a second display impossible.
    """
    two = Screen.from_bounds([
        {"x": 0, "y": 0, "width": 1352, "height": 878},
        {"x": -1920, "y": 0, "width": 1920, "height": 1080},
    ])
    assert parse_point(-500, 300, screen=two) == (-500.0, 300.0)
    box = bounding_box([{"x": 0, "y": 0, "width": 1352, "height": 878},
                        {"x": -1920, "y": 0, "width": 1920, "height": 1080}])
    assert box == {"x": -1920.0, "y": 0.0, "width": 3272.0, "height": 1080.0}


def test_unknown_geometry_falls_back_to_the_sanity_ceiling_not_to_silence():
    """With no display list the only honest check is the implausible bound."""
    assert parse_point(412, 336, screen=None) == (412.0, 336.0)
    with pytest.raises(CoordinateError):
        parse_point(999_999, 336, screen=None)


def test_a_drag_to_the_same_point_is_refused_because_it_would_do_nothing():
    with pytest.raises(CoordinateError) as caught:
        parse_drag(100, 100, 100, 100, screen=ONE_DISPLAY)
    assert "same point" in str(caught.value).lower()


def test_a_drag_names_which_end_was_wrong():
    with pytest.raises(CoordinateError) as caught:
        parse_drag(100, 100, 9000, 100, screen=ONE_DISPLAY)
    assert "end" in str(caught.value).lower()


# -- the part that actually matters: the pointer does not move ----------------

def _available() -> bool:
    return executor.executor_path() is not None


def _cursor():
    posture = executor.permissions()
    cursor = posture.get("cursor") or {}
    if cursor.get("x") is None:
        return None
    return (float(cursor["x"]), float(cursor["y"]))


@pytest.mark.parametrize("x,y", [
    ("336", "not_a_number"),
    ("nan", "300"),
    ("inf", "300"),
    (None, "300"),
    ("", "300"),
])
def test_a_refused_point_leaves_the_real_pointer_where_it_was(x, y):
    """The refusal must be a refusal, not a move to (0, 0).

    Reading the position back is the whole point: a validator that errors after
    posting would still pass every assertion that only inspects the error.
    """
    if not _available():
        pytest.skip("the native executor is not built")
    before = _cursor()
    if before is None:
        pytest.skip("the pointer position is not readable on this machine")

    from jarvis.tools import build_default_registry
    from jarvis.config import get_settings
    from jarvis.tools.base import ToolContext
    import asyncio

    registry = build_default_registry()
    ctx = ToolContext(settings=get_settings(), device_id="laptop")
    handler = registry.handler("desktop_move_pointer")
    result = asyncio.run(handler(ctx, x=x, y=y))

    assert result.ok is False, f"a malformed point was accepted: {x!r}, {y!r}"
    after = _cursor()
    assert after == before, (
        f"the pointer moved from {before} to {after} on a refused coordinate")
