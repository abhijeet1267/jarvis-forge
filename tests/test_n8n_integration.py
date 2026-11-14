"""n8n integration: config, validation, execution, security, degradation.

The rules under test are the ones that lie when they are wrong:

* a workflow the model can name is not a workflow n8n will run;
* a failed execution is never reported as success;
* an unreachable n8n is distinguishable from a rejected credential, a bad
  workflow and a timeout;
* the API key never appears in a payload, a log line, or an error;
* a consequential workflow does not run without an explicit confirmation;
* a retry after a timeout does not repeat a non-idempotent action;
* Vantage starts and works with n8n off or absent.

These are unit tests with a substituted transport. The live path against a real
n8n container is exercised separately and reported separately.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from jarvis.integrations import n8n as mod
from jarvis.integrations import n8n_audit
from jarvis.integrations.n8n_client import (
    ExecutionStatus,
    N8NClient,
    _in_flight,
)

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def config(**overrides) -> mod.N8NConfig:
    base = dict(enabled=True, base_url="http://127.0.0.1:5678",
                api_key="secret-key-DO-NOT-LEAK", timeout=5.0)
    base.update(overrides)
    return mod.N8NConfig(**base)


class FakeHTTP:
    """Stands in for ``http_read``. Records the request it was given."""

    def __init__(self, *, body: bytes = b'{"ok": true, "summary": "done"}',
                 status: int = 200, raise_exc: Exception | None = None):
        self.body = body
        self.status = status
        self.raise_exc = raise_exc
        self.requests: list = []

    def __call__(self, request, *, total, what):
        self.requests.append((request, total, what))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.status >= 400:
            import io

            raise urllib.error.HTTPError(
                request.full_url, self.status, "error", {},
                io.BytesIO(self.body))
        return self.body


def client(monkeypatch, fake: FakeHTTP, cfg: mod.N8NConfig | None = None) -> N8NClient:
    monkeypatch.setattr("jarvis.integrations.n8n_client.http_read", fake)
    return N8NClient(cfg or config())


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "http://127.0.0.1:5678/",
    "http://localhost:5678",
    "https://localhost:8443/n8n",
    "http://[::1]:5678",
])
def test_loopback_urls_normalise(raw):
    out = mod.normalize_base_url(raw)
    assert out.startswith(("http://", "https://"))
    assert not out.endswith("/")


@pytest.mark.parametrize("raw", [
    "ftp://127.0.0.1:5678",
    "file:///etc/passwd",
    "not a url",
    "",
    "http://",
    "http://127.0.0.1:5678/?x=1",
    "http://127.0.0.1:5678/#frag",
])
def test_bad_urls_are_refused(raw):
    with pytest.raises(mod.ConfigError):
        mod.normalize_base_url(raw)


def test_remote_host_is_refused_unless_deliberate():
    with pytest.raises(mod.ConfigError, match="not this machine"):
        mod.normalize_base_url("http://n8n.example.com:5678")
    # With the explicit opt-in it is accepted.
    assert mod.normalize_base_url("https://n8n.example.com",
                                  allow_remote=True) == "https://n8n.example.com"


def test_a_name_that_merely_ends_in_localhost_is_not_loopback():
    # DNS-rebinding shape: `evil-localhost` must not pass as localhost.
    with pytest.raises(mod.ConfigError):
        mod.normalize_base_url("http://evil-localhost:5678")


def test_validate_config_reports_each_state():
    disabled = mod.validate_config(config(enabled=False))
    assert disabled["state"] == "disabled"

    unconfigured = mod.validate_config(config(base_url=""))
    assert unconfigured["state"] == "not_configured"

    bad = mod.validate_config(config(base_url="http://remote.example.com"))
    assert bad["state"] == "misconfigured"
    assert bad["problems"]

    ok = mod.validate_config(config())
    assert ok["state"] == "ready"


def test_public_config_never_carries_the_key():
    public = config(api_key="super-secret").public()
    assert "super-secret" not in json.dumps(public)
    assert public["has_api_key"] is True


def test_allowlist_narrows_and_typos_are_reported():
    cfg = config(allowed_keys=["echo", "typo"])
    assert cfg.permits("echo")
    assert not cfg.permits("daily_agenda")
    report = mod.validate_config(cfg)
    assert any("typo" in p for p in report["problems"])


def test_empty_allowlist_means_every_registered_workflow():
    cfg = config(allowed_keys=[])
    assert cfg.permits("echo") and cfg.permits("daily_agenda") and cfg.permits("notify")
    assert not cfg.permits("something-not-registered")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_required_field_is_enforced():
    with pytest.raises(mod.InputRejected, match="required"):
        mod.validate_inputs(mod.workflow("echo"), {})


def test_unknown_field_is_refused():
    with pytest.raises(mod.InputRejected, match="does not take"):
        mod.validate_inputs(mod.workflow("echo"), {"message": "hi", "cmd": "rm -rf /"})


def test_choice_field_is_constrained():
    spec = mod.workflow("notify")
    with pytest.raises(mod.InputRejected, match="one of"):
        mod.validate_inputs(spec, {"message": "x", "level": "urgent-ish"})
    assert mod.validate_inputs(spec, {"message": "x", "level": "warning"})["level"] == "warning"


def test_integer_bounds_are_enforced():
    spec = mod.workflow("daily_agenda")
    with pytest.raises(mod.InputRejected, match="at most"):
        mod.validate_inputs(spec, {"days": 99})
    with pytest.raises(mod.InputRejected, match="whole number"):
        mod.validate_inputs(spec, {"days": "soon"})
    assert mod.validate_inputs(spec, {"days": "3"})["days"] == 3


def test_overlong_text_is_refused():
    with pytest.raises(mod.InputRejected, match="maximum"):
        mod.validate_inputs(mod.workflow("echo"), {"message": "x" * 5000})


def test_oversized_payload_is_refused():
    spec = mod.workflow("echo")
    with pytest.raises(mod.InputRejected):
        # Two fields under their individual limits but together over the body cap.
        mod.validate_inputs(spec, {"message": "x" * 1900, "tag": "y" * 1900})


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def test_unknown_workflow_is_refused_without_a_request(monkeypatch):
    fake = FakeHTTP()
    result = client(monkeypatch, fake).execute("made-up", {})
    assert result.status is ExecutionStatus.REFUSED
    assert fake.requests == [], "a refused workflow must not reach the network"
    assert "echo" in result.message      # names what is available


def test_successful_execution_reports_the_workflow_output(monkeypatch):
    fake = FakeHTTP(body=b'{"ok": true, "summary": "echoed it"}')
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.ok is True
    assert result.message == "echoed it"
    assert result.output["summary"] == "echoed it"
    assert result.correlation_id.startswith("v8n-")


def test_the_correlation_id_is_generated_and_sent(monkeypatch):
    fake = FakeHTTP()
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    request, _, _ = fake.requests[0]
    sent = json.loads(request.data)
    assert sent["_vantage"]["correlation_id"] == result.correlation_id
    assert request.full_url == "http://127.0.0.1:5678/webhook/vantage/echo"


def test_the_api_key_is_sent_but_never_echoed(monkeypatch):
    fake = FakeHTTP()
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    request, _, _ = fake.requests[0]
    assert request.get_header("Authorization") == "Bearer secret-key-DO-NOT-LEAK"
    assert "secret-key-DO-NOT-LEAK" not in json.dumps(result.as_dict())


def test_workflow_reported_failure_is_a_failure_even_with_http_200(monkeypatch):
    fake = FakeHTTP(body=b'{"ok": false, "error": "calendar not configured"}')
    result = client(monkeypatch, fake).execute("daily_agenda", {})
    assert result.status is ExecutionStatus.FAILED
    assert "calendar not configured" in result.message


def test_timeout_is_distinct_and_retry_is_safe_only_for_idempotent(monkeypatch):
    from jarvis.net import DeadlineExceeded

    fake = FakeHTTP(raise_exc=DeadlineExceeded(5.0, "the workflow"))
    read = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    assert read.status is ExecutionStatus.TIMEOUT
    assert read.retry_safe is True

    send = client(monkeypatch, fake).execute("notify", {"message": "hi"}, confirmed=True)
    assert send.status is ExecutionStatus.TIMEOUT
    assert send.retry_safe is False, "a send may have happened; a retry could repeat it"


def test_connection_refused_is_unreachable_and_says_so(monkeypatch):
    fake = FakeHTTP(raise_exc=urllib.error.URLError("Connection refused"))
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    assert result.status is ExecutionStatus.UNREACHABLE
    assert "not run" in result.message


def test_401_is_reported_as_a_credential_problem(monkeypatch):
    fake = FakeHTTP(status=401, body=b'{"message": "unauthorized"}')
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    assert result.status is ExecutionStatus.FAILED
    assert result.http_status == 401
    assert "credential" in result.message.lower()


def test_404_is_reported_as_an_inactive_webhook_not_a_missing_workflow(monkeypatch):
    fake = FakeHTTP(status=404, body=b'{"message": "not registered"}')
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    assert result.status is ExecutionStatus.FAILED
    assert "activate" in result.message.lower() or "webhook" in result.message.lower()


def test_500_is_a_workflow_failure(monkeypatch):
    fake = FakeHTTP(status=500, body=b'{"message": "boom in node 3"}')
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    assert result.status is ExecutionStatus.FAILED
    assert result.http_status == 500


def test_malformed_json_is_not_treated_as_a_result(monkeypatch):
    fake = FakeHTTP(body=b"<html>hello</html>")
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    assert result.status is ExecutionStatus.FAILED
    assert "not with JSON" in result.message or "no result" in result.message


def test_oversized_response_is_refused(monkeypatch):
    fake = FakeHTTP(body=b'{"blob": "' + b"x" * (mod.MAX_OUTPUT_BYTES + 10) + b'"}')
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    assert result.status is ExecutionStatus.FAILED
    assert "byte limit" in result.message


def test_the_key_is_scrubbed_from_an_error_body(monkeypatch):
    fake = FakeHTTP(status=500,
                    body=b'{"message": "auth failed for secret-key-DO-NOT-LEAK"}')
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    assert "secret-key-DO-NOT-LEAK" not in result.message
    assert "[redacted]" in result.message


# ---------------------------------------------------------------------------
# Confirmation and duplicates
# ---------------------------------------------------------------------------

def test_a_consequential_workflow_needs_confirmation(monkeypatch):
    fake = FakeHTTP()
    result = client(monkeypatch, fake).execute("notify", {"message": "hi"})
    assert result.status is ExecutionStatus.REFUSED
    assert "confirmation" in result.message.lower()
    assert fake.requests == [], "an unconfirmed send must not reach n8n"

    confirmed = client(monkeypatch, fake).execute("notify", {"message": "hi"},
                                                 confirmed=True)
    assert confirmed.status is ExecutionStatus.SUCCEEDED


def test_a_read_workflow_needs_no_confirmation(monkeypatch):
    fake = FakeHTTP()
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    assert result.status is ExecutionStatus.SUCCEEDED


def test_the_same_correlation_id_cannot_run_twice_at_once():
    assert _in_flight.acquire("echo", "v8n-same")
    assert not _in_flight.acquire("echo", "v8n-same"), "a duplicate must be refused"
    _in_flight.release("echo", "v8n-same")
    assert _in_flight.acquire("echo", "v8n-same")


def test_a_duplicate_submission_is_refused_before_the_network(monkeypatch):
    fake = FakeHTTP()
    _in_flight.acquire("echo", "v8n-dupe")
    try:
        result = client(monkeypatch, fake).execute(
            "echo", {"message": "hi"}, correlation_id="v8n-dupe")
    finally:
        _in_flight.release("echo", "v8n-dupe")
    assert result.status is ExecutionStatus.REFUSED
    assert "already in progress" in result.message
    assert fake.requests == []


# ---------------------------------------------------------------------------
# Disabled / offline degradation
# ---------------------------------------------------------------------------

def test_disabled_integration_refuses_without_a_request(monkeypatch):
    fake = FakeHTTP()
    result = client(monkeypatch, fake, config(enabled=False)).execute("echo", {"message": "x"})
    assert result.status is ExecutionStatus.REFUSED
    assert "turned off" in result.message
    assert fake.requests == []


def test_the_app_builds_with_n8n_offline(monkeypatch):
    """Constructing the client and reading its state must never touch the network."""
    fake = FakeHTTP(raise_exc=AssertionError("no network expected"))
    c = client(monkeypatch, fake)
    assert c.config.enabled is True
    assert mod.validate_config(c.config)["state"] == "ready"


def test_readiness_distinguishes_unreachable_from_misconfigured(monkeypatch):
    fake = FakeHTTP(raise_exc=urllib.error.URLError("Connection refused"))
    down = client(monkeypatch, fake).readiness()
    assert down["state"] == "unreachable"

    bad = client(monkeypatch, fake, config(base_url="http://example.com")).readiness()
    assert bad["state"] == "misconfigured"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_audit_records_the_required_fields_and_nothing_secret(monkeypatch):
    n8n_audit.clear()
    fake = FakeHTTP(body=b'{"ok": true, "summary": "done"}')
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    row = n8n_audit.record(result, session="SESS-abc")
    assert row["workflow"] == "echo"
    assert row["correlation_id"] == result.correlation_id
    assert row["status"] == "succeeded"
    assert row["session"] == "SESS-abc"
    blob = json.dumps(row)
    assert "secret-key-DO-NOT-LEAK" not in blob
    # The inputs are not part of the trail.
    assert "hi" not in blob


def test_audit_is_bounded():
    n8n_audit.clear()
    for i in range(n8n_audit.MAX_ENTRIES + 25):
        n8n_audit.record(type("R", (), {
            "status": type("S", (), {"value": "succeeded"})(),
            "workflow_key": f"w{i}", "correlation_id": f"c{i}",
            "message": "", "http_status": 200, "duration_ms": 1})())
    rows = n8n_audit.recent(500)
    assert len(rows) == n8n_audit.MAX_ENTRIES
    assert rows[0]["workflow"] == f"w{n8n_audit.MAX_ENTRIES + 24}"   # newest first
    n8n_audit.clear()

# ---------------------------------------------------------------------------
# Chat routing
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def planner():
    from jarvis.core.planner import RulePlanner
    from jarvis.tools import build_default_registry

    return RulePlanner(build_default_registry())


@pytest.mark.parametrize("sentence,intent,expect_actions", [
    ("list my n8n workflows", "list_n8n_workflows", [("list_n8n_workflows", {})]),
    ("run my echo workflow", "echo", [("n8n__echo", {})]),
    ("run the daily agenda workflow", "daily_agenda", [("n8n__daily_agenda", {})]),
])
def test_automation_sentences_route_to_the_registry(planner, sentence, intent,
                                                    expect_actions):
    plan = planner.parse(sentence)
    assert plan.intent == intent
    assert [(a.tool, a.params) for a in plan.actions] == expect_actions


def test_a_message_in_the_sentence_reaches_the_echo_workflow(planner):
    plan = planner.parse("run my echo workflow saying hello there")
    assert plan.intent == "echo"
    assert plan.actions[0].params.get("message") == "hello there"


def test_an_unregistered_workflow_is_answered_with_the_list(planner):
    plan = planner.parse("run my approved repository maintenance workflow")
    assert plan.intent == "unknown_workflow"
    assert plan.actions == [], "nothing may be sent for a workflow Vantage does not have"
    assert "Diagnostic echo" in (plan.direct_reply or ""), \
        "the answer names what *is* available"


@pytest.mark.parametrize("sentence,intent", [
    ("open Safari", "open_app"),
    ("what is my battery", "laptop_battery"),
    ("what is on my calendar today?", "list_events"),
])
def test_existing_routing_is_untouched(planner, sentence, intent):
    assert planner.parse(sentence).intent == intent


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

def test_tools_expose_one_per_workflow_plus_the_list():
    from jarvis.tools import build_default_registry

    names = set(build_default_registry().names())
    assert {"list_n8n_workflows", "n8n__echo", "n8n__daily_agenda",
            "n8n__notify"} <= names


def test_consequential_workflows_require_confirmation_as_tools():
    from jarvis.tools.n8n_tools import specs_for

    specs = {spec.name: spec for spec, _ in specs_for()}
    assert specs["n8n__notify"].requires_confirmation is True
    assert specs["n8n__echo"].requires_confirmation is False
    assert specs["n8n__daily_agenda"].requires_confirmation is False


def test_the_tool_result_never_claims_a_write_was_observed(monkeypatch):
    from jarvis.integrations.n8n_client import ExecutionResult
    from jarvis.tools.n8n_tools import emit_execution

    read = ExecutionResult(ExecutionStatus.SUCCEEDED, "v8n-a", "echo", "done",
                           read_only=True)
    assert emit_execution(read, device="laptop_main").verified is True

    write = ExecutionResult(ExecutionStatus.SUCCEEDED, "v8n-b", "notify", "sent",
                            read_only=False)
    assert emit_execution(write, device="laptop_main").verified is False, \
        "a workflow's claim about a side effect is not an observation"

    failed = ExecutionResult(ExecutionStatus.FAILED, "v8n-c", "echo", "boom")
    assert emit_execution(failed, device="laptop_main").ok is False


# ---------------------------------------------------------------------------
# Calendar context
# ---------------------------------------------------------------------------

def test_agenda_receives_events_from_vantage_not_a_credential(monkeypatch):
    fake = FakeHTTP(body=b'{"ok": true, "summary": "2 events"}')
    c = client(monkeypatch, fake)
    monkeypatch.setattr(c, "_calendar_events",
                        lambda: ([{"title": "Standup", "start": "2026-10-30T09:30:00+05:30"}], ""))
    result = c.execute("daily_agenda", {"days": 2})
    assert result.status is ExecutionStatus.SUCCEEDED
    sent = json.loads(fake.requests[0][0].data)
    assert sent["events"][0]["title"] == "Standup"
    assert sent["_events_available"] is True
    assert "secret-key-DO-NOT-LEAK" not in json.dumps(sent["events"])


def test_a_failed_calendar_read_is_passed_as_a_failure(monkeypatch):
    fake = FakeHTTP(body=b'{"ok": true, "summary": "ok"}')
    c = client(monkeypatch, fake)
    monkeypatch.setattr(c, "_calendar_events",
                        lambda: ([], "Google Calendar could not be read."))
    c.execute("daily_agenda", {})
    sent = json.loads(fake.requests[0][0].data)
    assert sent["events"] == []
    assert sent["_events_available"] is False
    assert "could not be read" in sent["events_error"], \
        "the workflow must be able to say the read failed, not claim a free day"


# ---------------------------------------------------------------------------
# Per-workflow enable/disable (Vantage-side allowlist)
# ---------------------------------------------------------------------------

def test_disabling_a_workflow_narrows_the_allowlist_without_touching_n8n(tmp_path):
    from jarvis.gateway.selection import save_n8n_config

    save_n8n_config(home=tmp_path, enabled=True, base_url="http://127.0.0.1:5678",
                    allowed_keys="echo,daily_agenda,notify")
    cfg = mod.N8NConfig(enabled=True, base_url="http://127.0.0.1:5678",
                        allowed_keys=["echo", "daily_agenda"])
    assert cfg.permits("echo") and not cfg.permits("notify")


def test_a_disabled_workflow_is_refused_before_the_network(monkeypatch):
    fake = FakeHTTP()
    c = client(monkeypatch, fake,
               config(allowed_keys=["echo", "daily_agenda"]))
    result = c.execute("notify", {"message": "hi"}, confirmed=True)
    assert result.status is ExecutionStatus.REFUSED
    assert "allowlist" in result.message
    assert fake.requests == [], "a disabled workflow must not reach n8n"


def test_an_empty_allowlist_still_means_every_registered_workflow():
    cfg = config(allowed_keys=[])
    assert cfg.permits("echo") and cfg.permits("notify")


# ---------------------------------------------------------------------------
# Execution status is Vantage's own record
# ---------------------------------------------------------------------------

def test_the_audit_row_answers_a_status_question(monkeypatch):
    from jarvis.integrations import n8n_audit

    n8n_audit.clear()
    fake = FakeHTTP(body=b'{"ok": true, "summary": "done"}')
    result = client(monkeypatch, fake).execute("echo", {"message": "hi"})
    n8n_audit.record(result)
    rows = [r for r in n8n_audit.recent(50) if r["workflow"] == "echo"]
    assert rows and rows[0]["correlation_id"] == result.correlation_id
    assert rows[0]["status"] == "succeeded"
    n8n_audit.clear()
