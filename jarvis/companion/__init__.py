"""Companion gateway — TLS-secured bridge for Android pairing."""

from .gateway import build_companion_app
from .cert import ensure, fingerprint, generate

__all__ = ["build_companion_app", "ensure", "fingerprint", "generate"]