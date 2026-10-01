"""Vision providers (v1.3).

Semantic understanding — "what is in this picture?" — needs a vision model.
None is configured here, so the default is the **local measurement backend**:
dimensions, colours, brightness, OCR text, change detection. All real, none
pretending to understand content.

A remote provider can be plugged in later. Until one is configured and enabled,
:func:`describe` says plainly what JARVIS can and cannot do, because claiming
to have seen something it has not is the one failure this agent must never
have.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from ..net import http_read

log = get_logger("vision.providers")


class VisionProvider:
    """Base class for a vision backend."""

    name = "base"
    understands_content = False

    def __init__(self, settings=None) -> None:
        from ..config import get_settings

        self.settings = settings or get_settings()

    def available(self) -> bool:
        return False

    def describe_image(self, path: str) -> Dict[str, Any]:
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available(),
            "understands_content": self.understands_content,
        }


class LocalMeasurementProvider(VisionProvider):
    """What can be measured from the pixels, with no model."""

    name = "local_measurement"
    understands_content = False

    def available(self) -> bool:
        from .image import available as pil_available

        return pil_available()

    def describe_image(self, path: str) -> Dict[str, Any]:
        from .image import inspect

        return inspect(path)

    def describe(self) -> Dict[str, Any]:
        base = super().describe()
        base["note"] = (
            "Measures pixels: size, colours, brightness, OCR text, change. "
            "Cannot describe what is depicted."
        )
        return base


#: Total budget for one vision call. The literal 60 it replaced bounded silence
#: between bytes, not the wait, so a server that trickled could hold a chat turn open
#: for as long as it liked.
REQUEST_TIMEOUT = 60.0


class RemoteVisionProvider(VisionProvider):
    """A vision-capable model endpoint, when one is configured."""

    name = "remote_vision"
    understands_content = True

    def __init__(self, settings=None) -> None:
        super().__init__(settings)
        self.base_url = getattr(self.settings, "llm_base_url", "")
        self.api_key = getattr(self.settings, "llm_api_key", "")

    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    def describe_image(self, path: str) -> Dict[str, Any]:
        if not self.available():
            return {"ok": False, "error": "No vision model is configured."}

        import base64
        import json
        import urllib.request
        from pathlib import Path

        target = Path(path).expanduser()
        if not target.is_file():
            return {"ok": False, "error": f"I could not find {path}."}

        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
        payload = json.dumps(
            {
                "model": getattr(self.settings, "vision_model", "") or "gpt-4o-mini",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this image concisely."},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{encoded}"},
                            },
                        ],
                    }
                ],
                "max_tokens": 300,
            }
        ).encode()

        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            # A total budget, not a silence budget: see jarvis/net.py. This call sits
            # inside a chat turn, so "60 seconds" has to mean sixty.
            body = json.loads(http_read(request, total=REQUEST_TIMEOUT,
                                        what="the vision model").decode())
        except Exception as exc:
            return {"ok": False, "error": f"the vision model could not be reached: {exc}"}

        description = body["choices"][0]["message"]["content"]
        return {"ok": True, "description": description, "backend": self.name}

    def describe(self) -> Dict[str, Any]:
        base = super().describe()
        base["note"] = "Vision model endpoint. Understands content when configured."
        return base


def providers(settings=None) -> List[VisionProvider]:
    return [
        RemoteVisionProvider(settings),
        LocalMeasurementProvider(settings),
    ]


def active_provider(settings=None) -> VisionProvider:
    """The provider in use: a configured model if there is one, else local."""
    for provider in providers(settings):
        if provider.available() and provider.understands_content:
            return provider
    return LocalMeasurementProvider(settings)


def describe(settings=None) -> Dict[str, Any]:
    """Vision posture, for /health."""
    active = active_provider(settings)
    from .ocr import available as ocr_available
    from .ocr import state as ocr_state

    present = ocr_available(settings)
    state = ocr_state(settings)
    return {
        "active": active.name,
        "understands_content": active.understands_content,
        "ocr": present,
        #: "ocr" only answers "is the helper there". This answers "will it answer
        #: me if I ask now" -- the two differ when macOS Vision stalls.
        "ocr_state": state,
        "providers": [p.describe() for p in providers(settings)],
    }
