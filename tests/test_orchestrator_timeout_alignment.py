"""The orchestrator's chat path must honor the documented 30s chat budget.

A slow-but-legitimate completion (a cold route on a combo gateway like 9Router)
was cut off at 20s while `/ai/chat/send` and the provider Test route both allowed
30s — so the Command Center (`/command`, which drives this path) dropped to the
rule planner before the model finished. The conversation timeout must match the
30s chat budget everywhere a single chat turn is made.
"""

from __future__ import annotations


def test_orchestrator_conversation_uses_the_30s_chat_budget():
    """The orchestrator's chat path must use exactly 30s to match /ai/chat/send
    and the provider Test route. A cold-route completion on 9Router was measured
    at >20s, so the value is a defined contract, not a default."""
    import re
    import jarvis.core.orchestrator as o

    src = __import__("inspect").getsource(o.JarvisCore._advise_from_model)
    # Find `task_type="conversation", timeout=<value>` in the source.
    m = re.search(r'task_type="conversation",\s*timeout=([\d.]+)', src)
    assert m, "could not find the timeout value in _advise_from_model"
    assert float(m.group(1)) == 30.0, (
        f"the conversation chat budget must be 30s to match /ai/chat/send and "
        f"/test, got {m.group(1)}"
    )