#!/usr/bin/env python3
"""Convenience launcher for Vantage.

    python run.py            # start the API + dashboard
    python run.py cli        # text REPL
    python run.py voice      # voice REPL (speaks replies aloud)
    python run.py demo       # run the acceptance commands and print results
    python run.py voice-demo # run the acceptance commands through the voice pipeline

Flags for the server mode, refused rather than ignored if unrecognised:

    --port N     the port to bind        (same as JARVIS_PORT)
    --host H     the address to bind     (same as JARVIS_HOST)
    --home DIR   the data directory      (same as JARVIS_HOME)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Flags `run.py` accepts, mapped to the environment name `Settings` reads. Anything else is
#: refused by `apply_flags` rather than dropped: measured on 2026-10-06, `python run.py serve
#: --port 8899` started on **8765**, because `main()` looked only at `argv[1]`.
_FLAGS: Dict[str, str] = {"--port": "JARVIS_PORT", "--host": "JARVIS_HOST",
                          "--home": "JARVIS_HOME"}

_MODES = ("serve", "cli", "voice", "demo", "voice-demo")


def apply_flags(argv: List[str]) -> str:
    """Resolve the mode and put the supported flags where the settings will pick them up.

    An unknown flag or mode exits non-zero. A silently ignored `--port` is how "verified on an
    isolated instance" turns into a second server on the production port, polling-visible to
    whoever already had the dashboard open.
    """
    argv = list(argv)
    if not argv:
        return "serve"
    if argv[0].startswith("-"):
        mode, rest = "serve", argv
    else:
        mode, rest = argv[0], argv[1:]

    if mode not in _MODES:
        raise SystemExit(f"run.py: unknown mode {mode!r}; expected one of {', '.join(_MODES)}")

    index = 0
    while index < len(rest):
        flag = rest[index]
        env_key = _FLAGS.get(flag)
        if env_key is None:
            raise SystemExit(
                f"run.py: unknown flag {flag!r}; supported: {', '.join(sorted(_FLAGS))}")
        if index + 1 >= len(rest):
            raise SystemExit(f"run.py: {flag} needs a value")
        value = rest[index + 1]
        if flag == "--port" and not value.isdigit():
            raise SystemExit(f"run.py: --port wants a number, got {value!r}")
        os.environ[env_key] = value
        index += 2
    return mode

ACCEPTANCE = [
    "open VS Code",
    "find my latest project report",
    "what is my laptop battery",
    "what time is it",
    "take a screenshot",
    "remember that this is my AMR project",
    "what did I work on yesterday",
    "check my car fuel",
    "give me today's location summary",
    "send an email to my teammate",
    "transfer this file to my phone",
    "plan my day",
    "delete everything",
    "stop all physical actions",
]


def main() -> None:
    mode = apply_flags(sys.argv[1:])

    if mode == "cli":
        from jarvis.cli import main as cli_main

        cli_main(voice=False)
        return

    if mode == "voice":
        from jarvis.cli import main as cli_main

        cli_main(voice=True)
        return

    if mode == "demo":
        import asyncio

        from jarvis.core.models import CommandRequest
        from jarvis.core.orchestrator import JarvisCore

        core = JarvisCore()

        async def run() -> None:
            for text in ACCEPTANCE:
                response = await core.handle(CommandRequest(text=text, source="demo"))
                decision = response.decision.value if response.decision else "-"
                print(f"\n>>> {text}")
                print(f"    intent={response.intent}  decision={decision}")
                print(f"    {response.reply}".replace("\n", "\n    "))

        asyncio.run(run())
        return

    if mode == "voice-demo":
        import asyncio

        from jarvis.core.orchestrator import JarvisCore
        from jarvis.voice import VoiceSession

        core = JarvisCore()
        session = VoiceSession(core)
        # Spoken wake word, exactly as it would arrive from speech recognition.
        spoken = [f"Jarvis, {text}" for text in ACCEPTANCE]

        async def run() -> None:
            print(f"wake words: {session.wake.words}")
            print(f"tts backend: {session.tts.name}  stt backend: {session.stt.name}\n")
            for text in spoken:
                turn = await session.process_text(text, speak=False)
                data = turn.as_dict()
                status = "REJECTED" if data["rejected"] else data["decision"]
                print(f"\n>>> {text}")
                print(f"    heard={data['transcript']!r} intent={data['intent']} decision={status}")
                if data["rejected"]:
                    print(f"    {data['reject_reason']}")
                else:
                    print(f"    {data['reply']}".replace("\n", "\n    "))

        asyncio.run(run())
        return

    import uvicorn

    from jarvis.api.app import app
    from jarvis.config import get_settings

    settings = get_settings()
    # `0.0.0.0` is a bind instruction; nobody can type it into a browser and reach this
    # machine. The phone URLs the app prints at startup carry the real addresses.
    display_host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    print(f"Vantage core       -> {settings.scheme}://{display_host}:{settings.port}/dashboard",
          flush=True)
    print(f"Voice status      -> {settings.scheme}://{display_host}:{settings.port}"
          "/voice/status", flush=True)

    ssl_kwargs: dict = {}
    if settings.https_enabled:
        # A browser refuses to treat plain http from another device as a secure context,
        # and without one a phone cannot install the client as an app. The certificate is
        # self-signed, so the handset shows a warning the user has to accept: that is the
        # honest cost of encrypting a link with no domain name behind it.
        from jarvis.transport.tls import TLSUnavailable, ensure_certificate

        try:
            material = ensure_certificate(settings)
        except TLSUnavailable as exc:
            print(f"Vantage: HTTPS requested but unavailable -- {exc}", flush=True)
            raise SystemExit(1)
        ssl_kwargs = {"ssl_certfile": str(material.cert_path),
                      "ssl_keyfile": str(material.key_path)}
        print(f"TLS certificate   -> {material.cert_path} (sha256 {material.fingerprint[:16]}…)",
              flush=True)

    # Started by the desktop panel? Then the panel's death is ours: a server adopted by
    # launchd keeps the wifi port open with no window left to close it.
    import os

    from jarvis.process import start_guardian_from_env

    start_guardian_from_env(on_exit=lambda: os._exit(0))

    from jarvis.persistence import serve_with_claim

    def bind(*, host, port):
        uvicorn.run(app, host=host, port=port, log_level="info", **ssl_kwargs)

    # Claim the data directory for the lifetime of this process, so a login item and a panel
    # window cannot both run schedulers over one database, and so a port that is already taken
    # ends this process quietly instead of in a traceback launchd would keep retrying.
    _served, code = serve_with_claim(settings.home, pid=os.getpid(), host=settings.host,
                                     port=settings.port, serve=bind)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
