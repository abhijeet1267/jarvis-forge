"""The example environment must actually load as a configuration.

A reader on a second laptop copies ``.env.example`` to ``.env`` and starts the
server. If that template cannot be parsed, the clone fails at startup for a
reason that has nothing to do with their code.

**This was broken.** ``JARVIS_ALLOWED_COMMANDS`` and ``JARVIS_WAKE_WORDS`` were
written as comma-separated strings (``ls,cat,head,...``). ``pydantic-settings``
2.x decodes ``List[str]`` fields as **JSON before** any ``field_validator`` runs,
so a comma string raises ``SettingsError`` and the server never starts -- the
``mode="before"`` splitter in ``jarvis/config.py`` never gets a chance. The fix is
JSON-array syntax (``["ls","cat"]``), which is what these tests pin.

The tests parse the file the way ``python-dotenv`` / ``pydantic-settings`` do --
key=value, no expansion -- and construct the real :class:`Settings` from exactly
what it holds, so any future un-parseable value is caught here rather than on
someone's second machine.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / ".env.example"


def _parse_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_the_example_environment_is_a_valid_configuration():
    """Every value in the template parses into Settings without error."""
    from jarvis.config import Settings

    values = _parse_example()
    saved = {k: os.environ.get(k) for k in os.environ if k.startswith("JARVIS_")}
    try:
        for key in list(os.environ):
            if key.startswith("JARVIS_"):
                del os.environ[key]
        os.environ.update({k: v for k, v in values.items() if v})

        settings = Settings()

        # The two fields that used to be comma strings, decoded for real.
        assert settings.allowed_commands[:3] == ["ls", "cat", "head"]
        assert "jarvis" in settings.wake_words
    finally:
        for key in list(os.environ):
            if key.startswith("JARVIS_"):
                del os.environ[key]
        os.environ.update({k: v for k, v in saved.items() if v is not None})


def test_list_valued_examples_use_json_not_comma_strings():
    """The specific regression, stated directly so its cause is legible.

    A comma-separated value for a ``List[str]`` setting is the shape that broke
    startup. If a future edit reintroduces it here, this fails with the reason.
    """
    values = _parse_example()
    for key in ("JARVIS_ALLOWED_COMMANDS", "JARVIS_WAKE_WORDS"):
        value = values.get(key, "")
        if not value:
            continue  # empty is fine -- the field keeps its default
        assert value.startswith("[") and value.endswith("]"), (
            f"{key} must be a JSON array (it is a List[str] setting); "
            f"got {value!r}")


def test_the_example_covers_every_setting_field():
    """A variable a user must set but the template never names is a trap."""
    import re

    from jarvis.config import Settings

    body = (REPO / "jarvis" / "config.py").read_text(encoding="utf-8")
    settings_body = body.split("class Settings", 1)[1].split("\nclass ", 1)[0]
    fields = [
        f for f in re.findall(r"^\s{4}([a-z][a-z0-9_]*):\s*", settings_body, re.M)
        if f not in ("try", "env", "else", "warnings")
    ]
    have = set(re.findall(r"^(JARVIS_[A-Z0-9_]+)", EXAMPLE.read_text(encoding="utf-8"), re.M))
    missing = [f"JARVIS_{f.upper()}" for f in fields
               if f"JARVIS_{f.upper()}" not in have]
    assert not missing, f"settings with no example line: {missing}"


def test_the_example_contains_no_real_credentials():
    """It is a template: a key-shaped value implies a leaked key."""
    import re

    text = EXAMPLE.read_text(encoding="utf-8")
    for pattern, name in (
        (r"AIza[0-9A-Za-z_\-]{35}", "Google API key"),
        (r"GOCSPX-[0-9A-Za-z_\-]{20,}", "Google client secret"),
        (r"gh[pousr]_[A-Za-z0-9]{36,}", "GitHub token"),
        (r"sk-(?!test|abcdef)[A-Za-z0-9]{20,}", "OpenAI-style key"),
    ):
        assert not re.search(pattern, text), f"the example carries a real {name}"

def test_a_blank_list_value_means_unset_not_a_crash(tmp_path):
    """``JARVIS_CORS_ORIGINS=`` must fall back to the default, not raise.

    This is the exact line a copied template contains. pydantic-settings decodes
    list fields as JSON before any validator, so a blank string is
    ``json.loads("")`` -> SettingsError and the server will not start. The
    ``settings_customise_sources`` hook in ``jarvis/config.py`` treats a blank
    value as "not set". ``"[]"`` remains the way to say "an empty list".
    """
    from jarvis.config import Settings

    env = tmp_path / "blank.env"
    env.write_text(
        "JARVIS_CORS_ORIGINS=\n"
        "JARVIS_ALLOWED_COMMANDS=\n"
        "JARVIS_POWERFUL_COMMANDS=\n"
        "JARVIS_WAKE_WORDS=\n",
        encoding="utf-8",
    )
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("JARVIS_")}
    try:
        settings = Settings(_env_file=str(env))
        assert settings.allowed_commands[:2] == ["ls", "cat"]  # default applied
        assert settings.cors_origins == []
        assert settings.powerful_commands == []
    finally:
        os.environ.update(saved)

    # An explicit empty array is still honoured as "an empty list".
    env_empty = tmp_path / "empty.env"
    env_empty.write_text("JARVIS_CORS_ORIGINS=[]\n", encoding="utf-8")
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("JARVIS_")}
    try:
        assert Settings(_env_file=str(env_empty)).cors_origins == []
    finally:
        os.environ.update(saved)


def test_a_copy_of_the_example_sets_the_server_up(tmp_path):
    """The literal second-laptop step: copy the template to `.env`, load it."""
    import shutil

    from jarvis.config import Settings

    target = tmp_path / ".env"
    shutil.copy(EXAMPLE, target)
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("JARVIS_")}
    try:
        settings = Settings(_env_file=str(target))
        assert settings.allowed_commands, "a copied template produced an empty command list"
        assert settings.port == 8765
    finally:
        os.environ.update(saved)


def test_a_tilde_home_from_the_example_lands_in_the_real_home(tmp_path, monkeypatch):
    """``JARVIS_HOME=~/.jarvis-forge`` must mean the user's home, not ``./~``.

    **This was broken.** The template says ``~/.jarvis-forge``, and dotenv does
    no expansion. ``_default_home`` expanded it, but three other readers of the
    same variable returned the literal path -- so on a fresh clone the token,
    database and onboarding state were created in a directory named ``~``
    inside the repository (which ``.gitignore`` then hid, because ``*~`` is a
    backup-file rule), while the real ``~/.jarvis-forge`` never existed. Every
    reader now goes through one expanding resolver.
    """
    from jarvis.config import Settings, data_home, llm_file_path
    from jarvis.gateway.selection import selection_path
    from jarvis.wizard import OnboardingManager

    monkeypatch.setenv("JARVIS_HOME", "~/.jarvis-forge")
    expected = Path.home() / ".jarvis-forge"

    # The root cause: the field is populated from the environment as-is, so
    # `default_factory` never ran and nothing expanded the tilde.
    assert Settings().home == expected, "Settings.home kept the literal '~'"
    assert data_home() == expected
    assert llm_file_path().parent == expected
    assert selection_path().parent == expected
    assert OnboardingManager().state_dir == expected

    # The failure this pins: a relative, unexpanded path resolved inside the
    # checkout rather than the home directory.
    for resolved in (Settings().home, data_home(), llm_file_path().parent, selection_path().parent):
        assert not str(resolved).startswith("~"), resolved
        assert "AppData/Vantage/~" not in str(resolved), resolved