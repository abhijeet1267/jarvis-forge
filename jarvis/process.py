"""Watching the process that started us.

`app/launcher.py` spawns the server and owns the window. When the window goes away on a
path Python does not control -- Tk's `exit`, a crash, a SIGKILL, a logout -- the child is
reparented to launchd and keeps listening on whatever address it was told to bind. On the
delivered app that was the wifi address, so a closed application left an open port.

Nothing can be *sent* to a process that is already gone, so this is the other half of the
guarantee: the server notices, and stops itself.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

#: Environment variable the panel sets to its own pid before spawning the server.
GUARDIAN_ENV = "JARVIS_GUARDIAN_PID"


def guardian_gone(guardian_pid: int) -> bool:
    """True when ``guardian_pid`` is no longer the process that started this one.

    Reparenting is checked first because it is the observed shape: the panel dies, launchd
    adopts the server, and the pid may by then belong to somebody else entirely.
    """
    if guardian_pid <= 0:
        return False
    if os.getppid() != guardian_pid:
        return True
    try:
        os.kill(guardian_pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False            # alive, just not ours to signal
    return False


def start_guardian_from_env(on_exit: Callable[[], None],
                            interval: float = 2.0) -> Optional[threading.Thread]:
    """Arm the watchdog if the panel asked for one. None when there is nothing to watch.

    A daemon thread, so it can never hold the process open by itself; `on_exit` is called
    once, from that thread, which is why the caller's own exit has to be immediate rather
    than graceful.
    """
    raw = os.environ.get(GUARDIAN_ENV, "").strip()
    if not raw.isdigit():
        return None
    guardian = int(raw)

    def watch() -> None:
        while True:
            if guardian_gone(guardian):
                on_exit()
                return
            time.sleep(interval)

    thread = threading.Thread(target=watch, name="guardian", daemon=True)
    thread.start()
    return thread
