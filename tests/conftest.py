"""Shared pytest fixtures.

Each test session runs against an isolated ``JARVIS_HOME`` so the developer's
real memory database and audit log are never touched.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Must happen before any ``jarvis`` import so settings pick up the temp home.
#
# This file is executed twice in a normal pytest run: pytest loads it as the
# top-level ``conftest`` module, and test modules that do ``from tests.conftest
# import ...`` import it again as ``tests.conftest``. A bare ``mkdtemp()`` here
# would therefore create two different temp homes, and the second import would
# overwrite ``JARVIS_HOME`` after ``get_settings()`` had already frozen the first
# into its lru_cache -- leaving ``settings.home`` and the env var pointing at
# different directories. Pinning the value on ``sys`` makes both executions agree.
_TMP_HOME = getattr(sys, "_vantage_test_home", None)
if _TMP_HOME is None:
    _TMP_HOME = tempfile.mkdtemp(prefix="jarvis-test-")
    sys._vantage_test_home = _TMP_HOME
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

# The credential tests store and retrieve through the real KeychainStorage + keyring
# code path. To keep them deterministic -- and, more importantly, to keep them from
# writing into (or clobbering) the developer's real macOS keychain, the keyring backend
# is swapped for an in-memory one for the whole test session. This exercises the actual
# store/retrieve/delete logic and its error handling (including delete-of-absent raising
# PasswordDeleteError) without any OS keychain access. If keyring is not installed,
# KeychainStorage's no-op fallback is used and the credential tests skip, as before.
try:
    import keyring  # noqa: E402
    import keyring.backend  # noqa: E402
    import keyring.errors  # noqa: E402

    class _MemoryKeyring(keyring.backend.KeyringBackend):
        priority = 999  # higher than any real backend, so it wins the selection

        def __init__(self) -> None:
            self._secrets: dict = {}

        def get_password(self, service, user, password=None):
            return self._secrets.get((service, user))

        def set_password(self, service, user, password):
            self._secrets[(service, user)] = password

        def delete_password(self, service, user, password=None):
            if (service, user) not in self._secrets:
                raise keyring.errors.PasswordDeleteError(f"{service}/{user}")
            del self._secrets[(service, user)]

    # One process-wide instance. pytest loads this file as top-level ``conftest`` while
    # tests also import it as ``tests.conftest``, so the module body can run twice; a
    # plain module global would then give two _MemoryKeyring objects, and the fixture
    # would clear one while the app wrote to the other (a real, reproducible leak).
    # Pinning the instance in sys.modules makes both copies converge on one backend.
    import sys as _sys  # noqa: E402

    _KEYRING_SINGLETON_ATTR = "_vantage_test_memory_keyring"
    _existing = getattr(_sys, _KEYRING_SINGLETON_ATTR, None)
    if _existing is None:
        _existing = _MemoryKeyring()
        setattr(_sys, _KEYRING_SINGLETON_ATTR, _existing)
    _MEM_KEYRING = _existing
    keyring.set_keyring(_MEM_KEYRING)
except ImportError:
    _MEM_KEYRING = None
    pass


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


@pytest.fixture(autouse=True)
def _fresh_keyring():
    """Give each test an empty in-memory keychain.

    Without this, a store in one test would leak into the next, so a test that
    asserts "unconfigured" after a delete could see a key an earlier test wrote.
    It also guarantees no test -- including one that forgets to clean up -- can
    leave anything in the real OS keychain (the memory backend is the active one).

    The clear targets the *active* backend (``keyring.get_keyring()``), not just the
    module-global reference: pytest can load this file twice (as ``conftest`` and as
    ``tests.conftest``), and clearing the active backend is what the app actually
    writes through, so the reset cannot miss.
    """
    def _clear():
        if _MEM_KEYRING is not None:
            _MEM_KEYRING._secrets.clear()
        try:
            import keyring as _kr
            active = _kr.get_keyring()
            secrets = getattr(active, "_secrets", None)
            if isinstance(secrets, dict):
                secrets.clear()
        except Exception:
            pass

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _fresh_ai_selection():
    """Give each test a clean AI-provider selection.

    The selection file lives in the shared test ``JARVIS_HOME``, so a test that
    persists a provider choice (via ``/ai/selection``) would otherwise leak it into
    every later test -- the same class of cross-test pollution the keyring fixture
    prevents for credentials. Also resets the *singleton* fields that the selection
    endpoint mutates in place (``ai_provider``/``llm_model``), because clearing the file
    alone leaves the process-wide settings object still telling the next test it is
    ``gemini``.
    """
    from jarvis.gateway.selection import selection_path

    def _clear():
        path = selection_path()
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
        # Reset the cached singleton's mutable AI fields to their defaults, so the
        # overlay runs from a clean base in the next test.
        from pathlib import Path as _Path
        session_home = _Path(os.environ["JARVIS_HOME"])

        def _reset(singleton_get):
            try:
                settings = singleton_get()
            except Exception:
                return
            settings.ai_provider = "openai"
            settings.llm_model = "gpt-4o-mini"
            settings.llm_base_url = ""
            # llm_api_key is normally empty in tests; clear it too so an env-less
            # Settings reports Basic Mode unless a test stores a key on purpose.
            settings.llm_api_key = ""
            settings.home = session_home

        # Reset the AI fields in place on each live singleton. There can be two:
        # `jarvis.config.get_settings()` (reloaded in place by a test like
        # test_phase45, which mutates its own cached instance) and
        # `jarvis.api.app.get_settings` (bound at the app module's import, so it
        # keeps the instance it first cached). Provider routes read the *app*
        # singleton, so a `test_phase45` run that sets it to `gemini` would bleed
        # into every later provider test unless we reset that very instance here.
        #
        # Crucially this only mutates the instances each `get_settings` already
        # returns -- it never calls `cache_clear()` and never re-binds the app
        # module's name. Clearing the lru cache would rebuild a *new* Settings
        # object, orphaning the one the session-scoped `core` fixture captured
        # (`core.settings`) -- which is exactly how a calendar test that patches
        # `get_settings()` stopped seeing the patch (the handler reads
        # `core.settings`, a different object). Resetting in place keeps every
        # reference to the same live object, so isolation and identity both hold.
        try:
            from jarvis.config import get_settings as _cfg_get
            _reset(_cfg_get)
        except Exception:
            pass
        try:
            import jarvis.api.app as _api_mod
            _reset(_api_mod.get_settings)
        except Exception:
            pass
        # Reset the per-provider config block too (it lives in the same selection
        # file, which the unlink above already clears for the shared home; this is
        # a belt-and-suspenders guard for tests that pointed settings.home at a
        # scratch dir and left a provider block behind).
        try:
            from jarvis.gateway import selection as _sel
            for _pid in list(_sel.provider_config(session_home).keys()):
                _sel.save_provider_config(_pid, home=session_home, base_url=None, model=None,
                                          last_test_ok=None, last_test_error=None,
                                          last_test_at=None, last_test_model=None,
                                          last_test_base_url=None)
        except Exception:
            pass
        # Reset the shared embedding cache: `_SHARED_CACHE` in jarvis.memory.embeddings
        # is keyed by raw text, so one test embedding "the heater coil arrived" leaves a
        # cached vector that makes a later test's embed() skip its network read. The
        # phase60→phase34 order showed exactly this (an assertion that expected two
        # reads saw one). Clearing here restores per-test isolation.
        try:
            from jarvis.memory import embeddings as _emb
            _emb.reset_embedder()
            _emb._SHARED_CACHE.clear()
        except Exception:
            pass
        # Reset the route-health ledger so a test's chat/test observation never leaks
        # into a later provider-health assertion.
        try:
            from jarvis.gateway import route_health as _rh
            _rh.reset_route_health()
        except Exception:
            pass

    _clear()
    yield
    _clear()


@pytest.fixture(scope="session")
def core() -> JarvisCore:
    """A single core instance shared across the session."""
    return JarvisCore()


@pytest.fixture(scope="session")
def isolated_server(tmp_path_factory):
    """A real Vantage server on a scratch home, for the tests that drive a browser.

    The browser tests used to point at the developer's *live* backend on
    ``:8765``, running against the real ``~/.jarvis-forge`` data directory --
    so the approval test, which clicks **Approve** on a "schedule a meeting"
    card, was creating real calendar events in the user's data on every run.

    This starts a real uvicorn on a real port with a real empty data
    directory, so the page, the session bootstrap and the approval round
    trip are all exercised for real against a calendar that is thrown away
    with the test home. See :mod:`tests.isolated_server`.
    """
    from tests.isolated_server import start_server, stop_server

    home = tmp_path_factory.mktemp("vantage-browser-home")
    server = start_server(home)
    try:
        yield server
    finally:
        stop_server(server)


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


def flat_routes(app):
    """Every route object on the app, flattening included routers.

    FastAPI 0.162 (on this Starlette) no longer splices an included router's
    routes into ``app.routes``: it appends one ``_IncludedRouter`` container, so
    ``[r.path for r in app.routes]`` raises ``AttributeError`` as soon as the app
    includes any router at all. Tests that need paths call this instead of
    assuming a flat list.
    """
    out = []
    for route in getattr(app, "routes", []):
        children = getattr(route, "routes", None)
        if children is not None and not hasattr(route, "path"):
            out.extend(children)
        elif hasattr(route, "path"):
            out.append(route)
        elif children is not None:
            out.extend(children)
    return out
