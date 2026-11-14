"""One-off probe: what does the pipeline answer for a given sentence?"""

import asyncio
import sys

from jarvis.core.models import CommandRequest
from jarvis.core.orchestrator import JarvisCore


async def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else "cancel my Design Review event"
    core = JarvisCore()
    resp = await core.handle(CommandRequest(text=text))
    print("=" * 70)
    print("text        =", text)
    print("reply       =", resp.reply)
    print("intent      =", resp.intent)
    print("planner     =", getattr(resp.plan, "planner", None))
    print("reply_kind  =", getattr(resp.plan, "reply_kind", None))
    print("named_thing =", repr(getattr(resp.plan, "named_thing", None)))
    print("sources     =", getattr(resp.plan, "identifier_sources_used", None))
    print("missing     =", getattr(resp.plan, "missing_identifiers", None))


asyncio.run(main())
