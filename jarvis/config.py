"""Central configuration for Vantage.

Everything is driven by environment variables (see ``.env.example``) so the
system can be deployed without editing code. Values are cached on first access.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("JARVIS_HOME", "~/.jarvis-forge")))


#: The desktop app's "allow devices on my wifi" checkbox writes this file. It is read as
#: one of exactly two words, never evaluated: a switch that parses arbitrary content is
#: an injection surface hiding in a settings file.
LAN_SWITCH = "lan_mode"


def lan_mode_path(home: Path) -> Path:
    return Path(home) / LAN_SWITCH


def lan_mode_state(home: Path) -> "bool | None":
    """True/false when the app wrote the switch, None when nobody ever touched it."""
    try:
        text = lan_mode_path(home).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    if text in ("true", "1", "on", "yes"):
        return True
    if text in ("false", "0", "off", "no"):
        return False
    return None


def set_lan_mode(home: Path, enabled: bool) -> Path:
    """Record the switch. Writing it does not open anything by itself -- the startup
    guard in `security.auth` still refuses a LAN mode without a token."""
    path = lan_mode_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("true\n" if enabled else "false\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class Settings(BaseSettings):
    """Runtime settings. All fields map to ``JARVIS_*`` env vars."""

    model_config = SettingsConfigDict(
        env_prefix="JARVIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: List-valued settings decode ``JARVIS_X`` as **JSON** before any
    #: ``field_validator`` runs (``pydantic-settings`` 2.x). That means a blank
    #: line -- ``JARVIS_CORS_ORIGINS=``, exactly what a copied ``.env.example``
    #: contains -- tries ``json.loads("")`` and raises ``SettingsError``, so the
    #: server will not start. A blank value plainly means "not set": it is
    #: replaced with ``None`` before decoding, so the field falls back to its
    #: default. ``"[]"`` still means "an empty list", which is a deliberate and
    #: different choice.
    @classmethod
    def settings_customise_sources(
        cls, settings_cls,
        init_settings, env_settings, dotenv_settings, file_secret_settings,
    ):
        _list_fields = {"cors_origins", "allowed_commands",
                        "powerful_commands", "wake_words"}

        def _blank_lists_are_unset(source):
            if source is None:
                return None
            original = source.prepare_field_value

            def prepare_field_value(field_name, field, value, value_is_complex):
                if (field_name in _list_fields
                        and isinstance(value, str) and value.strip() == ""):
                    return None  # "unset": the field's default applies
                return original(field_name, field, value, value_is_complex)

            source.prepare_field_value = prepare_field_value
            return source

        return (
            init_settings,
            _blank_lists_are_unset(env_settings),
            _blank_lists_are_unset(dotenv_settings),
            file_secret_settings,
        )

    # --- Core ---------------------------------------------------------------
    env: str = "dev"
    home: Path = Field(default_factory=_default_home)
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8765
    companion_port: int = 8766

    #: Where a relative filename typed by a user is anchored.
    #:
    #: Deliberately not the process working directory: "create a file called
    #: hello.txt" would otherwise land in whatever folder the server happened to
    #: be started from, and "../escape.txt" would escape it freely -- the system
    #: path guard refuses *OS* locations, not the CWD. Override with
    #: JARVIS_FILE_WORKSPACE.
    file_workspace: str = "~/JARVIS_REAL_TEST"

    #: Let devices other than this laptop reach the API. Off by default, and
    #: deliberately noisy when on: `host_allowed` stops accepting only loopback and
    #: starts accepting the addresses this machine actually owns, which keeps the
    #: DNS-rebinding defence while opening the door to a phone on the same network.
    #: Starting with it on requires authentication to be on, and a bind address that
    #: is not loopback -- see ``security.auth.lan_startup_problems``.
    lan_mode: bool = False

    #: Serve TLS in front of the API. Off by default; when on, the advertised phone URLs
    #: become https, because a browser refuses to treat plain http from another device as
    #: a secure context and an installable app needs one.
    https_enabled: bool = False

    # --- Policy -------------------------------------------------------------
    paranoid: bool = False
    confirm_channel: str = "cli"
    confirm_timeout: int = 300

    # --- API security -------------------------------------------------------
    # Token auth is ON by default. A localhost HTTP API that executes real
    # actions is reachable by any web page the user visits, so this is not
    # something to opt into.
    auth_enabled: bool = True
    # Blank -> a token is generated once and stored at <home>/api_token (0600).
    api_token: str = ""
    # Extra browser origins allowed to call the API. The dashboard origin is
    # always allowed; wildcards are rejected outright.
    cors_origins: List[str] = Field(default_factory=list)
    # Reject requests whose Host header is not a loopback name (anti DNS-rebinding).
    enforce_host_header: bool = True
    # Publish the API's own shape at /openapi.json, /docs and /redoc.
    #
    # FastAPI mounts those three for you, and none of them takes a credential. With
    # `lan_mode` on, `host_allowed` deliberately accepts this machine's own Wi-Fi address,
    # so the unauthenticated documentation is not a localhost convenience: it is readable
    # from anything on the network, and what it publishes is every route, every parameter
    # and every tool name this laptop can be asked to run. Phase 9 recorded it at 71,047
    # bytes; it was measured again at 82,556 before it was turned off. Development keeps
    # it behind one switch.
    api_docs: bool = False
    # Token-bucket rate limiting, per client address.
    #
    # Sized with headroom: the dashboard polls several endpoints every 2.5s
    # (~150 requests/minute), so a tighter limit would throttle the user's own
    # UI. This still stops a runaway script or a loop.
    rate_limit_per_minute: int = 600
    rate_limit_burst: int = 120

    # --- Privacy (section 49) ----------------------------------------------
    # Credentials are always redacted from audit records, logs and model
    # prompts. Privacy mode additionally masks personal identifiers (email
    # addresses, phone numbers) and collapses home-directory paths to "~".
    privacy_mode: bool = False
    # Set false only if you are debugging and accept secrets in the audit log.
    redact_audit: bool = True

    # --- Model gateway ------------------------------------------------------
    #: Which vendor the gateway talks to. `openai` is the shape this build has always
    #: used, so an install that never sets this keeps the same bytes on the wire.
    #: An unrecognised name is refused by `select_provider`, not defaulted.
    ai_provider: str = "openai"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_fast_model: str = "gpt-4o-mini"
    llm_reasoning_model: str = "o4-mini"
    # Used by the memory embedder and the vision agent when a model endpoint is
    # configured. Both fall back to local, dependency-free implementations.
    llm_embed_model: str = "text-embedding-3-small"
    vision_model: str = "gpt-4o-mini"

    # --- Safety -------------------------------------------------------------
    trash_on_delete: bool = True

    # Commands JARVIS may run. Split deliberately:
    #
    #   allowed_commands  -- read-only/inspection tools. Safe to expose.
    #   powerful_commands -- interpreters and package managers. These are
    #                        Turing-complete: `python3 -c '...'` is arbitrary
    #                        code execution, and `npm install` runs arbitrary
    #                        postinstall scripts. Empty by default.
    #
    # Putting an interpreter on the allowlist does not make it safe; it makes
    # the allowlist decorative. Opt in explicitly if you want them.
    allowed_commands: List[str] = Field(
        default_factory=lambda: [
            "ls",
            "cat",
            "head",
            "tail",
            "wc",
            "file",
            "stat",
            "echo",
            "date",
            "uptime",
            "df",
            "du",
            "whoami",
            "hostname",
            "uname",
            "pwd",
            "sw_vers",
        ]
    )
    powerful_commands: List[str] = Field(default_factory=list)

    # --- Email (v0.7) -------------------------------------------------------
    # Leave blank to use the local maildir (real .eml files, no account).
    # Set all of host + address + password to use a real IMAP/SMTP account.
    email_address: str = ""
    email_password: str = ""
    email_imap_host: str = ""
    email_imap_port: int = 993
    email_smtp_host: str = ""
    email_smtp_port: int = 465

    # --- Calendar (v0.7) ----------------------------------------------------
    # Leave blank to use the local SQLite calendar.
    calendar_url: str = ""
    calendar_user: str = ""
    calendar_password: str = ""

    # --- Google (Calendar + Tasks) -----------------------------------------
    # The OAuth *client* identifiers only. The refresh token is never a
    # setting: it lives in the OS keychain (see integrations/google_auth.py),
    # because settings are read into logs and diagnostics far more casually
    # than a credential should be.
    #
    # A desktop app is a "public client" under Google's rules -- it cannot keep
    # a secret -- so only the client id is needed for the installed-app flow.
    google_client_id: str = ""
    google_client_secret: str = ""
    # Least privilege: read calendar, read+write tasks. Calendar write is a
    # separate scope so it can be requested only once event editing exists.
    google_calendar_enabled: bool = True
    google_tasks_enabled: bool = True
    # The calendar to read when the account has several. Empty means "primary".
    google_calendar_id: str = "primary"
    google_task_list_id: str = ""

    # --- n8n automation ----------------------------------------------------
    # Vantage runs only workflows that are in its own registry
    # (integrations/n8n.py) and only over their webhooks. It deliberately has
    # no access to n8n's admin API, so a chat turn cannot create, edit or
    # activate a workflow.
    #
    # n8n_api_key is read from the environment or the OS keychain and is never
    # returned by any route. The URL must be http(s); a non-loopback host is
    # refused unless n8n_allow_remote is deliberately turned on.
    n8n_enabled: bool = False
    n8n_base_url: str = "http://127.0.0.1:5678"
    n8n_api_key: str = ""
    n8n_auth_header: str = "Authorization"
    n8n_auth_prefix: str = "Bearer "
    n8n_timeout: float = 20.0
    n8n_allow_remote: bool = False
    #: Comma-separated registry keys. Empty means every approved workflow.
    n8n_allowed_workflows: str = ""

    # --- MCP (one remote connector) ----------------------------------------
    # One Streamable-HTTP MCP server (Zapier MCP, an n8n MCP node, or another).
    # Empty URL means "not configured": nothing is attempted and no tool exists.
    #
    # JARVIS_MCP_ALLOWED_TOOLS is a comma-separated allowlist and defaults to
    # EMPTY on purpose. Discovering a server's tools is not the same as making
    # them callable, so a server that advertises hundreds of actions still
    # exposes only the names listed here -- each one through the normal
    # approval path, because the adapter registers them as EXTERNAL writes.
    #
    # The token is read from the environment but never echoed: status payloads
    # and error messages redact it (integrations/mcp_client.py).
    mcp_name: str = "mcp"
    mcp_url: str = ""
    mcp_token: str = ""
    mcp_allowed_tools: str = ""

    # --- Design (v1.2) -------------------------------------------------------
    # External design tools are opt-in and must be BOTH configured and enabled
    # before use. Nothing is on by default: artifacts are generated locally and
    # labelled as such.
    design_figma_token: str = ""
    design_figma_enabled: bool = False
    design_mermaid_path: str = ""
    design_mermaid_enabled: bool = False

    # --- Scheduler + briefing (v0.8) --------------------------------------
    # Which briefing sections to include. Empty means all of them.
    briefing_sections: str = ""
    # Run scheduled jobs in the background while the server is up.
    scheduler_enabled: bool = True

    # --- Voice (section 3 / Phase 2) ---------------------------------------
    voice_enabled: bool = True
    # auto | macos_say | pyttsx3 | null
    tts_backend: str = "auto"
    tts_voice: str = ""          # empty -> system default
    tts_rate: int = 180          # words per minute
    tts_enabled: bool = True
    # auto | macos_speech | whisper_cpp | openai_whisper | transcript
    stt_backend: str = "auto"
    stt_language: str = "en-US"
    stt_model: str = "whisper-1"
    # Wake words accepted before a command (case-insensitive).
    #: The product answers to its own name as well as the original one. Renaming the
    #: brand without touching this list left the voice pipeline listening for a name
    #: no longer on the window.
    wake_words: List[str] = Field(default_factory=lambda: [
        "jarvis", "hey jarvis", "ok jarvis",
        "vantage", "hey vantage", "ok vantage",
    ])
    require_wake_word: bool = True
    # Audio capture
    audio_device: str = ":1"     # ffmpeg avfoundation input (":1" = default mic)
    listen_seconds: float = 5.0
    sample_rate: int = 16000
    # Directory for synthesised speech and captured audio.
    voice_dir: str = ""          # empty -> <home>/voice

    @field_validator("allowed_commands", "powerful_commands", mode="before")
    @classmethod
    def _split_commands(cls, value: object) -> object:
        """Accept a comma-separated string from the environment."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("wake_words", mode="before")
    @classmethod
    def _split_wake_words(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().lower() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _apply_lan_switch_file(self) -> "Settings":
        """Let the desktop app open the door without a terminal.

        A double-clicked bundle never sees `export JARVIS_LAN_MODE=1`, so the switch also
        lives in the laptop's own data directory. An explicit environment value still
        wins, because someone debugging from a shell must be able to overrule the window.
        """
        if "JARVIS_LAN_MODE" not in os.environ:
            state = lan_mode_state(self.home)
            if state is not None:
                self.lan_mode = state
        return self

    # --- Derived paths ------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.home / "jarvis.db"

    @property
    def audit_path(self) -> Path:
        return self.home / "audit.jsonl"

    @property
    def log_path(self) -> Path:
        return self.home / "jarvis.log"

    @property
    def voice_path(self) -> Path:
        return Path(self.voice_dir) if self.voice_dir else self.home / "voice"

    @property
    def token_path(self) -> Path:
        return self.home / "api_token"

    @property
    def dashboard_origin(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def scheme(self) -> str:
        """The URL scheme this server's own pages must be addressed with."""
        return "https" if self.https_enabled else "http"

    def client_hosts(self) -> List[str]:
        """Host strings a browser may address this server with.

        Loopback always; with LAN mode on, the addresses this machine actually owns plus
        its mDNS name when that name resolves to one of them.
        One list feeds the URLs the server advertises and the origins it accepts, so
        "print an address to type" and "refuse the request that came from it" cannot
        drift apart -- the first phone test in a real browser found exactly that drift.
        Link-local IPv6 is excluded: it needs a zone id the phone cannot guess, and a
        bare IPv6 literal is not a valid URL host anyway.
        """
        from .security.auth import LOOPBACK_HOSTS, interface_addresses, mdns_name_is_ours

        hosts = ["127.0.0.1"]
        if self.lan_mode:
            for addr in sorted(interface_addresses()):
                if addr in LOOPBACK_HOSTS or addr.startswith("fe80:"):
                    continue
                hosts.append(f"[{addr}]" if ":" in addr else addr)
            # The mDNS name is the one address that survives a lease renewal. It is added
            # here rather than printed separately so the gate that accepts it, the origin
            # list that allows it, and the URL that advertises it cannot disagree -- and
            # it is only advertised when it genuinely resolves to this machine.
            import socket

            # Lowercased on purpose: a browser lowers the host before it sends `Origin`,
            # so a name advertised in mixed case is advertised in a form that comes back
            # different -- and the origin gate read that as a foreign site. Measured: the
            # panel printed `Abhijeets-MacBook-Air-2.local`, Chrome sent the lowercase,
            # and POST /auth/session answered 403 on a page that had just loaded.
            name = socket.gethostname().lower()
            if mdns_name_is_ours(name) and name not in hosts:
                hosts.append(name)
        return hosts

    def reachable_urls(self) -> List[str]:
        """What to type on the other device. ``host = 0.0.0.0`` is a bind instruction."""
        return [f"{self.scheme}://{host}:{self.port}/phone"
                for host in self.client_hosts()]

    def allowed_origins(self) -> List[str]:
        """Browser origins permitted to call the API.

        The dashboard's own origin is always included, along with every origin this
        machine will serve a client page from. A literal ``*`` is filtered out even if
        someone configures it, because a wildcard would let any web page read command
        results.
        """
        origins = [o for o in self.cors_origins if o and o != "*"]
        for host in (self.host, *self.client_hosts()):
            origin = f"{self.scheme}://{host}:{self.port}"
            if origin not in origins:
                origins.append(origin)
        return origins

    @property
    def llm_enabled(self) -> bool:
        """True when a model endpoint can actually be reached.

        Two conditions, and both must hold:
          1. the assistant is switched on (the AI enable/disable control; default on),
          2. the selected provider can be called -- a key in the environment/``llm.env``
             or in the secure store the AI Settings screen writes to, or a provider that
             needs no key at all (a loopback connector whose adapter sets
             ``REQUIRES_KEY = False``).

        The base URL is not part of the test because every supported provider has a
        default endpoint. The on/off switch is checked first so a stored key cannot
        defeat a deliberate "off": the user turning the assistant off means every
        sentence stays on the deterministic rule planner.
        """
        from .gateway.selection import ai_enabled, effective_key
        if not ai_enabled(self.home):
            return False
        if effective_key(self, self.ai_provider):
            return True
        return not _provider_requires_key(self.ai_provider)

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)


LLM_FILE_NAME = "llm.env"


def _provider_requires_key(provider_name: str) -> bool:
    """Whether the named provider refuses a request that carries no credential.

    Read from the adapter rather than a list here, so a new keyless connector
    declares the fact once (``REQUIRES_KEY = False``) and both this switch and the
    provider routes agree. An unknown name is treated as key-requiring: refusing is
    the safe answer for a name this build cannot call.
    """
    try:
        from .gateway.providers import PROVIDERS
    except Exception:
        return True
    cls = PROVIDERS.get(str(provider_name or "").strip().lower())
    if cls is None:
        return True
    return bool(getattr(cls, "REQUIRES_KEY", True))

#: Where the model configuration came from. `llm_enabled` only says "something is set",
#: which is the difference between a working key, a typo and an empty file -- and the
#: first thing a person asks when the model does not answer.
_LLM_SOURCE = "none"


def llm_file_path() -> Path:
    """The key file's place: beside the data, never inside the app bundle.

    `Settings` reads `.env` from the current working directory, and for the installed app
    that directory *is* `Vantage.app/Contents/Resources/app`. A file created there after the
    bundle was signed invalidates its seal -- the exact defect Phase 40 found by copying the
    app and running `codesign --verify` -- so a key that breaks the thing it configures is
    not a feature.
    """
    root = os.environ.get("JARVIS_HOME") or _default_home()
    return Path(root).expanduser() / LLM_FILE_NAME


def llm_source() -> str:
    """One of "none", "env", "llm.env"."""
    return _LLM_SOURCE


def apply_llm_file() -> list[str]:
    """Load `llm.env` into the environment, and return what is worth saying out loud.

    The real environment always wins: a key exported in a shell is the one that process
    meant, and a file silently outranking it is how a stale key outlives the thing it was
    written to replace. A file readable by anyone on the machine is complained about and
    still honoured -- refusing to boot over a permission bit would be its own bad trade --
    and no warning ever repeats the secret itself.
    """
    global _LLM_SOURCE

    path = llm_file_path()
    warnings: list[str] = []
    already = bool(os.environ.get("JARVIS_LLM_API_KEY"))

    if path.is_file():
        import stat

        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            mode = 0
        if mode & 0o077:
            warnings.append(
                f"{path} is readable beyond its owner (mode {mode:o}) and holds a "
                "credential; run: chmod 600 " + str(path))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            warnings.append(f"{path} could not be read: {exc}")
            lines = []

        for line in lines:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, _, value = text.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            name = key if key.startswith("JARVIS_") else f"JARVIS_{key}"
            if os.environ.get(name) or not value:
                continue
            os.environ[name] = value

    if already:
        _LLM_SOURCE = "env"
    elif os.environ.get("JARVIS_LLM_API_KEY"):
        _LLM_SOURCE = "llm.env"
    else:
        _LLM_SOURCE = "none"
    return warnings


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    `llm.env` is applied first because pydantic reads the environment when the model is
    constructed, and the file must not outrank a variable the shell already set. The
    AI-provider *selection* saved through the settings screen is then overlaid for the
    fields it controls, but only where the environment did not already supply a value:
    a shell variable is the one the person meant, and a UI selection must not silently
    outrank it.
    """
    for warning in apply_llm_file():
        _warn(warning)
    settings = Settings()
    settings.ensure_dirs()
    _overlay_ai_selection(settings)
    return settings


def _overlay_ai_selection(settings: "Settings") -> None:
    """Apply the persisted provider/model selection for fields the environment did not set.

    This imports ``gateway.selection`` lazily: it needs ``settings.home`` (already
    available here) and must not form an import cycle, since ``gateway.selection``
    itself imports ``config`` helpers lazily on first use, not at module load.
    """
    try:
        from .gateway.selection import load_selection
    except Exception:
        return

    selection = load_selection(settings.home)
    if not selection:
        return

    # An explicitly exported env var wins over the selection file, matching
    # apply_llm_file's "the real environment always wins" rule.
    provider = selection.get("ai_provider")
    if provider and "JARVIS_AI_PROVIDER" not in os.environ:
        settings.ai_provider = provider

    model = selection.get("llm_model")
    if model and "JARVIS_LLM_MODEL" not in os.environ:
        settings.llm_model = model

    base_url = selection.get("llm_base_url")
    if base_url and "JARVIS_LLM_BASE_URL" not in os.environ:
        settings.llm_base_url = base_url


def _warn(message: str) -> None:
    """Say once what a secret file should have been, without importing the logger early.

    config.py sits under the logging setup, so a module-level logger here would be a cycle.
    """
    try:
        from .logging_setup import get_logger

        get_logger("config").warning(message)
    except Exception:
        pass
