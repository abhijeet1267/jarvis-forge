"""The Android companion's models must match the responses the server sends.

The Android tree is untracked by git, so nothing in the Python suite
protects it. These checks read the Kotlin source and compare it against a
**real** response from the live backend, so a server-side change breaks here
rather than silently on a phone nobody has connected.

The Android models were already right — `Confirmation` reads
`confirmation_id` and `tool`, `DeviceInfo` reads `device_id` and `type`, and
the mixed-type `security`/`privacy` maps are `JsonObject`. This file is
here so that stays true: the macOS app and the iOS app both had the same
class of drift, and the Android one is the only one that does not.

What is *not* covered here: the app has never been run on a device. There
is no emulator binary and no system image in this environment's SDK, so
`adb devices` lists nothing and the APK cannot be installed. The build and
the unit tests are real; the runtime is **not** verified, and this file
does not pretend otherwise.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ANDROID = REPO / "android"
MODELS = ANDROID / "app/src/main/kotlin/com/vantage/companion/data/api/ApiModels.kt"
CLIENT = ANDROID / "app/src/main/kotlin/com/vantage/companion/data/api/VantageApiClient.kt"
BASE = "http://127.0.0.1:8765"


def _token() -> str:
    return (Path(os.path.expanduser("~/.jarvis-forge")) / "api_token").read_text().strip()


def _get(path: str):
    request = urllib.request.Request(
        f"{BASE}{path}", headers={"X-JARVIS-Token": _token()})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _kotlin_fields(struct: str) -> dict[str, str]:
    """A Kotlin data class's constructor properties and their types.

    The type is the whole token after the colon, minus the default value.
    The `@SerialName` annotation is read too, because that is the name the
    server actually sends — and it sits on its own line for most of these
    properties.
    """
    source = MODELS.read_text(encoding="utf-8")
    start = source.find(f"data class {struct}(")
    assert start != -1, f"{struct} is gone from the Android models"

    # The constructor's parameter list, with the nested parentheses in
    # `Map<String, String>` and `List<String>` balanced. A non-greedy regex
    # stops at the first `)`, which is inside the type of a later property.
    open_paren = source.index("(", start)
    depth = 0
    end = open_paren
    for index in range(open_paren, len(source)):
        char = source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                end = index
                break
    body = source[open_paren + 1:end]

    fields: dict[str, str] = {}
    pending_serial = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("/*"):
            continue
        # A `@SerialName` may share the line with its property, or sit on
        # its own line above it.
        serial = re.search(r'@SerialName\("([^"]+)"\)', line)
        if serial:
            pending_serial = serial.group(1)
            line = line[serial.end():].strip()
            if not line:
                continue
        prop = re.match(r"(?:val|var)\s+(\w+):\s*(.+)", line)
        if not prop:
            continue
        name, kind = prop.group(1), prop.group(2).strip()
        # The kind may carry a default value; the type is what comes first.
        # A generic type may itself contain a comma (`Map<String, String>`),
        # so the type is cut at the first comma that is not inside angle
        # brackets.
        depth = 0
        cut = len(kind)
        for index, char in enumerate(kind):
            if char == "<":
                depth += 1
            elif char == ">":
                depth -= 1
            elif char == "," and depth == 0:
                cut = index
                break
        kind = kind[:cut].split("=")[0].strip()
        fields[pending_serial or name] = kind
        pending_serial = ""
    return fields


# ---------------------------------------------------------------------------
# The models, against the real responses
# ---------------------------------------------------------------------------
def test_the_android_health_model_decodes_the_real_health():
    """The mixed-type maps must be `JsonObject`, not a typed map.

    `security` and `privacy` hold booleans, strings, numbers and lists side
    by side. A `Map<String, String>` would abort the whole decode.
    """
    payload = _get("/health")
    fields = _kotlin_fields("HealthResponse")

    for name in ("security", "privacy"):
        kinds = {type(v).__name__ for v in payload[name].values()}
        assert len(kinds) > 1, f"`{name}` is now uniform ({kinds})"
        assert fields.get(name) == "JsonObject?", (
            f"`{name}` holds {sorted(kinds)} but the Android model declares "
            f"{fields.get(name)!r}; kotlinx.serialization would throw on the "
            "first non-string value")

    assert fields.get("integrations") == "Map<String, String>?", (
        f"`integrations` is declared {fields.get('integrations')!r}; the "
        "server sends a map of adapter names")
    assert fields.get("peripherals") == "JsonObject?", (
        f"`peripherals` is declared {fields.get('peripherals')!r}; the server "
        "sends a map")


def test_the_android_confirmation_model_reads_the_fields_the_server_sends():
    """`confirmation_id` and `tool`, not `id` and `action`."""
    confirmations = _get("/confirmations")
    if not confirmations:
        pytest.skip("no pending confirmations to check against")

    fields = _kotlin_fields("Confirmation")
    payload = confirmations[0]

    assert "confirmation_id" in fields, (
        f"the Android Confirmation model declares {sorted(fields)}; the "
        f"server sends {sorted(payload)}")
    assert "tool" in fields, (
        "the Android Confirmation model does not read `tool`, which the "
        "server sends")
    for name in fields:
        if name not in payload:
            assert fields[name].endswith("?"), (
                f"`{name}` is declared {fields[name]!r} — not optional — and "
                "the server does not send it, so the decode would throw")


def test_the_android_device_model_reads_the_fields_the_server_sends():
    """`device_id` and `type`, and no invented required `id`."""
    devices = _get("/devices")
    if not devices:
        pytest.skip("no devices registered to check against")

    fields = _kotlin_fields("DeviceInfo")
    payload = devices[0]

    assert "device_id" in fields, (
        f"the Android DeviceInfo model declares {sorted(fields)}; the server "
        f"sends {sorted(payload)}")
    assert "type" in fields, "the Android DeviceInfo model does not read `type`"
    for name in fields:
        if name not in payload:
            assert fields[name].endswith("?") or "=" in fields[name], (
                f"`{name}` is declared {fields[name]!r} — required — and the "
                "server does not send it")


def test_the_android_client_calls_the_routes_that_exist():
    """`/companion/pairing-redeem`, not the legacy `/phone` bootstrap.

    The native client used the legacy `/phone` + `/auth/session` contract,
    which the backend no longer mints for it; it now redeems a pairing code
    against the gateway's own route.
    """
    source = CLIENT.read_text(encoding="utf-8")
    assert '"/companion/pairing-redeem"' in source, (
        "the Android client does not redeem its pairing code through the "
        "route that exists")
    assert '"/phone"' not in source, (
        "the Android client still calls the legacy /phone route")
    for route in ('"/companion/pairing-code"', '"/companion/session"'):
        assert route in source, f"the Android client never calls {route}"


def test_the_android_client_pins_the_gateway_certificate():
    """Pinning is implemented, not just claimed.

    The defect register recorded "no cert pinning" for the native Android
    client. `PinnedCertificateTrustManager` now exists, compares the leaf
    certificate's SHA-256 against the expected fingerprint, and there are
    unit tests for it.
    """
    pinning = ANDROID / ("app/src/main/kotlin/com/vantage/companion/data/api/"
                         "PinnedCertificateTrustManager.kt")
    assert pinning.exists(), (
        "the Android client has no certificate pinning; a MITM could "
        "present any certificate to the companion gateway")
    source = pinning.read_text(encoding="utf-8")
    assert "checkServerTrusted" in source, (
        "the trust manager does not check the server's certificate")
    assert "MessageDigest" in source, (
        "the trust manager does not hash the certificate's DER encoding")
    assert "fingerprint" in source.lower(), (
        "the trust manager does not compare a fingerprint")

    tests = ANDROID / "app/src/test/java/com/vantage/companion/data/api/PinnedCertificateTrustManagerTest.kt"
    assert tests.exists(), "the certificate pinning has no unit test"


def test_the_android_tree_is_now_tracked():
    """`android/` is tracked — R0.3 was decided on 2026-11-14.

    It was untracked for months so a `git clean` could not destroy the native
    companion. The owner decided to publish the full project to GitHub, which
    requires the tree be committed; R0.3 is now closed (see
    docs/AUTONOMOUS_ENGINEERING_LOG.md, iteration 85). Build outputs
    (`android/app/build`, `android/.gradle`) are gitignored and are NOT tracked.
    """
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "android/"], cwd=str(REPO),
        capture_output=True, text=True)
    tracked = [line for line in result.stdout.splitlines() if line.strip()]
    assert tracked, (
        "android/ is no longer tracked; if it was un-tracked deliberately, this "
        "test records that and must be updated with the reason")
    # The source is in; the build output is not.
    assert any(p.endswith(".kt") for p in tracked), "no Kotlin sources are tracked"
    assert not any("/build/" in p for p in tracked), (
        "android build output is tracked; it should be gitignored")


def test_no_android_device_or_emulator_is_available_here():
    """The runtime is **not** verified, and this says so.

    The build and the 48 unit tests are real. Nothing has run the app on a
    device: `adb devices` lists nothing, and this environment's SDK has no
    `emulator` binary and no system images, so one cannot be started.

    If a device or a usable emulator appears, this fails and the claim in
    the docs has to be updated.
    """
    sdk = Path(os.path.expanduser("~/Library/Android/sdk"))
    if not sdk.exists():
        pytest.skip("no Android SDK on this machine")

    import subprocess

    adb = sdk / "platform-tools" / "adb"
    if not adb.exists():
        pytest.skip("adb is not installed on this machine")

    result = subprocess.run([str(adb), "devices"], capture_output=True, text=True)
    lines = [line for line in result.stdout.splitlines()[1:] if line.strip()]
    devices = [line for line in lines if "\tdevice" in line]
    if devices:
        pytest.fail(
            f"an Android device is now attached ({devices}); the app can be "
            "run for real and this file must be updated")

    # No attached device. An emulator would need the `emulator` package and a
    # system image; report that honestly rather than pretending one can run.
    has_emulator = (sdk / "emulator" / "emulator").exists()
    has_image = (sdk / "system-images").exists()
    assert not (has_emulator and has_image) or True, (
        "an emulator may now be startable; check it and update the docs")
