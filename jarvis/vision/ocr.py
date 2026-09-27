"""OCR via the macOS Vision framework (v1.3).

No OCR library is installed (no ``pytesseract``, no ``cv2``), but macOS ships a
good text recogniser. This module compiles a tiny Swift helper against the
Vision framework on first use and caches the binary.

If the helper cannot be built — no Swift toolchain, non-macOS platform, or a
compile error — :func:`available` returns False and :func:`read_text` says so.
It never returns invented text.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("vision.ocr")

#: The Swift helper. Emits JSON so bounding boxes survive the boundary.
_SWIFT_SOURCE = r'''
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1 else {
    print("{\"error\":\"no path\"}")
    exit(1)
}
let path = args[1]
guard let image = NSImage(contentsOfFile: path),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    print("{\"error\":\"cannot load image\"}")
    exit(1)
}

var results: [[String: Any]] = []
let request = VNRecognizeTextRequest { request, error in
    guard let observations = request.results as? [VNRecognizedTextObservation] else { return }
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        results.append([
            "text": candidate.string,
            "confidence": candidate.confidence,
            "x": box.origin.x,
            "y": box.origin.y,
            "width": box.size.width,
            "height": box.size.height
        ])
    }
}
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([request])
} catch {
    print("{\"error\":\"recognition failed\"}")
    exit(1)
}

if let data = try? JSONSerialization.data(withJSONObject: ["lines": results], options: []),
   let text = String(data: data, encoding: .utf8) {
    print(text)
} else {
    print("{\"lines\":[]}")
}
'''

_TIMEOUT = 20.0

#: How long a timed-out recogniser is left alone before another attempt.
#:
#: macOS Vision is intermittently unresponsive under load (measured: the same
#: image returns in 0.6 s standalone and exceeds any bound while other work is
#: running). Retrying every call would then make each question take the full
#: timeout, which is a worse user experience than saying "it is not answering
#: right now" once and moving on. The binary still exists and still works -- this
#: is a latency guard, not a claim that OCR is missing.
_COOLDOWN_SECONDS = 45.0


def _configured_timeout() -> float:
    raw = (os.environ.get("JARVIS_OCR_TIMEOUT") or "").strip()
    if not raw:
        return _TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return _TIMEOUT
    return value if 1.0 <= value <= 120.0 else _TIMEOUT


class OCRBackend:
    """Compiles and runs the Vision OCR helper."""

    def __init__(self, settings=None) -> None:
        from ..config import get_settings

        self.settings = settings or get_settings()
        self._binary: Optional[Path] = None
        self._failure: Optional[str] = None
        #: When the recogniser last stopped answering, and for how long it is
        #: left alone. See :data:`_COOLDOWN_SECONDS`.
        self._cooldown_until = 0.0
        self._last_timeout: Optional[float] = None

    # -- responsiveness ------------------------------------------------------
    def cooling_down(self) -> float:
        """Seconds remaining on the cooldown, or 0.0 when another try is due."""
        remaining = self._cooldown_until - time.monotonic()
        return round(remaining, 1) if remaining > 0 else 0.0

    def responsive(self) -> bool:
        """Whether the helper is worth asking right now.

        Distinct from :meth:`available`: a cached binary is *present* even when
        the framework behind it is not answering, and reporting those two states
        as one would either hide a working feature or promise a stalled one.
        """
        return self.available() and self.cooling_down() == 0.0

    def state(self) -> Dict[str, Any]:
        waiting = self.cooling_down()
        return {
            "backend": "macos_vision",
            "present": self.available(),
            "responsive": waiting == 0.0 and self.available(),
            "cooling_down_seconds": waiting,
            "timeout_seconds": _configured_timeout(),
            "last_timeout_after": self._last_timeout,
            "reason": (
                f"the recogniser did not answer within {self._last_timeout:g}s and is "
                f"left alone for another {waiting:g}s" if waiting else ""
            ),
        }

    @property
    def cache_dir(self) -> Path:
        path = self.settings.home / "vision"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def binary_path(self) -> Path:
        return self.cache_dir / "vision_ocr"

    def _compile(self) -> Optional[Path]:
        """Build the helper. Returns the binary path, or None on failure."""
        if platform.system() != "Darwin":
            self._failure = "OCR uses the macOS Vision framework, and this is not macOS."
            return None

        compiler = shutil.which("swiftc")
        if compiler is None:
            self._failure = "swiftc is not installed, so I cannot build the OCR helper."
            return None

        with tempfile.TemporaryDirectory() as workdir:
            source = Path(workdir) / "ocr.swift"
            source.write_text(_SWIFT_SOURCE, encoding="utf-8")
            output = Path(workdir) / "vision_ocr"

            try:
                completed = subprocess.run(
                    [compiler, "-O", str(source), "-o", str(output)],
                    capture_output=True,
                    text=True,
                    timeout=180.0,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                self._failure = f"building the OCR helper failed: {exc}"
                return None

            if completed.returncode != 0 or not output.exists():
                detail = (completed.stderr or "").strip().splitlines()
                self._failure = (
                    "building the OCR helper failed: " + (detail[0][:120] if detail else "unknown error")
                )
                return None

            # Move it into place atomically-ish.
            target = self.binary_path
            temporary = target.with_suffix(".new")
            shutil.copy2(output, temporary)
            temporary.chmod(0o755)
            os.replace(temporary, target)

        log.info("ocr helper built", extra_fields={"path": str(self.binary_path)})
        return self.binary_path

    # -- public --------------------------------------------------------------
    def available(self) -> bool:
        if self._binary is not None:
            return True
        if self.binary_path.exists():
            self._binary = self.binary_path
            return True
        return self._compile() is not None

    def unavailable_reason(self) -> str:
        if self.available():
            return ""
        return self._failure or "OCR is not available."

    def read_text(self, path: str) -> Dict[str, Any]:
        """Recognise text in an image.

        Returns ``{"ok": True, "lines": [...], "text": "..."}`` or an honest
        failure. Never returns invented text.
        """
        target = Path(path).expanduser()
        if not target.is_file():
            return {"ok": False, "error": f"I could not find {path}."}

        if not self.available():
            return {"ok": False, "error": self.unavailable_reason()}

        waiting = self.cooling_down()
        if waiting:
            return {
                "ok": False,
                "error": (
                    f"The macOS recogniser stopped answering within {self._last_timeout:g}s, "
                    f"so I am not queueing more work on it for another {waiting:g}s. "
                    "No text was read."
                ),
                "degraded": True,
                "state": self.state(),
            }

        limit = _configured_timeout()
        try:
            completed = subprocess.run(
                [str(self._binary), str(target)],
                capture_output=True,
                text=True,
                timeout=limit,
            )
        except subprocess.TimeoutExpired:
            self._cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
            self._last_timeout = limit
            log.warning("ocr helper timed out", extra_fields={"timeout": limit})
            return {
                "ok": False,
                "error": (
                    f"The macOS recogniser did not answer within {limit:g}s, so no text was "
                    "read. Nothing here is guessed; try again shortly."
                ),
                "timed_out": True,
                "state": self.state(),
            }
        except OSError as exc:
            return {"ok": False, "error": f"OCR did not complete: {exc}"}

        if completed.returncode != 0:
            return {"ok": False, "error": f"OCR failed: {(completed.stderr or '').strip()[:120]}"}

        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": "I could not read the OCR output."}

        if "error" in payload:
            return {"ok": False, "error": str(payload["error"])}

        # A completed read clears any earlier stall.
        self._cooldown_until = 0.0
        self._last_timeout = None
        lines: List[Dict[str, Any]] = payload.get("lines", [])
        text = "\n".join(line.get("text", "") for line in lines).strip()
        return {
            "ok": True,
            "lines": lines,
            "text": text,
            "line_count": len(lines),
            "backend": "macos_vision",
        }


_backend: Optional[OCRBackend] = None


def get_ocr_backend(settings=None) -> OCRBackend:
    """The shared OCR backend (compilation is cached across calls)."""
    global _backend
    if _backend is None:
        _backend = OCRBackend(settings)
    return _backend


def available(settings=None) -> bool:
    return get_ocr_backend(settings).available()


def responsive(settings=None) -> bool:
    """Present *and* worth asking right now (see :meth:`OCRBackend.responsive`)."""
    return get_ocr_backend(settings).responsive()


def state(settings=None) -> Dict[str, Any]:
    return get_ocr_backend(settings).state()


def read_text(path: str, settings=None) -> Dict[str, Any]:
    return get_ocr_backend(settings).read_text(path)


def find_text(path: str, needle: str, settings=None) -> Dict[str, Any]:
    """Locate occurrences of ``needle`` in an image's recognised text.

    Returns the matching lines with their normalised bounding boxes. These are
    *text* locations — JARVIS can find words, not arbitrary widgets.
    """
    result = read_text(path, settings=settings)
    if not result.get("ok"):
        return result

    wanted = (needle or "").strip().lower()
    if not wanted:
        return {"ok": False, "error": "What text should I look for?"}

    matches = [
        line for line in result["lines"] if wanted in line.get("text", "").lower()
    ]
    return {
        "ok": True,
        "query": needle,
        "matches": matches,
        "match_count": len(matches),
        "backend": result.get("backend"),
    }
