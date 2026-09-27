"""Security and accuracy regression tests.

Every test here corresponds to a real weakness that was found and fixed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from jarvis.core.models import CommandRequest, PlannedAction, PolicyDecision
from jarvis.security.auth import RateLimiter, TokenStore, host_allowed
from jarvis.security.paths import is_protected, protection_reason
from jarvis.tools.base import ToolContext
from jarvis.tools.files import search_files
from jarvis.tools.system import run_allowlisted_command


# ---------------------------------------------------------------------------
# Paths: persistence and credential locations
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        # Shell startup files are arbitrary code execution at next login.
        "~/.zshrc",
        "~/.zprofile",
        "~/.bashrc",
        "~/.bash_profile",
        "~/.profile",
        # Launch agents/daemons give persistence without the user present.
        "~/Library/LaunchAgents/com.evil.plist",
        "~/Library/LaunchDaemons/com.evil.plist",
        # Credentials.
        "~/.netrc",
        "~/.git-credentials",
        "~/.docker/config.json",
        "~/.npmrc",
        "~/.config/gh/hosts.yml",
        # Already protected before this pass.
        "~/.ssh/id_rsa",
        "~/.aws/credentials",
    ],
)
def test_persistence_and_credential_paths_are_protected(path: str) -> None:
    assert is_protected(path), f"{path} must be protected"
    assert protection_reason(path)


@pytest.mark.parametrize("path", ["~/Documents/notes.txt", "~/Desktop/x.png", "/tmp/scratch"])
def test_normal_paths_still_allowed(path: str) -> None:
    assert not is_protected(path)


def test_writing_a_shell_rc_file_is_blocked_at_the_policy_layer(core) -> None:
    """A plan to modify ~/.zshrc must be refused, not merely confirmed."""
    action = PlannedAction(
        tool="copy_file",
        params={"source": "/tmp/payload.sh", "destination": str(Path.home() / ".zshrc")},
    )
    verdict = core.policy.evaluate(action)
    assert verdict.decision is PolicyDecision.BLOCK


# ---------------------------------------------------------------------------
# Command allowlist
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_interpreters_are_not_on_the_default_allowlist() -> None:
    """python3/node/npm/pip are code execution; they must be opt-in."""
    from jarvis.config import get_settings

    settings = get_settings()
    for dangerous in ("python3", "node", "npm", "pip", "sh", "bash", "osascript"):
        assert dangerous not in settings.allowed_commands, (
            f"{dangerous} on the allowlist makes it arbitrary code execution"
        )


@pytest.mark.asyncio
async def test_unknown_binary_is_refused() -> None:
    ctx = ToolContext()
    result = await run_allowlisted_command(ctx, command="python3", args="-c 'print(1)'")
    assert not result.ok
    assert "not permitted" in result.summary


@pytest.mark.asyncio
async def test_code_execution_flags_are_refused_even_when_opted_in(monkeypatch) -> None:
    """Opting a binary in must not silently permit `-c`/`-e`."""
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "powerful_commands", ["python3"])

    ctx = ToolContext(settings=settings)
    result = await run_allowlisted_command(ctx, command="python3", args="-c import os")
    assert not result.ok
    assert "arbitrary code" in result.summary


@pytest.mark.asyncio
async def test_shell_metacharacters_in_arguments_are_refused() -> None:
    ctx = ToolContext()
    for args in ("; rm -rf /", "&& whoami", "$(id)", "| tee /tmp/x", "`id`"):
        result = await run_allowlisted_command(ctx, command="ls", args=args)
        assert not result.ok, f"{args!r} should be refused"


@pytest.mark.asyncio
async def test_safe_command_still_runs() -> None:
    ctx = ToolContext()
    result = await run_allowlisted_command(ctx, command="echo", args="hello")
    assert result.ok


# ---------------------------------------------------------------------------
# Auth primitives
# ---------------------------------------------------------------------------
def test_token_store_generates_and_persists(tmp_area: Path) -> None:
    from jarvis.config import Settings

    settings = Settings(home=tmp_area, api_token="")
    store = TokenStore(settings)
    token = store.token()

    assert len(token) >= 32
    assert settings.token_path.exists()

    # Owner-only permissions -- the token is a credential.
    mode = settings.token_path.stat().st_mode & 0o777
    assert mode == 0o600, f"token file mode is {oct(mode)}, expected 0o600"


def test_token_roundtrips_from_disk(tmp_area: Path) -> None:
    from jarvis.config import Settings

    settings = Settings(home=tmp_area, api_token="")
    first = TokenStore(settings).token()
    second = TokenStore(settings).token()
    assert first == second


def test_token_verification_rejects_wrong_values(tmp_area: Path) -> None:
    from jarvis.config import Settings

    store = TokenStore(Settings(home=tmp_area, api_token=""))
    good = store.token()

    assert store.verify(good) is True
    assert store.verify("nope") is False
    assert store.verify("") is False
    assert store.verify(None) is False


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1:8765", True),
        ("localhost:8765", True),
        ("localhost", True),
        ("[::1]:8765", True),
        ("evil.example.com", False),
        ("evil.example.com:8765", False),
        ("", False),
        (None, False),
    ],
)
def test_host_header_validation(host, expected: bool) -> None:
    """Rejecting non-loopback Host headers defeats DNS rebinding."""
    assert host_allowed(host) is expected


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_rate_limiter_allows_burst_then_throttles() -> None:
    limiter = RateLimiter(per_minute=60, burst=5)

    for _ in range(5):
        allowed, _retry = limiter.check("client")
        assert allowed

    allowed, retry_after = limiter.check("client")
    assert not allowed
    assert retry_after > 0


def test_rate_limiter_is_per_client() -> None:
    limiter = RateLimiter(per_minute=60, burst=2)
    assert limiter.check("a")[0]
    assert limiter.check("a")[0]
    assert not limiter.check("a")[0]
    # A different client still has its own budget.
    assert limiter.check("b")[0]


def test_rate_limiter_refills_over_time() -> None:
    limiter = RateLimiter(per_minute=600, burst=1)
    assert limiter.check("c")[0]
    assert not limiter.check("c")[0]
    time.sleep(0.15)  # 600/min = 10/s, so 0.15s restores ~1.5 tokens
    assert limiter.check("c")[0]


def test_default_rate_limit_does_not_throttle_the_dashboard() -> None:
    """The dashboard polls ~150 req/min; the default must leave headroom."""
    from jarvis.config import get_settings

    settings = get_settings()
    assert settings.rate_limit_per_minute >= 300
    assert settings.rate_limit_burst >= 60


# ---------------------------------------------------------------------------
# Confirmation expiry
# ---------------------------------------------------------------------------
def test_confirmation_ids_are_high_entropy(core) -> None:
    """A confirmation ID authorises a real action, so it must not be guessable."""
    action = PlannedAction(tool="delete_file", params={"path": "/tmp/entropy-check.txt"})
    verdict = core.policy.evaluate(action)

    assert verdict.confirmation_id
    # 16 random bytes -> 32 hex characters.
    suffix = verdict.confirmation_id.split("-", 1)[1]
    assert len(suffix) >= 32


def test_expired_confirmation_is_not_honoured(core, monkeypatch) -> None:
    """An approval that arrives too late must not run the action."""
    action = PlannedAction(tool="delete_file", params={"path": "/tmp/expiry-check.txt"})
    verdict = core.policy.evaluate(action)
    confirmation_id = verdict.confirmation_id

    # Pretend the request was created long ago.
    core.policy.db.execute(
        "UPDATE confirmations SET created_at = ? WHERE confirmation_id = ?",
        ("2020-01-01T00:00:00+00:00", confirmation_id),
    )

    record = core.policy.get_confirmation(confirmation_id)
    assert core.policy.is_expired(record)

    core.policy.resolve(confirmation_id, approve=True)
    assert core.policy.get_confirmation(confirmation_id)["state"] == "expired"


def test_expire_stale_clears_pending_list(core) -> None:
    action = PlannedAction(tool="delete_file", params={"path": "/tmp/stale-check.txt"})
    confirmation_id = core.policy.evaluate(action).confirmation_id

    core.policy.db.execute(
        "UPDATE confirmations SET created_at = ? WHERE confirmation_id = ?",
        ("2020-01-01T00:00:00+00:00", confirmation_id),
    )

    assert core.policy.expire_stale() >= 1
    assert confirmation_id not in {c["confirmation_id"] for c in core.policy.pending()}


# ---------------------------------------------------------------------------
# Audit chain
# ---------------------------------------------------------------------------
def test_audit_chain_verifies(core) -> None:
    core.audit.record(intent="chain-test", tool="get_time", result="success")
    result = core.audit.verify_chain()
    assert result["ok"] is True
    assert result["checked"] > 0


@pytest.fixture()
def pristine_chain(core):
    """Restore `action_logs` byte-for-byte after a test has broken the chain.

    The two tests below forge records on purpose. `core` is session-scoped, so
    their rows used to stay behind, and the poisoning was not confined to test
    runs: on the user's real data directory a leftover row with created_at
    '2020-01-01' sorts to the head of the chain, its hash no longer matches its
    content, and `verify_chain()` then aborts at checked=0 -- permanently
    disabling integrity verification for every genuine record after it.
    """
    db = core.audit.db
    rows = db.query("SELECT rowid, * FROM action_logs ORDER BY rowid")
    cols = list(rows[0].keys()) if rows else []
    snapshot = [tuple(r[c] for c in cols) for r in rows]

    yield

    if cols:
        placeholders = ", ".join("?" * len(cols))
        statement = f"INSERT INTO action_logs ({', '.join(cols)}) VALUES ({placeholders})"
        db.execute("DELETE FROM action_logs")
        for row in snapshot:
            db.execute(statement, row)


def test_audit_chain_detects_tampering(core, pristine_chain) -> None:
    """Editing a past record must break verification."""
    core.audit.record(intent="tamper-target", tool="get_time", result="success")

    rows = core.audit.db.query(
        "SELECT log_id FROM action_logs WHERE intent = 'tamper-target' ORDER BY rowid DESC LIMIT 1"
    )
    assert rows
    core.audit.db.execute(
        "UPDATE action_logs SET result = 'forged' WHERE log_id = ?", (rows[0]["log_id"],)
    )

    assert core.audit.verify_chain()["ok"] is False


def test_tampering_does_not_persist_after_the_test(core, pristine_chain) -> None:
    """Runs after test_audit_chain_detects_tampering: its poison must be gone."""
    result = core.audit.verify_chain()
    assert result["ok"] is True, (
        f"a previous tamper test left the shared ledger unverifiable: {result}"
    )

    core.audit.record(intent="tamper-target", tool="get_time", result="success")
    core.audit.db.execute(
        "UPDATE action_logs SET result = 'forged' WHERE intent = 'tamper-target'"
    )
    assert core.audit.verify_chain()["ok"] is False


def test_unhashed_legacy_records_are_reported_not_silently_passed(
    core, pristine_chain
) -> None:
    """A record written before the chain existed must be flagged, not ignored."""
    core.audit.db.execute(
        "INSERT INTO action_logs (log_id, intent, tool, result, detail, created_at) "
        "VALUES ('LEGACY-TEST', 'legacy', 'get_time', 'success', '{}', '2020-01-01T00:00:00+00:00')"
    )
    result = core.audit.verify_chain()
    assert result["ok"] is False
    assert "no hash" in result["reason"]

    # Sealing makes the chain coherent again...
    assert core.audit.seal_chain() >= 1
    assert core.audit.verify_chain()["ok"] is True

    # ...and tampering after sealing is still detected.
    core.audit.db.execute(
        "UPDATE action_logs SET result = 'forged' WHERE log_id = 'LEGACY-TEST'"
    )
    assert core.audit.verify_chain()["ok"] is False


def test_seal_chain_is_idempotent(core) -> None:
    """Sealing an already-sealed chain must be a no-op."""
    core.audit.seal_chain()  # ensure everything is sealed
    assert core.audit.seal_chain() == 0, "a second seal should find nothing to do"


# ---------------------------------------------------------------------------
# Accuracy: search ordering
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_ranks_newest_first_when_asked(tmp_area: Path) -> None:
    """'find my latest report' must actually return the latest one."""
    old = tmp_area / "report_old.txt"
    new = tmp_area / "report_new.txt"
    old.write_text("old")
    new.write_text("new")

    # Backdate one file so the ordering is unambiguous.
    past = time.time() - 90 * 86400
    os.utime(old, (past, past))

    ctx = ToolContext()
    result = await search_files(ctx, query="report", directory=str(tmp_area), sort="recent")

    assert result.ok
    names = [r["name"] for r in result.output["results"]]
    assert names[0] == "report_new.txt", names


@pytest.mark.asyncio
async def test_search_relevance_prefers_exact_match(tmp_area: Path) -> None:
    (tmp_area / "report_final.txt").write_text("x")
    (tmp_area / "report_final_archive_backup.txt").write_text("x")

    ctx = ToolContext()
    result = await search_files(ctx, query="report_final", directory=str(tmp_area))

    names = [r["name"] for r in result.output["results"]]
    assert names[0] == "report_final.txt", names


@pytest.mark.asyncio
async def test_search_results_carry_human_readable_age(tmp_area: Path) -> None:
    (tmp_area / "aged.txt").write_text("x")
    ctx = ToolContext()
    result = await search_files(ctx, query="aged", directory=str(tmp_area))
    assert result.output["results"][0]["modified_human"] == "just now"


def test_planner_requests_recency_sort(core) -> None:
    plan = core.planner.parse("find my latest project report")
    assert plan.actions[0].params.get("sort") == "recent"


# ---------------------------------------------------------------------------
# Accuracy: memory recall
# ---------------------------------------------------------------------------
def test_memory_search_ignores_stopwords(core) -> None:
    """'what did I work on' should be judged on 'work', not on 'what'."""
    core.memory.remember("The chassis uses 3mm aluminium", scope=__import__(
        "jarvis.core.models", fromlist=["MemoryScope"]).MemoryScope.LONG_TERM)

    hits = core.memory.search("what did I work on")
    # Nothing about work exists, so stopwords alone must not manufacture a hit.
    assert not any("aluminium" in h.content for h in hits)


def test_memory_search_matches_stemmed_forms(core) -> None:
    from jarvis.core.models import MemoryScope

    core.memory.remember(
        "The AMR robot is running integration tests", scope=MemoryScope.LONG_TERM
    )
    hits = core.memory.search("run test")
    assert hits
    assert any("running" in h.content for h in hits)


def test_memory_search_prefers_distinctive_terms(core) -> None:
    from jarvis.core.models import MemoryScope

    core.memory.remember("The project uses a standard component", scope=MemoryScope.LONG_TERM)
    core.memory.remember(
        "The gearbox overheated during the endurance bench test", scope=MemoryScope.LONG_TERM
    )

    hits = core.memory.search("gearbox endurance")
    assert hits
    assert "gearbox" in hits[0].content
