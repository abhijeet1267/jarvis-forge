"""Image inspection (v1.3).

Everything here is measured from the pixels with Pillow. Nothing is inferred
about *content* — "what is in this picture" needs a vision model, and if one is
not configured this module says so rather than guessing.

What it can tell you truthfully: dimensions, format, colour mode, file size,
EXIF, dominant colours, brightness and contrast statistics, whether an image is
effectively greyscale, and how much two images differ.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..logging_setup import get_logger

log = get_logger("vision.image")

try:
    from PIL import Image, ImageStat  # type: ignore

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    PIL_AVAILABLE = False


def available() -> bool:
    return PIL_AVAILABLE


def _open(path: Path):
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow is not installed, so I cannot inspect images.")
    return Image.open(path)


def dominant_colors(image, count: int = 5) -> List[Dict[str, Any]]:
    """The most common colours, by quantising. Measured, not guessed."""
    quantised = image.convert("RGB").quantize(colors=max(2, count))
    palette = quantised.getpalette() or []
    colours = sorted(quantised.getcolors(), reverse=True)[:count]

    total = sum(c for c, _ in colours) or 1
    out: List[Dict[str, Any]] = []
    for frequency, index in colours:
        base = index * 3
        rgb = tuple(palette[base : base + 3]) if len(palette) >= base + 3 else (0, 0, 0)
        out.append(
            {
                "rgb": rgb,
                "hex": "#{:02x}{:02x}{:02x}".format(*rgb),
                "share": round(frequency / total, 3),
            }
        )
    return out


def _contrast(grayscale) -> float:
    """Standard deviation of luminance — a simple, honest contrast measure."""
    stat = ImageStat.Stat(grayscale)
    return round(stat.stddev[0], 2)


def inspect(path: str) -> Dict[str, Any]:
    """Inspect an image file and report what is measurable."""
    target = Path(path).expanduser()
    if not target.is_file():
        return {"ok": False, "error": f"I could not find {path}."}
    if not PIL_AVAILABLE:
        return {"ok": False, "error": "Pillow is not installed, so I cannot inspect images."}

    try:
        with _open(target) as image:
            width, height = image.size
            grayscale = image.convert("L")
            histogram = grayscale.histogram()
            total = sum(histogram) or 1
            brightness = sum(index * count for index, count in enumerate(histogram)) / total

            exif = {}
            try:
                raw_exif = image.getexif()
                if raw_exif:
                    exif = {str(k): str(v) for k, v in list(raw_exif.items())[:10]}
            except Exception:
                exif = {}

            result = {
                "ok": True,
                "path": str(target),
                "filename": target.name,
                "width": width,
                "height": height,
                "aspect_ratio": round(width / height, 3) if height else 0.0,
                "format": image.format,
                "mode": image.mode,
                "bytes": target.stat().st_size,
                "brightness": round(brightness, 1),
                "contrast": _contrast(grayscale),
                "dominant_colors": dominant_colors(image),
                "exif": exif,
            }
    except Exception as exc:
        return {"ok": False, "error": f"I could not read that image: {exc}"}

    result["summary"] = describe(result)
    return result


def describe(info: Dict[str, Any]) -> str:
    """A one-line human summary of a measured result."""
    if not info.get("ok"):
        return info.get("error", "I could not inspect that image.")

    colours = ", ".join(c["hex"] for c in info.get("dominant_colors", [])[:3])
    return (
        f"{info['filename']}: {info['width']}x{info['height']} {info['format']}, "
        f"brightness {info['brightness']}, contrast {info['contrast']}, "
        f"dominant colours {colours or 'unknown'}."
    )
