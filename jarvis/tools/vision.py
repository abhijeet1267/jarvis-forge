"""Vision tools (v1.3).

Observe, read, compare — and **propose**. The plan's pipeline is
``SEE → UNDERSTAND → PROPOSE → PERMISSION → ACT``, so vision stops at PROPOSE.
There is no action tool in this module: nothing here clicks, types, moves or
deletes. A vision observation can never become an action on its own.

What is genuinely available in this environment:

* image measurement with Pillow — size, colours, brightness, contrast
* OCR through the macOS Vision framework — real text, real bounding boxes
* screen capture
* image diffing — "did it change, and where?"

Describing *content* ("there is a cat on the desk") needs a vision model. None
is configured, so those requests say so rather than inventing a description.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok
from ..vision.diff import compare
from ..vision.image import inspect
from ..vision.ocr import available as ocr_available
from ..vision.ocr import find_text, read_text
from ..vision.providers import describe as describe_providers
from ..vision.screen import capture, read_screen

log = get_logger("tools.vision")

#: Vision may not act. Kept as an explicit constant so a test can assert it.
ACTION_TOOLS: List[str] = []


async def inspect_image(ctx: ToolContext, path: str = "") -> Any:
    """Report the measurable properties of an image."""
    if not path:
        return fail("inspect_image", "Which image should I look at?", device=ctx.device_id)

    result = inspect(path)
    if not result.get("ok"):
        return fail("inspect_image", result.get("error", "I could not inspect that."), device=ctx.device_id)

    return ok(
        "inspect_image",
        result["summary"],
        device=ctx.device_id,
        image={k: v for k, v in result.items() if k != "summary"},
    )


async def read_image_text(ctx: ToolContext, path: str = "") -> Any:
    """Recognise text in an image (OCR)."""
    if not path:
        return fail("read_image_text", "Which image should I read?", device=ctx.device_id)

    result = read_text(path, settings=ctx.settings)
    if not result.get("ok"):
        return fail("read_image_text", result.get("error", "I could not read that."), device=ctx.device_id)

    if not result.get("text"):
        return ok(
            "read_image_text",
            "I found no text in that image.",
            device=ctx.device_id,
            text="",
            line_count=0,
        )

    preview = result["text"][:300]
    return ok(
        "read_image_text",
        f"I read {result['line_count']} line(s) of text:\n{preview}",
        device=ctx.device_id,
        text=result["text"],
        line_count=result["line_count"],
        lines=result["lines"][:50],
        backend=result.get("backend"),
    )


async def find_image_text(ctx: ToolContext, path: str = "", query: str = "") -> Any:
    """Locate text within an image, returning bounding boxes.

    This finds **words**, not arbitrary widgets — JARVIS can say where the word
    "Submit" is, not "the blue button".
    """
    if not path:
        return fail("find_image_text", "Which image should I search?", device=ctx.device_id)
    if not query:
        return fail("find_image_text", "What text should I look for?", device=ctx.device_id)

    result = find_text(path, query, settings=ctx.settings)
    if not result.get("ok"):
        return fail("find_image_text", result.get("error", "I could not search that."), device=ctx.device_id)

    if not result.get("match_count"):
        return ok(
            "find_image_text",
            f"I did not find {query!r} in that image.",
            device=ctx.device_id,
            matches=[],
            match_count=0,
        )

    first = result["matches"][0]
    return ok(
        "find_image_text",
        (
            f"Found {result['match_count']} match(es) for {query!r}. "
            f"First at ({first['x']:.2f}, {first['y']:.2f}) in normalised coordinates."
        ),
        device=ctx.device_id,
        matches=result["matches"],
        match_count=result["match_count"],
    )


async def capture_screen(ctx: ToolContext, path: str = "", read: bool = True) -> Any:
    """Capture the screen, optionally reading the text from it."""
    if read:
        result = read_screen(path)
        if not result.get("ok"):
            return fail("capture_screen", result.get("error", "capture failed"), device=ctx.device_id)
        return ok(
            "capture_screen",
            result["summary"],
            device=ctx.device_id,
            path=result["path"],
            text=result.get("text", ""),
            measured=result.get("measured"),
            understands_content=result["understands_content"],
            caveat=result["caveat"],
        )

    shot = capture(path)
    if not shot.get("ok"):
        return fail("capture_screen", shot.get("error", "capture failed"), device=ctx.device_id)
    return ok(
        "capture_screen", f"Captured {shot['path']}.", device=ctx.device_id, path=shot["path"]
    )


async def compare_images(
    ctx: ToolContext, first: str = "", second: str = "", threshold: int = 24
) -> Any:
    """Compare two images and report where they differ."""
    if not first or not second:
        return fail("compare_images", "I need two images to compare.", device=ctx.device_id)

    result = compare(first, second, threshold=max(1, min(int(threshold), 255)))
    if not result.get("ok"):
        return fail("compare_images", result.get("error", "I could not compare those."), device=ctx.device_id)

    return ok(
        "compare_images",
        result["summary"],
        device=ctx.device_id,
        changed=result["changed"],
        changed_percent=result["changed_percent"],
        region=result["region"],
    )


async def vision_capabilities(ctx: ToolContext) -> Any:
    """Report what vision JARVIS actually has, and what it does not."""
    info = describe_providers(ctx.settings)
    active = info["active"]
    understands = info["understands_content"]

    lines = [
        f"Active backend: {active}.",
        f"OCR available: {'yes' if info.get('ocr') else 'no'}.",
    ]
    ocr_state = info.get("ocr_state") or {}
    if ocr_state.get("present") and not ocr_state.get("responsive", True):
        lines.append(
            "OCR is installed but the macOS recogniser is not answering right now "
            f"({ocr_state.get('reason', 'temporarily stalled')}). Text reading will "
            "report that plainly rather than guessing."
        )
    if understands:
        lines.append("A vision model is configured, so I can describe content.")
    else:
        lines.append(
            "No vision model is configured. I can measure images and read text, "
            "but I cannot describe what is depicted."
        )
    lines.append("Vision never acts on what it sees: proposals only.")

    return ok("vision_capabilities", "\n".join(lines), device=ctx.device_id, vision=info)


SPECS = [
    (
        ToolSpec(
            name="inspect_image",
            description="Report an image's measurable properties.",
            agent="vision",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"path": "string"},
            required_parameters=["path"],
            tags=["vision", "read"],
        ),
        inspect_image,
    ),
    (
        ToolSpec(
            name="read_image_text",
            description="Recognise text in an image (OCR).",
            agent="vision",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"path": "string"},
            required_parameters=["path"],
            tags=["vision", "ocr", "read"],
        ),
        read_image_text,
    ),
    (
        ToolSpec(
            name="find_image_text",
            description="Locate text in an image, with bounding boxes.",
            agent="vision",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"path": "string", "query": "string"},
            required_parameters=["path", "query"],
            tags=["vision", "ocr", "read"],
        ),
        find_image_text,
    ),
    (
        ToolSpec(
            name="capture_screen",
            description="Capture the screen and read text from it.",
            agent="vision",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"path": "string", "read": "bool"},
            tags=["vision", "screen", "read"],
        ),
        capture_screen,
    ),
    (
        ToolSpec(
            name="compare_images",
            description="Compare two images and report where they differ.",
            agent="vision",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"first": "string", "second": "string", "threshold": "int"},
            required_parameters=["first", "second"],
            tags=["vision", "read"],
        ),
        compare_images,
    ),
    (
        ToolSpec(
            name="vision_capabilities",
            description="Report available vision capabilities.",
            agent="vision",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["vision", "read"],
        ),
        vision_capabilities,
    ),
]
