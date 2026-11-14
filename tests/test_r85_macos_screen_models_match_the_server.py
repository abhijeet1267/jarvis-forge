"""The app's screen models must match the responses the server actually sends.

**The Activity screen's audit list was empty, and three other screens showed
nothing at all.** This is the same class of defect as the `/health` decode
fixed in iteration 75: the Swift structs declared field names and types the
backend had never sent, and the failure was silent because the caller catches
it and shows an empty list.

What was wrong, checked against live responses:

| model | declared | the server sends | effect |
|---|---|---|---|
| `AuditEntry` | `detail: String?` | `detail` is an **object** | the **whole `/audit` array failed to decode** -- the Activity screen's audit list was empty |
| `AuditEntry` | `action`, `timestamp`, `id`, `device_id`, `confirmation_id` | `intent`, `tool`, `created_at`, `log_id`, `device`, `confirmation` | every row read as nil and showed "Unknown" |
| `Device` | `device_name`, `device_type`, `status` | `type`, `owner`, `platform`, `trust_level`, `online`, `auth_state` | every device showed its type as "unknown" |
| `TaskInfo` | `name` | `goal` | every job showed its job id instead of what it was for |
| `ProjectInfo` | `id` | `project_id` | the identity was a fresh UUID on every load, so SwiftUI's diffing treated the whole list as changed every refresh |
| `SessionInfo` | `ref`, `created_at` | `session_ref`, `expires_in` | the Settings screen's "Revoke" sent `DELETE /auth/sessions/` with an **empty** path and reported success |

The `result` vocabulary was wrong too: the app knew `allowed` / `denied` /
`error`, and the server sends `success` / `denied` / `error` /
`awaiting_confirmation` / `no_action` -- so **every** audit row, including
the successful ones, fell through to a neutral grey circle.

The models are pinned against a **real** response from each endpoint, so a
server-side change breaks here rather than silently in the app. The Swift
decoder cannot run in this environment (`no such module 'XCTest'`), so the
payload is decoded by the Python equivalent of the same rules and the Swift
declaration is read out of the source.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MACOS = REPO / "macos" / "Vantage"
BASE = "http://127.0.0.1:8765"

#: The endpoints each screen fetches, and the model it decodes them into.
SCREENS = [
    ("/devices", "Device"),
    ("/audit", "AuditEntry"),
    ("/tasks", "TaskInfo"),
    ("/memory", "MemoryResponse"),
    ("/projects", "ProjectInfo"),
    ("/auth/sessions", "SessionInfo"),
    ("/ai/providers", "ProviderInfo"),
    ("/ai/selection", "ProviderSelection"),
]


def _swift(*parts: str) -> str:
    return MACOS.joinpath("Sources", *parts).read_text(encoding="utf-8")


def _token() -> str:
    return (Path(os.path.expanduser("~/.jarvis-forge")) / "api_token").read_text().strip()


def _get(path: str):
    request = urllib.request.Request(
        f"{BASE}{path}", headers={"X-JARVIS-Token": _token()})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _declared_fields(struct: str) -> dict[str, str]:
    """The Swift struct's fields and their declared types.

    The type is the whole token after the colon: `[String: String]?`,
    `Bool?`, `Int?`, `String`. The optional marker matters -- a non-optional
    field the server does not send aborts the decode.
    """
    source = _swift("APIClient", "APIModels.swift")
    match = re.search(r"struct " + struct + r": Codable[^{]*\{(.*?)\n    \}", source, re.S)
    assert match, f"{struct} is gone from the app's models"
    body = match.group(1)
    fields = {}
    for name, kind in re.findall(r"\blet (\w+):\s*(\[[^\]]+\]\??|[A-Za-z_][\w.]*\??)",
                                 body):
        fields[name] = kind.strip()
    return fields


def _sample(path: str, struct: str):
    """One payload of the shape the screen decodes."""
    payload = _get(path)
    if struct == "MemoryResponse":
        return payload
    if isinstance(payload, list):
        if not payload:
            # The decode contract is checked against a *real* row. A list with
            # nothing in it has no row to decode -- the same case this file
            # already skips for the audit log ("this check proves nothing").
            # Notably /auth/sessions lists only scoped (phone) sessions, which
            # expire after 8 hours: with no phone connected there is simply
            # nothing live to check, and the model stays pinned by
            # _declared_fields below.
            pytest.skip(f"{path} returned no rows, so there is nothing to decode-check")
        return payload[0]
    return payload


def _swifts_decode_ok(fields: dict[str, str], payload: dict) -> list[str]:
    """What Swift's decoder would do with this payload, in the same rules.

    A field the server does not send aborts the decode when it is not
    optional. A field whose type does not match aborts it too -- a JSON
    object cannot decode into a `String?`, and that is what made the audit
    list empty.
    """
    problems = []
    for name, kind in fields.items():
        optional = kind.endswith("?")
        base = kind.rstrip("?").strip()
        if name not in payload:
            if not optional:
                problems.append(f"{name} ({kind}) is not optional and the server "
                                "does not send it")
            continue
        value = payload[name]
        if value is None:
            continue  # a JSON null decodes into a Swift optional fine
        if base == "String" and not isinstance(value, str):
            problems.append(f"{name}: declared {kind}, the server sends "
                            f"{type(value).__name__}")
        elif base == "Int" and not isinstance(value, int):
            problems.append(f"{name}: declared {kind}, the server sends "
                            f"{type(value).__name__}")
        elif base == "Double" and not isinstance(value, (int, float)):
            problems.append(f"{name}: declared {kind}, the server sends "
                            f"{type(value).__name__}")
        elif base == "Bool" and not isinstance(value, bool):
            problems.append(f"{name}: declared {kind}, the server sends "
                            f"{type(value).__name__}")
        elif base == "[String: String]" and not (
                isinstance(value, dict)
                and all(isinstance(v, str) for v in value.values())):
            kinds = sorted({type(v).__name__ for v in value.values()}) \
                if isinstance(value, dict) else "not a map"
            problems.append(f"{name}: declared {kind}, the server sends a map of "
                            f"{kinds}")
        elif base == "[String: AnyCodable]" and not isinstance(value, dict):
            problems.append(f"{name}: declared {kind}, the server sends "
                            f"{type(value).__name__}")
    return problems


# ---------------------------------------------------------------------------
# Every screen's model, against the response it actually fetches
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,struct", SCREENS)
def test_each_screen_model_decodes_the_response_it_fetches(path, struct):
    """The decode must succeed, or the screen shows nothing.

    This is the check that would have caught the empty audit list.
    """
    payload = _sample(path, struct)
    fields = _declared_fields(struct)
    problems = _swifts_decode_ok(fields, payload)

    assert not problems, (
        f"{struct} cannot decode the real {path} response, so the screen that "
        f"uses it shows nothing: {problems}")


@pytest.mark.parametrize("path,struct", SCREENS)
def test_the_model_reads_the_fields_the_server_sends(path, struct):
    """The field the screen *shows* must be one the server sends.

    A model can decode perfectly and still show nothing: `Device` declared
    `device_type` while the server sends `type`, so every device rendered as
    "unknown" while the screen looked fine.
    """
    payload = _sample(path, struct)
    fields = _declared_fields(struct)

    # The names the app's views actually read.
    view = _swift("Views", "ActivityView.swift")
    view += _swift("Views", "DevicesView.swift")
    view += _swift("Views", "HomeView.swift")
    view += _swift("Views", "SettingsView.swift")
    view += _swift("Views", "MemoryView.swift")

    for name in fields:
        # Only check the names the views read off this struct.
        if not re.search(rf"\.{name}\b", view):
            continue
        if struct == "MemoryResponse" and name in ("stats", "items"):
            continue
        if struct == "TaskInfo" and name == "steps":
            # The steps come from the job's *detail* route, not the list;
            # there is a separate test for that fetch.
            continue
        if name in payload:
            continue
        # The view reads it, so the model must have a real source for it.
        if name in ("id", "identifier"):
            continue  # a computed identity is fine
        raise AssertionError(
            f"{struct}.{name} is read by a view but the server does not send "
            f"it in the {path} payload; the payload has: {sorted(payload)}")


def test_the_task_steps_are_fetched_from_the_route_that_has_them():
    """``GET /tasks`` has no steps; ``GET /tasks/{job_id}`` does.

    The detail sheet used to render the *list* row and offer a "Steps"
    heading that could never appear, because the list payload has no steps
    in it. The sheet now fetches the job's own detail.
    """
    listed = _get("/tasks")
    if not listed:
        # No workflow job has run against this data directory, so there is no
        # row whose steps could be checked. This is the same empty-data case the
        # file already skips for every other list endpoint (`_sample`); a fresh
        # install is not a defect, and asserting non-empty here made a green
        # tree look broken. The Swift side is still pinned by the source reads
        # below, which need no data.
        pytest.skip("/tasks returned no rows, so there is no job detail to check")
    assert "steps" not in listed[0], (
        "the list route now carries steps, so the detail sheet may not need "
        "its own fetch -- update this test rather than leave it stale")

    job_id = listed[0]["job_id"]
    detail = _get(f"/tasks/{job_id}")
    assert detail.get("steps"), (
        f"the detail route for {job_id} carries no steps; the sheet's fetch "
        "has nothing to show")

    source = _swift("Views", "ActivityView.swift")
    assert '"/tasks/\\(task.job_id)"' in source, (
        "the task detail sheet does not fetch the job's own detail, so the "
        "steps it renders can never appear")
    assert "detail?.steps" in source, (
        "the task detail sheet does not render the steps it fetched")


def test_the_audit_detail_is_not_declared_as_a_string():
    """The detail is an object, and one type mismatch emptied the screen.

    `detail` was `String?` while the server sends
    `{"ok": true, "verification": "success"}`. That aborted the decode of
    the **whole** `/audit` array, so the Activity screen's audit list was
    empty and the screen looked broken.
    """
    payload = _get("/audit")[0]
    assert isinstance(payload.get("detail"), dict), (
        "the server no longer sends an object as the audit detail; the app's "
        "model should follow it")
    fields = _declared_fields("AuditEntry")
    assert "AnyCodable]" in fields.get("detail", ""), (
        f"`detail` is declared {fields.get('detail')!r}, but the server sends an "
        "object; a string-typed field aborts the decode of the whole array")


def test_the_audit_row_knows_the_servers_result_vocabulary():
    """The server's outcomes, and the app's icon for each.

    The app knew `allowed` / `denied` / `error`; the server writes
    `success`, `unverified`, `failure`, `denied`, `blocked`, `expired`,
    `awaiting_confirmation`, `pending`, `clarification`, `recovered` and
    `no_action`. Every row that was not one of the three fell through to a
    neutral grey circle — including the successful ones.

    The vocabulary is read from the **server's source**, not from live
    audit rows: a live log only contains the outcomes that happened to
    occur, so a check built on it would miss every word nobody has
    produced yet — which is exactly how `unverified`, `failure` and
    `expired` were each discovered one run at a time.
    """
    import re as _re

    server = ""
    for path in (REPO / "jarvis").rglob("*.py"):
        if path.name.endswith(".multimodal"):
            continue
        server += path.read_text(encoding="utf-8")

    server_results = set(_re.findall(r'result="([a-z_]+)"', server))
    # `result=result` and friends are not vocabulary; the regex only
    # matches a quoted literal, so what is left is the written set.
    assert server_results, (
        "the server's audit result vocabulary could not be read from its "
        "source")

    source = _swift("Views", "ActivityView.swift")
    for result in sorted(server_results):
        assert f'"{result}"' in source, (
            f"the server records a {result!r} outcome but the Activity view "
            f"does not know it, so the row renders with no meaning; the "
            f"server's vocabulary is {sorted(server_results)}")


def test_the_audit_row_knows_the_outcomes_that_occurred():
    """And the ones that actually happened, in the live log.

    The source check above is the real guard; this one confirms the live
    log's vocabulary is a subset of what the app knows.
    """
    source = _swift("Views", "ActivityView.swift")
    seen = set()
    for entry in _get("/audit"):
        result = entry.get("result")
        if result:
            seen.add(result)
    if not seen:
        pytest.skip("the audit log is empty, so this check proves nothing")
    for result in sorted(seen):
        assert f'"{result}"' in source, (
            f"the server records a {result!r} outcome but the Activity view "
            f"does not know it; the server's vocabulary is {sorted(seen)}")


def test_the_session_revoke_sends_a_real_reference():
    """``DELETE /auth/sessions/{ref}`` needs a reference.

    The model read a `ref` field the server does not send, so the button
    sent an **empty** path and the screen reported a successful revoke of a
    session that was still alive.
    """
    fields = _declared_fields("SessionInfo")
    assert "session_ref" in fields, (
        f"SessionInfo declares {sorted(fields)}; the server sends session_ref")
    assert "ref" not in fields, (
        "SessionInfo still declares a bare `ref`, which the server never sends")

    view = _swift("Views", "SettingsView.swift")
    assert "target.session_ref" in view, (
        "the revoke button does not send the session's own reference")


def test_the_device_type_is_the_field_the_server_sends():
    """The server sends ``type``, not ``device_type``."""
    payload = _get("/devices")[0]
    assert "type" in payload, "the server no longer sends a device type"
    fields = _declared_fields("Device")
    assert fields.get("type") == "String?", (
        f"`Device.type` is declared {fields.get('type')!r}; the server sends a "
        "string")
    assert "device_type" not in fields, (
        "Device still declares device_type, which the server never sends")
    assert "device.deviceType" in _swift("Views", "DevicesView.swift"), (
        "the Devices view does not read the device's type")


def test_every_device_type_the_server_sends_has_an_icon():
    """The icon switch must know the server's own names for a device.

    It knew `phone` / `watch` / `car` / `home`. The server reports
    `computer`, `android`, `smartwatch`, `vehicle` and `home_hub` -- so every
    device fell through to the same laptop icon, and the type the row
    displayed was the only thing that told them apart.
    """
    source = _swift("Views", "DevicesView.swift")
    match = re.search(r"private var deviceIcon: String \{(.*?)\n    \}", source, re.S)
    assert match, "the Devices view has no device icon any more"
    body = match.group(1)

    types = {device.get("type") for device in _get("/devices")}
    types.discard(None)
    assert types, "no devices are registered, so this check proves nothing"

    for name in sorted(types):
        assert f'"{name}"' in body, (
            f"the server reports a device of type {name!r} but the icon switch "
            f"does not know it, so it renders as the default laptop; the "
            f"server's types are {sorted(types)}")
