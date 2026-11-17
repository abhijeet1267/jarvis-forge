"""The Command Center journey, against the live app, with observed effects.

Every assertion here is about the world, not about a response body: a denied
plan leaves no file, an approved plan leaves exactly the approved bytes, and a
plan that has not been answered writes nothing at all.

Run with the backend up:

    .venv/bin/python benchmarks/v23_command_center/journey.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.conftest import TEST_TOKEN
from fastapi.testclient import TestClient
from jarvis.api.app import app

HEADERS = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}


def _client():
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _write_mission(client, target: str, content: str) -> str:
    return client.post("/missions", headers=HEADERS, json={
        "request": "write a note",
        "steps": [{"step_id": "S1", "title": "write", "tool": "write_file",
                   "params": {"path": target, "content": content}}],
    }).json()["mission_id"]


def _confirmation(mission: dict) -> str:
    for step in mission.get("steps", []):
        detail = step.get("detail") or ""
        if "confirmation " in detail:
            return detail.split("confirmation ", 1)[1].split()[0].rstrip(")").strip()
    raise AssertionError("the parked step named no confirmation")


def _step(mission: dict, step_id: str = "S1") -> dict:
    return next(s for s in mission["steps"] if s["step_id"] == step_id)


def deny_leaves_no_trace(tmp="/tmp/vantage_journey_deny.txt") -> str:
    client = _client()
    if os.path.exists(tmp):
        os.unlink(tmp)
    mission_id = _write_mission(client, tmp, "should not exist")

    parked = client.post(f"/missions/{mission_id}/run", headers=HEADERS, json={}).json()
    assert parked["state"] == "awaiting_approval", parked["state"]
    assert not os.path.exists(tmp), "a parked plan wrote its file before any decision"

    denied = client.post(f"/missions/{mission_id}/steps/S1/decide",
                         headers=HEADERS, json={"decision": "deny"}).json()
    assert denied["state"] == "blocked", denied["state"]

    final = client.get(f"/missions/{mission_id}", headers=HEADERS).json()
    step = _step(final)
    assert step["status"] == "failed", step["status"]
    assert "denied" in (step["detail"] or "").lower(), step["detail"]
    assert not os.path.exists(tmp), "a denied plan wrote its file anyway"
    return "deny: parked -> blocked, no file written"


def approve_writes_exactly_what_was_approved(tmp="/tmp/vantage_journey_approve.txt") -> str:
    client = _client()
    if os.path.exists(tmp):
        os.unlink(tmp)
    content = "approved by a person"
    mission_id = _write_mission(client, tmp, content)

    parked = client.post(f"/missions/{mission_id}/run", headers=HEADERS, json={}).json()
    assert parked["state"] == "awaiting_approval", parked["state"]
    confirmation = _confirmation(parked)
    assert not os.path.exists(tmp), "a parked plan wrote its file before approval"

    resumed = client.post(f"/missions/{mission_id}/run", headers=HEADERS,
                          json={"approval": {"confirmation_id": confirmation,
                                             "lease_id": ""}}).json()
    assert resumed["state"] == "partially_completed", resumed["state"]
    assert _step(resumed)["status"] == "done", _step(resumed)
    assert os.path.exists(tmp), "an approved plan reported done but wrote nothing"
    assert open(tmp).read() == content, "the file does not match what was approved"
    return "approve: parked -> done, file matches the approved bytes"


def a_forged_id_is_not_an_approval(tmp="/tmp/vantage_journey_forged.txt") -> str:
    client = _client()
    if os.path.exists(tmp):
        os.unlink(tmp)
    mission_id = _write_mission(client, tmp, "forged")

    parked = client.post(f"/missions/{mission_id}/run", headers=HEADERS, json={}).json()
    issued = _confirmation(parked)

    attempted = client.post(f"/missions/{mission_id}/run", headers=HEADERS,
                            json={"approval": {"confirmation_id": "deadbeef",
                                               "lease_id": ""}}).json()
    assert attempted["state"] == "awaiting_approval", attempted["state"]
    assert _confirmation(attempted) == issued, \
        "a rejected id rotated the live confirmation"
    assert not os.path.exists(tmp), "a forged id executed the write"
    return "forged: refused, no file, issued id unchanged"


def main() -> int:
    checks = [deny_leaves_no_trace, approve_writes_exactly_what_was_approved,
              a_forged_id_is_not_an_approval]
    failed = 0
    for check in checks:
        try:
            print(f"PASS  {check()}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {check.__name__}: {exc}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
