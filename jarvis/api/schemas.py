"""Request and response models for the Vantage API.

These were inline in ``jarvis/api/app.py``; they live here so the API layer owns its
contract in one place, and so a route module can import a body model without importing
the whole application (which would create a circular import).

They are re-exported from ``jarvis.api.app`` for backward compatibility: anything that
did ``from jarvis.api.app import CommandBody`` keeps working.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, field_validator

from ..core.models import MAX_COMMAND_CHARS


class CommandBody(BaseModel):
    """One natural-language command from a surface (API, CLI, voice, watch)."""

    text: str
    session_id: Optional[str] = None
    device_id: str = "laptop_main"
    source: str = "api"
    context: Dict[str, Any] = {}

    @field_validator("text")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("command text must not be empty")
        # Enforced here, at the HTTP boundary, not just on CommandRequest (which
        # the route builds *after* FastAPI validates the body). Without this, an
        # oversized paste sails through the request model and raises inside the
        # route -> HTTP 500. Rejecting it here is a clean 422 with the same
        # message, so the client learns the limit instead of seeing a server error.
        if len(value) > MAX_COMMAND_CHARS:
            raise ValueError(
                f"command text is too long ({len(value)} chars; the limit is "
                f"{MAX_COMMAND_CHARS}). Break it into shorter requests."
            )
        return value.strip()


class MemoryBody(BaseModel):
    """A memory the user is asking Vantage to keep.

    The content is validated here rather than in the route: an empty memory is not a
    memory, and storing one silently answered "saved" for nothing the user said.
    """

    content: str
    scope: str = "long_term"
    project: Optional[str] = None

    @field_validator("content")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("memory content must not be empty")
        return value.strip()


class BootstrapBody(BaseModel):
    """The single-use code the dashboard was served with on page load."""

    bootstrap: str = ""


class VoiceTextBody(BaseModel):
    """A spoken command, or a request for Vantage to speak."""

    text: str
    speak: bool = False
    require_wake: Optional[bool] = None

    @field_validator("text")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("text must not be empty")
        return value.strip()


__all__ = ["CommandBody", "MemoryBody", "BootstrapBody", "VoiceTextBody"]
