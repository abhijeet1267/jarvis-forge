"""Image comparison (v1.3).

Answers "did this change, and where?" — genuinely useful for watching a screen
or a render between runs.

It reports the **fraction of pixels that changed** and the bounding box of the
changed region. It does *not* say what changed: describing content needs a
vision model, and without one JARVIS says so rather than speculating.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..logging_setup import get_logger

log = get_logger("vision.diff")

try:
    from PIL import Image, ImageChops  # type: ignore

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False


def available() -> bool:
    return PIL_AVAILABLE


def compare(
    first: str,
    second: str,
    *,
    threshold: int = 24,
) -> Dict[str, Any]:
    """Compare two images.

    ``threshold`` is the per-channel difference below which a pixel counts as
    unchanged, which keeps JPEG/anti-aliasing noise from looking like a change.
    """
    if not PIL_AVAILABLE:
        return {"ok": False, "error": "Pillow is not installed, so I cannot compare images."}

    a_path = Path(first).expanduser()
    b_path = Path(second).expanduser()
    for label, path in (("first", a_path), ("second", b_path)):
        if not path.is_file():
            return {"ok": False, "error": f"I could not find the {label} image ({path})."}

    try:
        with Image.open(a_path) as a_image, Image.open(b_path) as b_image:
            a = a_image.convert("RGB")
            b = b_image.convert("RGB")

            if a.size != b.size:
                # Compare on the larger canvas; note that sizes differed.
                size = (max(a.width, b.width), max(a.height, b.height))
                a = a.resize(size)
                b = b.resize(size)
                same_size = False
            else:
                same_size = True

            delta = ImageChops.difference(a, b)
            # Collapse channels: a pixel differs if any channel exceeds threshold.
            mask = delta.convert("L").point(
                lambda value: 255 if value >= threshold else 0
            )

            total = mask.width * mask.height
            changed_pixels = sum(
                1 for value in mask.getdata() if value > 0
            )
            bbox: Optional[Tuple[int, int, int, int]] = mask.getbbox()

    except Exception as exc:
        return {"ok": False, "error": f"I could not compare those images: {exc}"}

    fraction = changed_pixels / total if total else 0.0
    result = {
        "ok": True,
        "same_size": same_size,
        "changed": changed_pixels > 0,
        "changed_pixels": changed_pixels,
        "total_pixels": total,
        "changed_fraction": round(fraction, 4),
        "changed_percent": round(fraction * 100, 2),
        "region": (
            {"x": bbox[0], "y": bbox[1], "width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1]}
            if bbox else None
        ),
    }

    if not result["changed"]:
        result["summary"] = "The two images look identical at this sensitivity."
    else:
        where = ""
        if result["region"]:
            region = result["region"]
            where = (
                f", in a region at ({region['x']}, {region['y']}) "
                f"of {region['width']}x{region['height']}"
            )
        result["summary"] = (
            f"{result['changed_percent']}% of pixels differ{where}. "
            "I can see that it changed, not what it means."
        )

    return result
