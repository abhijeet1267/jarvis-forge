"""The Text & Chat tab must be the thing that configures the chat path.

The defect this file exists to prevent: the screen describes itself as
"Command Center answers, conversation and reasoning", it saves a provider, a
model, a base URL and a key, and its Test button proves the provider answers --
while the chat path read none of it. Every sentence still fell through to the
rule planner, so the app looked configured and answered nothing.

These tests pin the three facts that make the tab real:

* a saved category reaches ``Settings`` (provider, model, base URL);
* the category's key reaches the adapter, and only for the provider it names;
* an unconfigured tab changes nothing, and its off switch only ever disables.
"""

from __future__ import annotations

import pytest

from jarvis.config import Settings
from jarvis.gateway import provider_categories as pc
from jarvis.gateway.selection import effective_base_url, effective_key, stored_key


@pytest.fixture()
def home(tmp_path):
    return tmp_path


@pytest.fixture()
def cat_key():
    """Store a key for a category the way the tab's save route does."""
    from jarvis.security.providers import KeychainStorage

    stored = []

    def _store(category: str, secret: str = "sk-category-test-key") -> str:
        KeychainStorage.store(pc.key_ref_for(category), secret)
        stored.append(category)
        return secret

    yield _store
    for category in stored:
        KeychainStorage.delete(pc.key_ref_for(category))


@pytest.fixture(autouse=True)
def _pin_environment(monkeypatch):
    """Clear the provider environment so the test decides, not the developer's shell.

    ``_overlay_text_category`` honours an exported ``JARVIS_LLM_MODEL`` and friends
    above the saved tab -- correct behaviour, but it means a test that inherited one
    would silently assert against the shell's value instead of the category's.
    """
    for name in ("JARVIS_AI_PROVIDER", "JARVIS_LLM_MODEL", "JARVIS_LLM_BASE_URL",
                 "JARVIS_LLM_API_KEY", "JARVIS_LLM_FAST_MODEL",
                 "JARVIS_LLM_REASONING_MODEL"):
        monkeypatch.delenv(name, raising=False)


def _settings(home) -> Settings:
    """A Settings whose home is this test's directory.

    Built directly rather than through ``get_settings`` so the developer's own
    ``ai_selection.json`` and keychain cannot decide the outcome.
    """
    return Settings(home=str(home), llm_api_key="", llm_base_url="",
                    ai_provider="openai")


# ---------------------------------------------------------------------------
# The saved category reaches the chat path
# ---------------------------------------------------------------------------

def test_the_text_category_supplies_provider_model_and_base_url(home):
    """Saving Text & Chat changes what the chat path resolves -- the whole fix."""
    pc.set_category("text", provider="9router", model="ali/qwen3.8-flash",
                    base_url="http://127.0.0.1:20128/v1", home=home)

    from jarvis.config import _overlay_text_category

    s = _settings(home)
    _overlay_text_category(s)

    assert s.ai_provider == "9router"
    assert s.llm_model == "ali/qwen3.8-flash"
    assert s.llm_base_url == "http://127.0.0.1:20128/v1"


def test_the_role_models_follow_the_configured_model(home):
    """Fast/reasoning must not stay on OpenAI names a gateway has never heard of.

    The defaults are ``gpt-4o-mini`` and ``o4-mini``. A non-OpenAI vendor answers
    those with a 404, so leaving them alone would make the planner role fail on a
    correctly configured install.
    """
    pc.set_category("text", provider="9router", model="van",
                    base_url="http://127.0.0.1:20128/v1", home=home)

    from jarvis.config import _overlay_text_category

    s = _settings(home)
    _overlay_text_category(s)

    assert s.llm_fast_model == "van"
    assert s.llm_reasoning_model == "van"


def test_an_unconfigured_category_changes_nothing(home):
    """A tab nobody has filled in is not a configuration."""
    from jarvis.config import _overlay_text_category

    s = _settings(home)
    before = (s.ai_provider, s.llm_model, s.llm_base_url)
    _overlay_text_category(s)

    assert (s.ai_provider, s.llm_model, s.llm_base_url) == before


