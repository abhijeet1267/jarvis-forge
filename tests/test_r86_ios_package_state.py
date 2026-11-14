"""The iOS package's actual state, recorded so it cannot drift unnoticed.

**iOS is paused by the project's own instructions** ("preserve the sources;
do not make iOS work part of the project"). This file therefore does **not**
try to make it work. It records *what is true* so the next person does not
have to rediscover it, and so a change to the server's payloads — which
would make the iOS app's models wrong in a new way — is noticed.

Measured on 2026-10-22:

**It does not compile.** `swift build` in `ios/VantageAI` exits 1 with 23
error lines across four files:

| file | error |
|---|---|
| `BackendManager.swift:7` | `type 'ConnectionState' does not conform to protocol 'Equatable'` — the enum carries a `HealthResponse`, which is not `Equatable` |
| `BackendManager.swift:98` | `value of type 'NWBrowser' has no member 'browseResultsChanged'` — the Network framework's Bonjour API is `browseResultsChangedHandler`, and this spelling has never existed |
| `CompanionAPIClient.swift:148,152,164,168` | `generic parameter 'some Encodable' could not be inferred` — `post(_:body:)` declares `(some Encodable)?`, which cannot be called with no argument at all |
| `DevicesView.swift:70` | `cannot assign to value: 'error' is immutable` — the `catch` binds `error` and then tries to assign to it |
| `SetupView.swift:171` | `'navigationBarTitleDisplayMode' is unavailable in macOS` — the package declares a macOS platform, and this modifier is iOS-only |

**And its models have the same contract drift the macOS app had.** The
server's `/health` sends `security` and `privacy` as **mixed-type** maps, and
the iOS model declares both as `[String: String]?` — so the health response
would not decode even if the package compiled. `Confirmation` and
`DeviceInfo` both declare a non-optional `id` the server never sends.

The iOS app is **paused, not broken-and-being-fixed**. These are recorded
here so the state is honest, not so it can be acted on. The live path is the
macOS app and the phone companion.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IOS = REPO / "ios" / "VantageAI"
MODELS = IOS / "Sources/VantageCompanionCore/Models/APIModels.swift"
BASE = "http://127.0.0.1:8765"


def _token() -> str:
    return (Path(os.path.expanduser("~/.jarvis-forge")) / "api_token").read_text().strip()


def _get(path: str):
    request = urllib.request.Request(
        f"{BASE}{path}", headers={"X-JARVIS-Token": _token()})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _declared(struct: str) -> dict[str, str] | None:
    source = MODELS.read_text(encoding="utf-8")
    match = re.search(r"struct " + struct + r": Codable[^{]*\{(.*?)\n\}", source, re.S)
    if not match:
        return None
    fields = {}
    for name, kind in re.findall(
            r"public let (\w+):\s*(\[[^\]]+\]\??|[A-Za-z_][\w.]*\??)", match.group(1)):
        fields[name] = kind.strip()
    return fields


# ---------------------------------------------------------------------------
# The compile state
# ---------------------------------------------------------------------------
def test_the_ios_package_does_not_compile_and_this_is_recorded():
    """The failure is real, and it is *recorded* rather than left as a surprise.

    The point of this test is not that the build fails — iOS is paused, and
    a failing test would be a false alarm in a suite that is otherwise
    green. The point is that the **state is written down**: if a future
    change fixes the package, this test says so and stops claiming it does
    not compile.
    """
    if not IOS.exists():
        pytest.skip("there is no ios/ package on this tree")

    result = subprocess.run(
        ["swift", "build"], cwd=str(IOS), capture_output=True, text=True)
    errors = [line for line in (result.stdout + result.stderr).splitlines()
              if "error:" in line and "emit-module" not in line]

    if result.returncode == 0:
        # The package now builds. That is worth knowing; this file should be
        # updated to say so rather than keep claiming otherwise.
        pytest.fail(
            "the ios/ package now compiles -- the recorded state in this file "
            "is stale and must be updated")

    assert errors, (
        "the ios/ build failed without an error line; that is a different "
        "problem from the recorded one and this file must be updated")


def test_the_recorded_compile_failures_are_the_ones_that_occur():
    """The four files and the nature of their failures are pinned.

    A change to the iOS sources that fixes one of them should be visible
    here, not silently.
    """
    if not IOS.exists():
        pytest.skip("there is no ios/ package on this tree")

    result = subprocess.run(
        ["swift", "build"], cwd=str(IOS), capture_output=True, text=True)
    output = result.stdout + result.stderr
    if result.returncode == 0:
        pytest.skip("the ios/ package compiles now; nothing to pin")

    # The four files that failed, by name.
    for name in ("BackendManager.swift", "CompanionAPIClient.swift",
                 "DevicesView.swift", "SetupView.swift"):
        assert name in output, (
            f"{name} no longer appears in the build failure -- the iOS "
            "package's recorded state has changed and this file must be "
            "updated")

    # The nature of each failure, so a *different* failure is not mistaken
    # for the recorded one.
    assert "does not conform to protocol 'Equatable'" in output, (
        "the ConnectionState Equatable failure is gone -- update this file")
    assert "browseResultsChanged" in output, (
        "the NWBrowser failure is gone -- update this file")
    assert "some Encodable" in output, (
        "the post(_:body:) inference failure is gone -- update this file")
    assert "is immutable" in output, (
        "the DevicesView error-assignment failure is gone -- update this file")
    assert "navigationBarTitleDisplayMode" in output, (
        "the SetupView macOS-only failure is gone -- update this file")


# ---------------------------------------------------------------------------
# The decode state
# ---------------------------------------------------------------------------
def test_the_ios_health_model_would_not_decode_the_real_health():
    """`security` and `privacy` are mixed-type maps.

    The iOS model declares both as `[String: String]?`, so the health
    response would abort on the first non-string value — the same defect the
    macOS app had, and the same reason it had no connection state.

    The assertion is the **current, wrong** state on purpose: iOS is
    paused, so this file records what is true rather than what ought to be.
    If the iOS models are ever fixed, this fails and says so.
    """
    payload = _get("/health")
    fields = _declared("HealthResponse")
    assert fields is not None, "HealthResponse is gone from the iOS models"

    for name in ("security", "privacy"):
        kinds = {type(v).__name__ for v in payload[name].values()}
        assert len(kinds) > 1, (
            f"`{name}` is now uniform ({kinds}); the iOS model's declaration "
            "should follow it")
        assert fields.get(name) == "[String: String]?", (
            f"`{name}` holds {sorted(kinds)} and the iOS model declares "
            f"{fields.get(name)!r}; a string-typed map aborts the decode. If "
            "the iOS models have been fixed, update this file.")


def test_the_ios_peripherals_field_is_declared_as_a_string():
    """The server sends a map; the iOS model declares a string.

    Recorded as it is, not as it ought to be — iOS is paused.
    """
    payload = _get("/health")
    assert isinstance(payload.get("peripherals"), dict), (
        "the server no longer sends a map as `peripherals`")
    fields = _declared("HealthResponse")
    assert fields.get("peripherals") == "String?", (
        f"`peripherals` is declared {fields.get('peripherals')!r}; the server "
        "sends a map. If the iOS models have been fixed, update this file.")


def test_the_ios_confirmation_and_device_models_require_an_id_the_server_sends():
    """Both declare a non-optional `id` the server never sends.

    A non-optional field the server omits aborts the whole decode, so both
    the approvals list and the devices list would fail to load.
    """
    confirmations = _get("/confirmations")
    if confirmations:
        fields = _declared("Confirmation")
        assert fields is not None, "Confirmation is gone from the iOS models"
        assert "confirmation_id" in fields or "id" in confirmations[0], (
            "the iOS Confirmation model should read the field the server "
            f"sends; the server sends {sorted(confirmations[0])}")

    devices = _get("/devices")
    if devices:
        fields = _declared("DeviceInfo")
        assert fields is not None, "DeviceInfo is gone from the iOS models"
        assert "device_id" in fields, (
            "the iOS DeviceInfo model should read `device_id`, which the "
            f"server sends; the server sends {sorted(devices[0])}")
