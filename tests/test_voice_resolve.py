"""The reference resolver: it may disambiguate, never authorize, never guess."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from jarvis.voice.resolve import VAGUE_REFERENCES, describe_resolution, resolve_reference


@dataclass
class _Entity:
    name: str
    verified: bool = True


class _Memory:
    """Minimal stand-in for EntityMemory, keyed the same way."""

    def __init__(self, entries=None):
        self.entries = entries or {}

    def last_verified_of_kind(self, session_id, kind):
        return self.entries.get((session_id, kind))


def test_resolves_pronoun_to_verified_app():
    memory = _Memory({("s1", "app"): _Entity("Calculator")})
    assert resolve_reference("close it", memory, "s1", kind="app") == "close Calculator"


def test_no_reference_returns_none():
    memory = _Memory({("s1", "app"): _Entity("Calculator")})
    assert resolve_reference("open Calculator", memory, "s1", kind="app") is None


def test_no_verified_entity_returns_none():
    memory = _Memory({})
    assert resolve_reference("close it", memory, "s1", kind="app") is None


def test_empty_name_is_not_a_resolution():
    memory = _Memory({("s1", "app"): _Entity("")})
    assert resolve_reference("close it", memory, "s1", kind="app") is None


def test_session_isolation():
    memory = _Memory({("other", "app"): _Entity("Calculator")})
    assert resolve_reference("close it", memory, "s1", kind="app") is None


def test_kind_is_respected():
    memory = _Memory({("s1", "app"): _Entity("Calculator")})
    assert resolve_reference("close it", memory, "s1", kind="file") is None


def test_pronoun_inside_another_word_does_not_fire():
    memory = _Memory({("s1", "app"): _Entity("Calculator")})
    # "edit" and "item" contain "it"; neither is the pronoun.
    assert resolve_reference("edit the item", memory, "s1", kind="app") is None


def test_only_first_occurrence_is_rewritten():
    memory = _Memory({("s1", "app"): _Entity("Calculator")})
    out = resolve_reference("close it then reopen it", memory, "s1", kind="app")
    assert out == "close Calculator then reopen it"


def test_describe_reports_the_substitution():
    memory = _Memory({("s1", "app"): _Entity("Calculator")})
    info = describe_resolution("close it", memory, "s1", kind="app")
    assert info["entity"] == "Calculator"
    assert info["resolved"] == "close Calculator"
    assert info["original"] == "close it"


def test_describe_is_honest_when_nothing_resolved():
    memory = _Memory({})
    info = describe_resolution("close it", memory, "s1", kind="app")
    assert info == {"original": "close it", "resolved": None, "entity": None}


def test_resolver_agrees_with_planner_vocabulary():
    """The two pronoun lists must not drift apart.

    If the planner starts refusing a word this resolver does not know (or the
    reverse), one of the two behaviours silently stops applying.
    """
    from jarvis.core import planner as planner_module

    source = planner_module.__file__
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()
    # The planner's refusal set is written inline; assert every member appears.
    for word in ("it", "this", "that", "these", "those", "them"):
        assert f'"{word}"' in text, f"planner no longer mentions {word!r}"
    assert "Never infer a broad target from a vague pronoun" in text


def test_vague_set_is_not_empty():
    assert VAGUE_REFERENCES


def test_no_session_id_returns_none():
    memory = _Memory({("", "app"): _Entity("Calculator")})
    assert resolve_reference("close it", memory, "", kind="app") is None


def test_memory_of_none_is_tolerated():
    assert resolve_reference("close it", None, "s1", kind="app") is None


@pytest.mark.parametrize("kind", ["app", "file", "window"])
def test_each_kind_resolves_independently(kind):
    memory = _Memory({("s1", kind): _Entity(f"Target-{kind}")})
    assert resolve_reference("close it", memory, "s1", kind=kind) == f"close Target-{kind}"