def test_an_environment_variable_still_wins(home, monkeypatch):
    """The env-is-authoritative rule survives: the tab does not outrank a shell export."""
    pc.set_category("text", provider="9router", model="van",
                    base_url="http://127.0.0.1:20128/v1", home=home)
    monkeypatch.setenv("JARVIS_AI_PROVIDER", "openai")
    monkeypatch.setenv("JARVIS_LLM_MODEL", "gpt-4o")

    from jarvis.config import _overlay_text_category

    s = _settings(home)
    _overlay_text_category(s)

    assert s.ai_provider == "openai"
    assert s.llm_model == "gpt-4o"


# ---------------------------------------------------------------------------
# The key reaches the adapter, and only where it belongs
# ---------------------------------------------------------------------------

def test_the_category_key_reaches_its_own_provider(home, cat_key):
    """The key saved on the Text & Chat tab is the key the chat path uses."""
    secret = cat_key("text")
    pc.set_category("text", provider="9router", model="van",
                    base_url="http://127.0.0.1:20128/v1", home=home)

    assert stored_key("9router", home) == secret
    s = _settings(home)
    s.ai_provider = "9router"
    assert effective_key(s, "9router") == secret


def test_the_category_key_is_never_given_to_another_provider(home, cat_key):
    """A key saved for 9Router must not be sent to OpenAI.

    This is the security half of the bridge: widening where a credential travels
    would leak one vendor's secret to another.
    """
    cat_key("text")
    pc.set_category("text", provider="9router", model="van",
                    base_url="http://127.0.0.1:20128/v1", home=home)

    assert stored_key("openai", home) is None
    assert stored_key("gemini", home) is None


def test_a_provider_entry_wins_over_the_category(home, cat_key):
    """The provider card's own key is the more specific answer, so it takes precedence."""
    from jarvis.security.providers import KeychainStorage

    KeychainStorage.store("9router", "sk-from-the-provider-card")
    cat_key("text")
    pc.set_category("text", provider="9router", model="van", home=home)
    try:
        assert stored_key("9router", home) == "sk-from-the-provider-card"
    finally:
        KeychainStorage.delete("9router")


# ---------------------------------------------------------------------------
# The off switch only ever disables
# ---------------------------------------------------------------------------

def test_a_disabled_tab_configures_nothing(home):
    """The tab's off switch means "this tab is not a configuration".

    It must be *reversible*: the overlay runs at every startup, so writing
    `ai_enabled: false` from inside it would make "off" permanent and turning the
    tab back on could never undo it.
    """
    pc.set_category("text", provider="9router", model="van", enabled=False, home=home)

    from jarvis.config import _overlay_text_category

    s = _settings(home)
    _overlay_text_category(s)

    assert s.ai_provider == "openai", "a disabled tab changed the provider"
    assert s.llm_model == "gpt-4o-mini", "a disabled tab changed the model"


def test_switching_the_tab_back_on_works(home):
    """The switch must round-trip: off then on leaves the tab configuring again."""
    from jarvis.config import _overlay_text_category

    pc.set_category("text", provider="9router", model="van", enabled=False, home=home)
    s = _settings(home)
    _overlay_text_category(s)
    assert s.ai_provider == "openai"

    pc.set_category("text", provider="9router", model="van", enabled=True, home=home)
    s2 = _settings(home)
    _overlay_text_category(s2)
    assert s2.ai_provider == "9router"
    assert s2.llm_model == "van"


# ---------------------------------------------------------------------------
# The base URL the adapter will actually call
# ---------------------------------------------------------------------------

def test_the_adapter_calls_the_category_endpoint(home, cat_key):
    """End to end within the process: settings -> the URL the adapter would POST to."""
    cat_key("text")
    pc.set_category("text", provider="9router", model="van",
                    base_url="http://127.0.0.1:20128/v1", home=home)

    from jarvis.config import _overlay_text_category
    from jarvis.gateway.providers import select_provider

    s = _settings(home)
    _overlay_text_category(s)
    provider = select_provider(s)

    assert provider.name == "9router"
    assert provider.base_url.rstrip("/") == "http://127.0.0.1:20128/v1"
    assert provider.api_key == "sk-category-test-key"
    assert effective_base_url(s, "9router").startswith("http://127.0.0.1:20128")

