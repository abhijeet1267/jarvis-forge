"""Machine capabilities some tests legitimately depend on.

A GitHub macOS runner is a VM: it has no battery and, unless it is installed,
no ffmpeg. Those are facts about the machine, not about JARVIS -- and the product's
*correct* answer on such a host is "battery percentage unavailable" and "no capture
backend". Asserting a reading instead would be asserting the machine has hardware
it does not have.

Only genuinely hardware-bound assertions are gated here. The voice tests are not:
their premise is "a backend is installed and consent varies", and CI satisfies that
premise by installing ffmpeg rather than by skipping the check.
"""

from __future__ import annotations

import shutil
import subprocess


def _battery_present() -> bool:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "InternalBattery" in out


HAS_BATTERY = _battery_present()
HAS_FFMPEG = shutil.which("ffmpeg") is not None
