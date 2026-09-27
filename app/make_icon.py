#!/usr/bin/env python3
"""Generate the JARVIS Forge app icon.

Renders a macOS-style rounded square ("squircle") with an arc-reactor motif,
at every size macOS expects, then hands the iconset to ``iconutil``.

Run standalone:  python app/make_icon.py [output.icns]
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# Artwork is drawn at 4x and downsampled -- cheap, and gives clean edges.
SUPERSAMPLE = 4
BASE = 1024

# Palette: deep navy body with a cyan/blue core.
BG_OUTER = (10, 20, 42)
BG_INNER = (22, 48, 96)
RING = (56, 189, 248)
RING_DIM = (37, 99, 175)
CORE = (186, 240, 255)
CORE_GLOW = (56, 189, 248)

# macOS squircle: artwork fills the canvas with a small transparent margin.
MARGIN_RATIO = 0.055
CORNER_RATIO = 0.225


def rounded_mask(size: int, margin: int, radius: int) -> Image.Image:
    """An anti-aliased rounded-rectangle mask."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        [margin, margin, size - margin - 1, size - margin - 1],
        radius=radius,
        fill=255,
    )
    return mask


def vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    """A top-to-bottom linear gradient."""
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        grad.putpixel(
            (0, y),
            tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    return grad.resize((size, size), Image.BILINEAR)


def draw_artwork(size: int) -> Image.Image:
    """Draw the full icon artwork on a transparent canvas."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    margin = int(size * MARGIN_RATIO)
    radius = int(size * CORNER_RATIO)
    mask = rounded_mask(size, margin, radius)

    # --- body: radial-ish glow over a vertical gradient --------------------
    body = vertical_gradient(size, BG_INNER, BG_OUTER).convert("RGBA")

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    cx = cy = size / 2
    for step in range(28, 0, -1):
        r = size * 0.46 * (step / 28)
        alpha = int(46 * (1 - step / 28) ** 2)
        glow_draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(*CORE_GLOW, alpha),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.02))
    body = Image.alpha_composite(body, glow)
    body.putalpha(mask)
    canvas = Image.alpha_composite(canvas, body)

    # --- rings -------------------------------------------------------------
    rings = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rdraw = ImageDraw.Draw(rings)

    outer_r = size * 0.315
    ring_w = size * 0.028
    rdraw.ellipse(
        [cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r],
        outline=(*RING, 235),
        width=int(ring_w),
    )

    mid_r = size * 0.235
    rdraw.ellipse(
        [cx - mid_r, cy - mid_r, cx + mid_r, cy + mid_r],
        outline=(*RING_DIM, 200),
        width=int(size * 0.014),
    )

    # Three arc segments on the outer ring (reactor coil look).
    arc_r = size * 0.315
    arc_box = [cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r]
    for i in range(3):
        start = -90 + i * 120 + 12
        rdraw.arc(arc_box, start, start + 76, fill=(*RING, 255), width=int(size * 0.036))

    # --- core --------------------------------------------------------------
    core_r = size * 0.115
    core = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(core)
    halo = core_r * 2.1
    for step in range(18, 0, -1):
        r = halo * (step / 18)
        alpha = int(70 * (1 - step / 18) ** 1.6)
        cdraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*CORE_GLOW, alpha))
    core = core.filter(ImageFilter.GaussianBlur(size * 0.012))
    cdraw = ImageDraw.Draw(core)
    cdraw.ellipse(
        [cx - core_r, cy - core_r, cx + core_r, cy + core_r],
        fill=(*CORE, 255),
    )

    # Triangular core detail, echoing the reactor.
    tri = core_r * 0.62
    points = [
        (cx, cy - tri),
        (cx + tri * math.cos(math.radians(30)), cy + tri * math.sin(math.radians(30))),
        (cx - tri * math.cos(math.radians(30)), cy + tri * math.sin(math.radians(30))),
    ]
    cdraw.polygon(points, fill=(*CORE_GLOW, 190))

    rings = Image.alpha_composite(rings, core)
    canvas = Image.alpha_composite(canvas, rings)

    # --- top gloss ---------------------------------------------------------
    gloss = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(gloss)
    gdraw.ellipse(
        [size * 0.14, -size * 0.34, size * 0.86, size * 0.42],
        fill=(255, 255, 255, 26),
    )
    gloss = gloss.filter(ImageFilter.GaussianBlur(size * 0.035))
    gloss.putalpha(Image.composite(gloss.getchannel("A"), Image.new("L", (size, size), 0), mask))
    canvas = Image.alpha_composite(canvas, gloss)

    # Keep everything inside the squircle.
    final_mask = mask
    canvas.putalpha(Image.composite(canvas.getchannel("A"), Image.new("L", (size, size), 0), final_mask))
    return canvas


# (filename suffix, pixel size) pairs that iconutil expects.
ICONSET_SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def build_icns(destination: Path, workdir: Path) -> Path:
    """Render the iconset and convert it to .icns."""
    iconset = workdir / "JarvisForge.iconset"
    iconset.mkdir(parents=True, exist_ok=True)

    master = draw_artwork(BASE)

    for name, px in ICONSET_SIZES:
        master.resize((px, px), Image.LANCZOS).save(iconset / name)

    destination.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(destination)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"iconutil failed: {proc.stderr.strip()}")

    return destination


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("app/build/JarvisForge.icns")
    workdir = out.parent
    workdir.mkdir(parents=True, exist_ok=True)

    icns = build_icns(out, workdir)
    print(f"icon written: {icns} ({icns.stat().st_size} bytes)")

    preview = workdir / "icon_preview.png"
    draw_artwork(512).save(preview)
    print(f"preview written: {preview}")


if __name__ == "__main__":
    main()
