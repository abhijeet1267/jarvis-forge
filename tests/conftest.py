"""Shared pytest fixtures.

Each test session runs against an isolated ``JARVIS_HOME`` so the developer's
real memory database and audit log are never touched.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# Must happen before any ``jarvis`` import so settings pick up the temp home.
_TMP_HOME = tempfile.mkdtemp(prefix="jarvis-test-")
os.environ["JARVIS_HOME"] = _TMP_HOME
os.environ["JARVIS_LOG_LEVEL"] = "WARNING"
os.environ.setdefault("JARVIS_TRASH_ON_DELETE", "true")
# A fixed token keeps API tests deterministic; auth itself is tested explicitly.
TEST_TOKEN = "test-token-do-not-use-in-production"
os.environ["JARVIS_API_TOKEN"] = TEST_TOKEN

import pytest  # noqa: E402

from jarvis.core.models import CommandRequest  # noqa: E402
from jarvis.core.orchestrator import JarvisCore  # noqa: E402

AUTH_HEADERS = {"X-JARVIS-Token": TEST_TOKEN}


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Give each test a fresh rate-limit budget.

    The limiter is a process-wide singleton, so without this a long test module
    would start returning 429 and mask the behaviour under test.
    """
    from jarvis.security.auth import get_rate_limiter

    get_rate_limiter().reset()
    yield
    get_rate_limiter().reset()


@pytest.fixture(scope="session")
def core() -> JarvisCore:
    """A single core instance shared across the session."""
    return JarvisCore()


@pytest.fixture()
def tmp_area():
    """A scratch directory for file-operation tests.

    Created under /tmp rather than pytest's tmp_path, because the managed
    runtime sandbox may refuse to create pytest's own temp root.
    """
    area = Path(tempfile.mkdtemp(prefix="jarvis-area-", dir="/tmp"))
    try:
        yield area
    finally:
        shutil.rmtree(area, ignore_errors=True)


@pytest.fixture()
def make_request():
    """Build a CommandRequest with sensible defaults."""

    def _make(text: str, **kwargs) -> CommandRequest:
        return CommandRequest(text=text, source="test", **kwargs)

    return _make
