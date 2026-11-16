"""Real desktop control: the native executor and its bridge.

The package holds one thing the capability needs and the rest of the tree does
not: a handle on a compiled helper macOS can authorize to post input events.
See :mod:`jarvis.desktop.executor` for why that cannot be a Python script.
"""

from . import executor

__all__ = ["executor"]
