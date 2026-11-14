"""No route may be registered twice for the same method.

FastAPI/Starlette silently keeps the **first** registration and ignores later
ones, so a duplicated handler is dead code that still runs at import, shadows a
name, and emits a startup warning nobody reads. Six such pairs shipped
(`/meetings/prepare`, `/calls/availability`, `/contacts/{name}/call`,
`/contacts/{name}/call-notes`, `/contacts/{name}/outreach`,
`/meetings/{uid}/follow-up`).

This test reads the live route table, not the source text: a duplicate that a
router resolves differently from what the eye expects is exactly what it should
catch. A path registered under several *methods* is correct and is not flagged.
"""

from __future__ import annotations

import collections

import pytest


def _routes():
    from jarvis.api.app import app

    return app.routes


def test_no_path_is_registered_twice_for_the_same_method():
    seen: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
    for route in _routes():
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        endpoint = getattr(route, "endpoint", None)
        if not path or not methods or endpoint is None:
            continue
        for method in methods:
            seen[(path, method)].append(endpoint.__code__.co_firstlineno)

    duplicates = {k: v for k, v in seen.items() if len(v) > 1}

    assert not duplicates, (
        "these (path, method) pairs are registered more than once; Starlette "
        "keeps only the first, so the rest are dead code:\n"
        + "\n".join(f"  {m} {p}: definitions at lines {lines}"
                    for (p, m), lines in sorted(duplicates.items())))


def test_the_app_reports_no_duplicate_operation_ids():
    """No operation id is claimed by two routes.

    `app` is a FastAPI instance, not a module, so it cannot be re-imported to
    re-trigger the startup warning. Read the ids the OpenAPI layer would build
    instead: FastAPI warns precisely when two routes resolve to one id, and two
    routes with the same id are the same defect whether or not the warning was
    printed during this process.
    """
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(title="vantage", version="test", routes=_routes())
    seen: dict[str, int] = collections.Counter()
    for path, operations in (schema.get("paths") or {}).items():
        for method, op in operations.items():
            if not isinstance(op, dict):
                continue
            op_id = op.get("operationId")
            if op_id:
                seen[(op_id, method)] += 1

    duplicates = {k: n for k, n in seen.items() if n > 1}
    assert not duplicates, f"duplicate operation ids: {duplicates}"


def test_a_path_may_serve_several_methods():
    """The check must not be so strict that it forbids correct routing."""
    same_path = collections.defaultdict(set)
    for route in _routes():
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            same_path[path].update(methods)

    multi = [p for p, methods in same_path.items() if len(methods) > 1]
    assert multi, "expected at least one path serving more than one method"
    for path in multi:
        # Each method on that path is registered exactly once.
        counts = collections.Counter(
            m for route in _routes()
            if getattr(route, "path", None) == path
            for m in (getattr(route, "methods", None) or ())
        )
        assert all(c == 1 for c in counts.values()), (path, counts)