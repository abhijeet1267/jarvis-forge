"""Text + voice JARVIS -- the first two working milestones.

``Repl`` drives the same orchestrator the API uses. Phase 1 (text) is the
baseline; Phase 2 adds voice: wake-word gating, push-to-talk listening,
text-to-speech and barge-in.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

from .core.models import CommandRequest, CommandResponse
from .core.orchestrator import JarvisCore

BANNER = r"""
   _   _   _   _   _   _   _
  / \ / \ / \ / \ / \ / \ / \
 ( J | A | R | V | I | S )   Forge v0.2
  \_/ \_/ \_/ \_/ \_/ \_/ \_/
  Permissioned multi-agent core -- type /help for commands.
"""

HELP = """
Slash commands
  /help              show this help
  /tools             list registered tools with risk levels
  /devices           list known devices and their status
  /audit [n]         show the last n audit records (default 10)
  /memory [n]        show stored memories
  /confirm <id>      approve a pending action
  /cancel <id>       deny a pending action
  /pending           list pending confirmations
  /quit              exit

Voice
  /voice             toggle speaking replies aloud
  /speak <text>      speak text directly
  /listen [seconds]  record from the microphone, then run the command
  /wake on|off       require the wake word ("Jarvis ...")
  /vstatus           show voice subsystem status

Natural commands (examples)
  Jarvis, open VS Code
  Jarvis, find my report
  Jarvis, what is my battery
  Jarvis, remember that this is my AMR project
  Jarvis, delete /tmp/scratch.txt
  stop everything
"""


def _print_response(response: CommandResponse) -> None:
    print(f"\nJARVIS: {response.reply}")
    if response.actions:
        for result in response.actions:
            marker = "ok" if result.ok else "FAILED"
            print(f"   [{marker}] {result.tool} ({result.duration_ms} ms) verified={result.verified}")
    if response.confirmation_id:
        print(f"   pending confirmation: {response.confirmation_id}")
    print()


class Repl:
    """Interactive session supporting both text and voice."""

    def __init__(self, voice: bool = False) -> None:
        self.core = JarvisCore()
        self.session_id: Optional[str] = None
        self.speak = voice
        self._voice = None

    @property
    def voice_session(self):
        if self._voice is None:
            from .voice import VoiceSession

            self._voice = VoiceSession(self.core)
        return self._voice

    async def run(self) -> None:
        print(BANNER)
        if self.speak:
            print("Voice mode is ON -- replies are spoken aloud. /voice to toggle.\n")

        while True:
            try:
                prompt = "You (voice): " if self.speak else "You: "
                raw = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                return

            if not raw:
                continue

            if raw.startswith("/"):
                if await self._slash(raw):
                    return
                continue

            if self.speak:
                # Route through the voice pipeline so wake-word and follow-up
                # rules apply, exactly as they would for real speech.
                turn = await self.voice_session.process_text(raw, speak=True)
                if turn.rejected:
                    print(f"\nJARVIS: (ignored -- {turn.reject_reason})\n")
                else:
                    _print_response(turn.response)
            else:
                request = CommandRequest(text=raw, source="cli")
                if self.session_id:
                    request.session_id = self.session_id
                self.session_id = request.session_id
                response = await self.core.handle(request)
                _print_response(response)

    async def _slash(self, raw: str) -> bool:
        """Handle a slash command. Returns True to exit."""
        parts = raw.split()
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        rest = raw[len(cmd):].strip()

        if cmd in ("/quit", "/exit", "/q"):
            print("Goodbye.")
            return True

        if cmd == "/help":
            print(HELP)

        elif cmd == "/tools":
            for spec in self.core.tools.all():
                confirm = " (confirm)" if spec.requires_confirmation else ""
                print(f"  [{int(spec.risk)} {spec.risk_label():<11}] {spec.name}{confirm} - {spec.description}")

        elif cmd == "/devices":
            for device in self.core.devices.all():
                status = "ONLINE" if device.online else "OFFLINE"
                print(f"  {device.device_id:<15} {device.type:<12} {status:<8} {', '.join(device.capabilities[:4])}")

        elif cmd == "/audit":
            limit = int(arg) if arg.isdigit() else 10
            for row in self.core.audit.recent(limit):
                print(f"  {row['created_at'][:19]}  {row['result']:<22} {row['tool'] or row['intent']}")

        elif cmd == "/memory":
            limit = int(arg) if arg.isdigit() else 10
            stats = self.core.memory.stats()
            print(f"  memory: {stats}")
            for item in self.core.memory.all(limit=limit):
                print(f"  [{item.scope.value:<9}] {item.content[:90]}")

        elif cmd == "/pending":
            pending = self.core.policy.pending()
            if not pending:
                print("  no pending confirmations")
            for item in pending:
                print(f"  {item['confirmation_id']}  {item['tool']}  {item['preview']}")

        elif cmd in ("/confirm", "/cancel"):
            if not arg:
                print("  usage: /confirm <confirmation_id>")
                return False
            response = await self.core.resolve_confirmation(
                arg, approve=(cmd == "/confirm"), session_id=self.session_id or ""
            )
            _print_response(response)

        elif cmd == "/voice":
            self.speak = not self.speak
            print(f"  voice mode {'ON' if self.speak else 'OFF'}")

        elif cmd == "/speak":
            if not rest:
                print("  usage: /speak <text>")
                return False
            result = await self.voice_session.speak(rest)
            print(f"  {'spoke' if result.ok else 'failed'}: {result.backend} {result.error}".rstrip())

        elif cmd == "/listen":
            seconds = float(arg) if arg else None
            print(f"  listening{f' for {seconds}s' if seconds else ''}...")
            turn = await self.voice_session.listen_and_respond(seconds)
            if turn.rejected:
                print(f"  could not listen: {turn.reject_reason}")
            else:
                print(f"  heard: {turn.transcript!r}")
                _print_response(turn.response)

        elif cmd == "/wake":
            if arg.lower() in ("on", "off"):
                self.core.settings.require_wake_word = arg.lower() == "on"
                print(f"  wake word required: {self.core.settings.require_wake_word}")
            else:
                print(f"  wake word required: {self.core.settings.require_wake_word}")

        elif cmd == "/vstatus":
            import json

            print(json.dumps(self.voice_session.status(), indent=2))

        else:
            print(f"  unknown command {cmd!r}; try /help")

        return False


def main(voice: bool = False) -> None:  # pragma: no cover - entrypoint
    repl = Repl(voice=voice)
    try:
        asyncio.run(repl.run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":  # pragma: no cover
    main()
