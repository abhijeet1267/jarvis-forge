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

    # --- Core ---------------------------------------------------------------
    env: str = "dev"
    home: Path = Field(default_factory=_default_home)
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8765

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

        Loopback always; with LAN mode on, the addresses this machine actually owns.
        One list feeds the URLs the server advertises and the origins it accepts, so
        "print an address to type" and "refuse the request that came from it" cannot
        drift apart -- the first phone test in a real browser found exactly that drift.
        Link-local IPv6 is excluded: it needs a zone id the phone cannot guess, and a
        bare IPv6 literal is not a valid URL host anyway.
        """
        from .security.auth import LOOPBACK_HOSTS, interface_addresses

        hosts = ["127.0.0.1"]
        if self.lan_mode:
            for addr in sorted(interface_addresses()):
                if addr in LOOPBACK_HOSTS or addr.startswith("fe80:"):
                    continue
                hosts.append(f"[{addr}]" if ":" in addr else addr)
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
        """True when a model endpoint has been configured."""
        return bool(self.llm_base_url and self.llm_api_key)

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    settings = Settings()
    settings.ensure_dirs()
    return settings
