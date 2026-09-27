"""Vision Agent support (v1.3).

The plan's rule for vision is explicit:

    SEE → UNDERSTAND → PROPOSE → PERMISSION → ACT

and *"Vision should NEVER automatically perform physical or computer actions
merely because it sees something."*

That is enforced structurally: **this package has no action path**. It measures
pixels, recognises text, detects change, and returns *proposals* as data. There
is no click, type, or execute function anywhere in it — so the agent cannot act
on what it sees even in principle.

What is real here: image measurement (Pillow), OCR (macOS Vision framework),
screen capture, and image diffing. What is not available without a configured
model: describing *content*. JARVIS says so rather than guessing.
"""

from .diff import compare
from .image import describe as describe_image
from .image import inspect
from .ocr import available as ocr_available
from .ocr import find_text, read_text
from .providers import active_provider, describe as describe_providers, providers
from .screen import capture, read_screen

__all__ = [
    "inspect",
    "describe_image",
    "read_text",
    "find_text",
    "ocr_available",
    "compare",
    "capture",
    "read_screen",
    "providers",
    "active_provider",
    "describe_providers",
]
