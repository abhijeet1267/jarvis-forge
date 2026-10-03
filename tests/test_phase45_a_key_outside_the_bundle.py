"""Phase 45a: a key the app can read without damaging itself.

The reader was offered a Gemini key and told to put it in an environment variable, because
that is the only way that works today. It is not a good way. `Settings` reads `.env` from the
*current working directory*, and for the installed app that directory is
`Vantage.app/Contents/Resources/app` -- so the one file the app would naturally read sits
inside the bundle, and a file added to a bundle after it has been signed invalidates the seal.
That is exactly the defect Phase 40 found by copying the app and running `codesign --verify`:
*"a sealed resource is missing or invalid"*. A key file that breaks the signature is a feature
that damages the thing it configures.

So the key lives beside the data -- `~/.jarvis-forge/llm.env`, mode 0600, outside the bundle --
the real environment still wins over it, a loose file mode is complained about out loud, and
the status page says where the configuration came from instead of only that something is set.
"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A data directory with nothing in it, and the environment cleaned of LLM vars."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    for name in ("JARVIS_LLM_API_KEY", "JARVIS_LLM_BASE_URL", "JARVIS_LLM_MODEL",
                 "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _reload(home):
    """Re-read config the way a fresh process would.

    `get_settings` is process-wide, so the only way to test "what does a new launch see" is
    to make one.
    """
    config = importlib.reload(importlib.import_module("jarvis.config"))
    return config


@pytest.fixture()
def client(core):
    from fastapi.testclient import TestClient

    from jarvis.config import get_settings

    # `jarvis.api.app` is shadowed by the FastAPI instance the package re-exports.
    app_module = importlib.import_module("jarvis.api.app")
    with TestClient(app_module.app, base_url="http://127.0.0.1:8765",
                    headers={"X-JARVIS-Token": get_settings().api_token}) as test_client:
        yield test_client


def test_a_key_written_next_to_the_data_is_picked_up(home):
    (home / "llm.env").write_text(
        "JARVIS_LLM_API_KEY=file-supplied-key\n"
        "JARVIS_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/\n"
        "JARVIS_LLM_MODEL=gemini-2.5-flash\n")
    settings = _reload(home).get_settings()
    assert settings.llm_api_key == "file-supplied-key"
    assert settings.llm_model == "gemini-2.5-flash"


def test_the_real_environment_still_wins(home, monkeypatch):
    (home / "llm.env").write_text("JARVIS_LLM_API_KEY=file-key\n")
    monkeypatch.setenv("JARVIS_LLM_API_KEY", "environment-key")
    settings = _reload(home).get_settings()
    assert settings.llm_api_key == "environment-key", (
        "a file silently outranking the shell is how a stale key outlives the thing it was "
        "meant to replace")


def test_no_file_changes_nothing(home):
    settings = _reload(home).get_settings()
    assert settings.llm_api_key == ""


def test_a_comment_and_a_blank_line_are_not_secrets(home):
    (home / "llm.env").write_text(
        "# put your key here\n\n   \nJARVIS_LLM_MODEL=gemini-2.5-flash\n")
    settings = _reload(home).get_settings()
    assert settings.llm_model == "gemini-2.5-flash"


def test_an_unreadable_line_is_skipped_not_fatal(home):
    """A typo in a hand-edited file must not stop the product booting."""
    (home / "llm.env").write_text("not a key value line\nJARVIS_LLM_MODEL=ok-2\n")
    settings = _reload(home).get_settings()
    assert settings.llm_model == "ok-2"


def test_a_key_readable_by_the_whole_machine_is_said_out_loud(home):
    """The file holds a credential; 0644 is a fact worth a sentence, not a silent one."""
    path = home / "llm.env"
    path.write_text("JARVIS_LLM_API_KEY=exposed-key\n")
    os.chmod(path, 0o644)
    config = _reload(home)
    warnings = config.apply_llm_file()
    assert any("600" in w for w in warnings), warnings
    assert config.get_settings().llm_api_key == "exposed-key", "warned, not refused"


def test_the_warning_never_prints_the_key(home, capsys):
    path = home / "llm.env"
    path.write_text("JARVIS_LLM_API_KEY=a-secret-nobody-should-see\n")
    os.chmod(path, 0o644)
    config = _reload(home)
    printed = str(config.apply_llm_file()) + capsys.readouterr().out
    assert "a-secret-nobody-should-see" not in printed


def test_the_key_file_is_never_staged_into_the_bundle():
    """The whole point of the file is that it sits outside the sealed folder."""
    build_app = importlib.import_module("build_app")
    assert "llm.env" not in build_app.STAGED_ITEMS
    assert ".env.example" in build_app.STAGED_ITEMS, (
        "the example file is staged, so a reader might create its real twin inside the app")


def test_the_source_of_the_model_configuration_is_named(home):
    """`llm_enabled: true` hides the difference between a shell key and an empty file.

    42-B: a configured model is not a reachable one, and "where did this come from" is the
    next thing a person asks when it does not answer.
    """
    config = _reload(home)
    config.get_settings()                      # a process only knows after it has loaded
    assert config.llm_source() == "none"

    (home / "llm.env").write_text("JARVIS_LLM_API_KEY=file-key\n")
    config = _reload(home)
    config.get_settings()
    assert config.llm_source() == "llm.env"


def test_health_reports_that_source(client, home):
    """The status page is the one place a user looks when the model is not answering."""
    body = client.get("/health").json()
    assert body["llm_source"] in ("none", "env", "llm.env"), body