# ---------------------------------------------------------------------------
# The clock must not steal a question that merely contains the word "time"
# ---------------------------------------------------------------------------

class _Planner:
    """The real rule planner, behind the real tool registry."""

    @staticmethod
    def parse(text):
        from jarvis.core.orchestrator import JarvisCore

        if not hasattr(_Planner, "_core"):
            _Planner._core = JarvisCore()
        return _Planner._core.planner.parse(text)


def _tools(plan):
    return [a.tool for a in getattr(plan, "actions", [])]


@pytest.mark.parametrize("utterance", [
    "what time is it?", "what time is it now", "what's the time",
    "what date is it?", "what day is it",
])
def test_a_clock_question_still_reaches_the_clock(utterance):
    assert "get_time" in _tools(_Planner.parse(utterance)), utterance


@pytest.mark.parametrize("utterance", [
    "what time should I leave if the trip takes 45 minutes?",
    "what time should I leave",
    "what time will I arrive",
    "what time do I need to leave",
])
def test_a_question_about_a_departure_time_is_not_stolen_by_the_clock(utterance):
    """"what time should I leave" asks about a plan, not the wall clock.

    The bare ``the time`` alternative in the clock rule matched any sentence
    containing those words, so a follow-up carrying real conversational context
    was answered with the current time instead of reaching the model that had it.
    """
    assert "get_time" not in _tools(_Planner.parse(utterance)), utterance


# ---------------------------------------------------------------------------
# Calendar answers must cover the window the sentence named
# ---------------------------------------------------------------------------

class _CalendarPlanner:
    @staticmethod
    def parse(text):
        from jarvis.core.orchestrator import JarvisCore

        if not hasattr(_CalendarPlanner, "_core"):
            _CalendarPlanner._core = JarvisCore()
        return _CalendarPlanner._core.planner.parse(text)


def _plan(text):
    plan = _CalendarPlanner.parse(text)
    return plan, (getattr(plan, "actions", []) or [])


@pytest.mark.parametrize("utterance,days", [
    ("what's on my calendar today", 1),
    ("what's on my calendar tomorrow", 1),
    ("list my calendar events this week", 7),
    ("do I have anything scheduled this week", 7),
    ("what's on next week", 7),
    ("anything this weekend", 3),
    ("what have I got this month", 30),
])
def test_a_listing_covers_the_span_it_was_asked_for(utterance, days):
    """"this week" was answered with today's schedule and reported as the week.

    ``days`` was hardcoded to 1 in both the calendar rule and the path that
    reached ``list_events`` through the capability fallback, so a correct empty
    read of a week looked identical to a correct empty read of one day.
    """
    plan, actions = _plan(utterance)
    assert actions, f"{utterance!r} reached no tool"
    assert actions[0].tool == "list_events", actions[0].tool
    assert (actions[0].params or {}).get("days") == days, actions[0].params


@pytest.mark.parametrize("utterance", [
    "show my schedules", "list my recurring schedules",
])
def test_a_job_listing_is_still_a_job_listing(utterance):
    """The span words must not drag Scheduled Jobs over to the calendar."""
    plan, actions = _plan(utterance)
    assert actions and actions[0].tool == "list_schedules", \
        [a.tool for a in actions]


def test_an_empty_read_names_the_window_it_read():
    """"Nothing scheduled for today." while reading 30 days is a false statement."""
    from jarvis.tools.calendar import _window_phrase

    assert _window_phrase("today", 1) == "for today"
    assert "7 days" in _window_phrase("today", 7)
    assert "30 days" in _window_phrase("today", 30)
    assert _window_phrase("today", 7) != _window_phrase("today", 1)
