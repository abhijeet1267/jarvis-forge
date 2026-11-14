"""Identity for one running backend process.

Vantage had no way to answer "is the server I am talking to the code I just
changed?" A stale process on ``:8765`` served old routing for a day while the
app and the tests both looked fine, and the client accepted *any* HTTP response
on that port as "the backend is running". This module makes the process say who
it is, so a stale instance is a stated fact rather than a suspicion.

What it reports, and the rule behind each field:

* ``instance_id`` -- random per process. Two requests to the same id came from
  the same process; a changed id means the server restarted.
* ``started_at`` -- when this process imported its modules. A source file newer
  than this is *not* running here (see ``/health/code``).
* ``app_version`` -- the package version.
* ``revision`` -- the git revision captured **once at import**, never per
  request (a subprocess per health poll would be a tax on the UI). ``None`` when
  there is no repository or the command fails: an honest unknown, never an
  invented hash.
* ``source_root`` -- the repository the process is running from, so a second
  checkout is visible instead of guessed at.
* ``pid`` -- for an operator who needs to stop *this* process, not a name match.

Nothing here reads settings, environment values or credentials.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

#: One identity per process, fixed at import. Everything downstream reads this,
#: so every route reports the same instance rather than recomputing.
_INSTANCE_ID = f"BKT-{secrets.token_hex(8)}"
_STARTED_AT = time.time()
_PID = os.getpid()

#: The project root (the directory containing the `jarvis` package), not
#: the package itself: a client comparing checkouts wants the repository.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str, timeout: float = 2.0) -> Optional[str]:
    """Run one git command, or ``None`` if it cannot answer.

    Bounded: a repository on a slow or network filesystem must not hang an
    import. A failure is reported as unknown rather than as a plausible value.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def _capture_revision() -> Optional[str]:
    rev = _git("rev-parse", "--short=12", "HEAD")
    if rev is None:
        return None
    # A dirty tree means the running code is not exactly this commit. Say so:
    # a bare hash would otherwise imply the running build matches it.
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    if dirty is None:
        return rev
    return f"{rev}+dirty" if dirty else rev


def _capture_branch() -> Optional[str]:
    return _git("rev-parse", "--abbrev-ref", "HEAD")


_REVISION = _capture_revision()
_BRANCH = _capture_branch()

try:
    from .. import __version__ as APP_VERSION
except Exception:  # pragma: no cover - a broken package must still report
    APP_VERSION = "unknown"


def identity() -> Dict[str, Any]:
    """The safe, stable description of this process. No secrets, no settings."""
    return {
        "service": "vantage-backend",
        "instance_id": _INSTANCE_ID,
        "pid": _PID,
        "started_at": _STARTED_AT,
        "app_version": APP_VERSION,
        "revision": _REVISION,
        "branch": _BRANCH,
        "source_root": str(_REPO_ROOT),
        "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}."
                  f"{os.sys.version_info.micro}",
    }


def fingerprint() -> str:
    """A short value the client can compare between polls.

    Deliberately excludes ``started_at``/``pid``: the point is to notice that
    the *code* changed, not that the process did (a restart is reported by
    ``instance_id`` changing).
    """
    return f"{APP_VERSION}@{_REVISION or 'unknown'}"


def newest_source_mtime() -> Optional[float]:
    """The newest ``.py`` mtime the running backend actually loads.

    Scans the ``jarvis`` package and ``run.py`` -- the code this process imports.
    It deliberately does NOT scan the whole checkout: measured 2026-10-28,
    editing ``tests/test_backend_lifecycle.py`` made the live backend report
    ``stale: true`` and tell the user to restart it, because the test tree is
    under the repo and the old scan was `_REPO_ROOT.rglob("*.py")`. That is a
    timer restarting a healthy server for a change it will never load.

    Tests and the macOS Swift sources are excluded for the same reason: neither
    is imported by this process.
    """
    roots = [_REPO_ROOT / "jarvis", _REPO_ROOT / "run.py"]
    newest: Optional[float] = None
    for root in roots:
        if not root.exists():
            continue
        candidates = root.rglob("*.py") if root.is_dir() else [root]
        for source in candidates:
            if "__pycache__" in source.parts:
                continue
            try:
                when = source.stat().st_mtime
            except OSError:
                continue
            if newest is None or when > newest:
                newest = when
    return newest


def staleness() -> Dict[str, Any]:
    """Whether the code on disk is newer than the process serving it.

    This is the check that turns "my fix did not work" into "my fix is not
    loaded". It reads file mtimes, not git, so it works in a copy without a
    repository.
    """
    newest = newest_source_mtime()
    if newest is None:
        return {"stale": False, "checked": 0, "note": "No source files could be read."}
    stale = newest > _STARTED_AT
    return {
        "stale": stale,
        "started_at": _STARTED_AT,
        "newest_mtime": newest,
        "note": ("Source files changed after this process started, so what is "
                 "running is older than the code on disk. Restart the backend."
                 if stale else
                 "The running process is newer than every source file."),
    }