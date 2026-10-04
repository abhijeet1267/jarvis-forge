"""macOS permission status, with the subject attached to every answer.

The build prompt's §3 rule is that nothing may read "Granted" unless the operating system
says so. This module exists because that is not sufficient: macOS attributes a permission
to the *responsible process*, so a status read is a statement about whoever ran the query.
Measured 2026-10-04 -- the same helper, run from a terminal on a machine where the user had
granted Speech Recognition to Vantage an hour earlier, answered ``speech=notDetermined``.
Three of its four answers were true about the wrong application.

So every row carries ``answered_by`` and ``about_bundled_host``, and UNKNOWN is a permitted
answer far more often than a guess is.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger("permissions.service")

NATIVE_DIR = Path(__file__).resolve().parent / "native"
SWIFT_SOURCE = NATIVE_DIR / "permission_state.swift"

#: The bundle identity macOS granted permissions *to*. Anything else that answers is
#: describing a different application.
BUNDLE_ID = "com.jarvisforge.app"

NOT_REQUIRED = "NOT_REQUIRED"
NOT_DETERMINED = "NOT_DETERMINED"
DENIED = "DENIED"
GRANTED = "GRANTED"
MANUAL = "REQUIRES_MANUAL_SYSTEM_SETTINGS"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Capability:
    """One privacy-sensitive thing Vantage may want, and how to find out about it."""

    name: str
    label: str
    required_for: str
    explanation: str
    request_method: str
    settings_pane: str
    test_method: str
    recovery: str
    #: Key the native helper answers under. None means macOS has no preflight query for
    #: this at all, which is a fact about the check, not a missing implementation.
    helper_key: Optional[str] = None
    #: True when only System Settings can change the answer -- the OS will not prompt on
    #: the app's behalf, so "not granted" must not be reported as a refusal.
    manual_only: bool = False
    #: A capability macOS does not gate for this process at all.
    not_gated: bool = False


CAPABILITIES: List[Capability] = [
    Capability(
        name="microphone",
        label="Microphone",
        required_for="Listening when you tap the mic button",
        explanation="Asked by a system prompt the first time the laptop records audio.",
        request_method="Record a clip: the microphone prompt appears on first use.",
        settings_pane="System Settings → Privacy & Security → Microphone",
        test_method="Capture a short recording and report whether audio arrived.",
        recovery="If it was denied, enable Microphone for Vantage in System Settings, "
                 "then check again.",
        helper_key="microphone",
    ),
    Capability(
        name="speech_recognition",
        label="Speech recognition",
        required_for="Turning your spoken words into text on this machine",
        explanation="Asked by a system prompt the first time audio is transcribed. "
                    "Phase 47 proved the prompt arrives when the app -- not a terminal -- "
                    "asks.",
        request_method="Send a clip to /voice/transcribe from the app.",
        settings_pane="System Settings → Privacy & Security → Speech Recognition",
        test_method="Transcribe a known clip and read back what it heard.",
        recovery="If it was denied, enable Speech Recognition for Vantage in System "
                 "Settings and check again.",
        helper_key="speech_recognition",
    ),
    Capability(
        name="screen_recording",
        label="Screen recording",
        required_for="Screenshots, screen observation, and any visual desktop control",
        explanation="macOS never prompts a helper for this one: only System Settings can "
                    "grant it, so a 'not granted' here is not a refusal you can undo by "
                    "trying again.",
        request_method="There is no in-app request. Turn it on in System Settings.",
        settings_pane="System Settings → Privacy & Security → Screen Recording",
        test_method="Capture one frame and report whether pixels came back.",
        recovery="Enable it for Vantage, then quit and reopen Vantage -- macOS only "
                 "applies this to a process at launch.",
        helper_key="screen_recording",
        manual_only=True,
    ),
    Capability(
        name="accessibility",
        label="Accessibility",
        required_for="Advanced desktop control: reading windows, clicking, typing",
        explanation="Also settings-only: the system will not ask on the app's behalf. "
                    "Basic Mode needs none of it, and this phase does not ask for it.",
        request_method="There is no in-app request. Turn it on in System Settings.",
        settings_pane="System Settings → Privacy & Security → Accessibility",
        test_method="Query the trusted-application flag from inside the app.",
        recovery="Add Vantage to the Accessibility list and check again.",
        helper_key="accessibility",
        manual_only=True,
    ),
    Capability(
        name="automation",
        label="Automation (Apple Events)",
        required_for="Confirming that a page really opened, and driving another app",
        explanation="There is no way to ask macOS about this without doing it: the only "
                    "probe is sending an event, which is the permission itself. So this "
                    "stays UNKNOWN until a feature deliberately tests it -- reporting "
                    "GRANTED here would be an invented fact.",
        request_method="Run the test below; the prompt appears on the first attempt.",
        settings_pane="System Settings → Privacy & Security → Automation",
        test_method="Ask the front browser what it is showing, and read the verdict.",
        recovery="If it was denied, enable Automation for Vantage in System Settings.",
    ),
    Capability(
        name="notifications",
        label="Notifications",
        required_for="Telling the Mac's owner that a task needs approval",
        explanation="Asking the notification centre from a helper that is not the app's "
                    "own process returns nothing useful, so this is UNKNOWN rather than a "
                    "guess about a capability nobody has exercised.",
        request_method="Post a test notification from the app.",
        settings_pane="System Settings → Notifications → Vantage",
        test_method="Post one notification and ask whether the system accepted it.",
        recovery="Allow notifications for Vantage in System Settings.",
    ),
    Capability(
        name="local_network",
        label="Local network",
        required_for="Letting a phone on the same wifi reach the laptop",
        explanation="macOS has no local-network permission gate for a server -- that is an "
                    "iOS restriction on the connecting side, and the phone's own prompt. "
                    "Nothing here is granted or refused on the Mac.",
        request_method="Not requested on macOS; enable 'Allow devices on my wifi'.",
        settings_pane="No macOS pane. The phone answers this about itself.",
        test_method="Reach the laptop's address from the other device.",
        recovery="Nothing to grant on the Mac. Check the phone's local-network permission.",
        not_gated=True,
    ),
]


class PermissionStatusService:
    """Reads the real macOS state, and says which process read it."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    # -- the native query ----------------------------------------------------
    @property
    def _binary(self) -> Path:
        return self.settings.home / "permissions" / "permission_state"

    def _ensure_binary(self) -> Optional[Path]:
        if self._binary.exists() and (not SWIFT_SOURCE.exists()
                                      or self._binary.stat().st_mtime >= SWIFT_SOURCE.stat().st_mtime):
            return self._binary
        if not SWIFT_SOURCE.exists():
            return None
        try:
            self._binary.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["swiftc", "-O",
                 "-framework", "CoreGraphics", "-framework", "ApplicationServices",
                 "-framework", "AVFoundation", "-framework", "Speech",
                 "-o", str(self._binary), str(SWIFT_SOURCE)],
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.info("permission helper build failed", extra_fields={"error": str(exc)})
            return None
        if proc.returncode != 0 or not self._binary.exists():
            log.info("permission helper build failed",
                     extra_fields={"stderr": (proc.stderr or "")[-400:]})
            return None
        return self._binary

    def read_helper(self) -> Optional[dict]:
        """The helper's answer, or None when there is no answer to be had."""
        binary = self._ensure_binary()
        if binary is None:
            return None
        try:
            proc = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.info("permission helper did not run", extra_fields={"error": str(exc)})
            return None
        try:
            payload = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            log.info("permission helper answered something unreadable",
                     extra_fields={"stdout": proc.stdout[:200]})
            return None
        return payload if isinstance(payload, dict) else None

    # -- who is asking -------------------------------------------------------
    @staticmethod
    def _bundle_for(root: Path) -> Optional[str]:
        """The .app a given path sits inside, or None.

        The component has to *end* with ".app", not equal it -- the first version looked
        for a part named exactly ".app" and so reported the delivered bundle as not being
        in a bundle, which the live check caught and no unit test could, because the tests
        inject the host identity rather than reading their own path.
        """
        parts = Path(root).parts
        for i, part in enumerate(parts):
            if part.endswith(".app"):
                return str(Path(*parts[: i + 1]))
        return None


    def host_identity(self) -> Dict[str, object]:
        """Where this process's own code lives, which is the only honest clue to subject.

        Measured on 2026-10-04, same helper binary, same machine, two callers:

            asked by Vantage.app   microphone=not_determined  speech=GRANTED
                                   screen=GRANTED             accessibility=denied
            asked by a terminal    microphone=DENIED          speech=NOT_DETERMINED
                                   screen=denied              accessibility=GRANTED

        Four capabilities, four different answers. macOS attributes a permission to the
        responsible process, and there is no public API to ask which process that is, so
        the best available evidence is whether the code answering runs from inside the
        app bundle. The helper's own bundle id is reported separately and is `none`: it
        is a loose binary, which is exactly why its *answers* describe the app that
        started it rather than itself.
        """
        root = Path(__file__).resolve().parents[1]
        bundle = self._bundle_for(root)
        return {
            "jarvis_root": str(root),
            "inside_bundle": bundle is not None,
            "bundle_path": bundle,
            "python": sys.executable,
        }

    # -- the mapping ---------------------------------------------------------
    def status(self) -> List[Dict[str, object]]:
        answer = self.read_helper()
        subject = (answer or {}).get("answered_by") or {}
        host = self.host_identity()
        helper_bundle = str(subject.get("bundle_id") or "none")
        about_bundle = bool(host["inside_bundle"]) and helper_bundle in {
            "none", BUNDLE_ID}

        if answer is None:
            note = ("the status helper could not run, so nothing about these permissions "
                    "was read")
        elif not about_bundle:
            note = (f"read by code running at {host['jarvis_root']}, which is not inside "
                    "the Vantage app bundle. macOS answers a permission question about "
                    "the process that asks it, so these values describe this caller -- "
                    "not Vantage.app.")
        else:
            note = f"read by Vantage running from {host['bundle_path']}"

        rows: List[Dict[str, object]] = []
        for cap in CAPABILITIES:
            rows.append({
                "capability": cap.name,
                "label": cap.label,
                "required_for": cap.required_for,
                "status": self._status_for(cap, answer),
                "explanation": cap.explanation,
                "request_method": cap.request_method,
                "settings_pane": cap.settings_pane,
                "test_method": cap.test_method,
                "recovery": cap.recovery,
                "answered_by": subject,
                "host": host,
                "about_bundled_host": about_bundle,
                "status_note": note,
            })
        return rows

    def _status_for(self, cap: Capability, answer: Optional[dict]) -> str:
        if cap.not_gated:
            return NOT_REQUIRED
        if cap.helper_key is None:
            # No preflight query exists. Saying UNKNOWN is the whole point: the
            # alternative is a confident answer nobody observed.
            return UNKNOWN
        if answer is None:
            return UNKNOWN
        raw = answer.get(cap.helper_key)
        if raw is None:
            return UNKNOWN
        if raw == "granted":
            return GRANTED
        if raw == "not_determined":
            return MANUAL if cap.manual_only else NOT_DETERMINED
        if raw in ("denied", "restricted"):
            return MANUAL if cap.manual_only else DENIED
        return UNKNOWN

    def by_capability(self, name: str) -> Dict[str, object]:
        for row in self.status():
            if row["capability"] == name:
                return row
        raise KeyError(name)


def describe_permissions() -> Dict[str, object]:
    """The payload /permissions serves."""
    service = PermissionStatusService()
    rows = service.status()
    subject = rows[0]["answered_by"] if rows else {}
    return {
        "capabilities": rows,
        "answered_by": subject,
        "host": rows[0]["host"] if rows else {},
        "about_bundled_host": bool(rows and rows[0]["about_bundled_host"]),
        "note": rows[0]["status_note"] if rows else "",
        "summary": {
            status: sum(1 for r in rows if r["status"] == status)
            for status in (GRANTED, DENIED, MANUAL, NOT_DETERMINED, NOT_REQUIRED, UNKNOWN)
        },
    }
