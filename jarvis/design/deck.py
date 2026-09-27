"""Presentation generator (v1.2).

Produces a **real, self-contained HTML deck** with keyboard navigation and no
external requests. Slide titles come from the request; the talking points are
placeholders, and the deck says so.
"""

from __future__ import annotations

from typing import Any, Dict
from xml.sax.saxutils import escape

FONT_STACK = "ui-sans-serif, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"

DEFAULT_SLIDES = ["Context", "Approach", "Results", "Next steps"]


def presentation(title: str, slides: str = "", theme: str = "light") -> Dict[str, Any]:
    """Build a self-contained HTML slide deck."""
    if not title:
        return {"ok": False, "error": "I need a title for the deck."}

    names = [s.strip() for s in (slides or "").split(",") if s.strip()] or list(DEFAULT_SLIDES)

    sections = []
    for index, name in enumerate(names, start=2):
        sections.append(
            '    <section class="slide">\n'
            f'      <h2>{escape(name)}</h2>\n'
            '      <ul>\n'
            '        <li>Key point one</li>\n'
            '        <li>Key point two</li>\n'
            '      </ul>\n'
            f'      <span class="num">{index} / {len(names) + 1}</span>\n'
            '    </section>'
        )

    dark = theme.lower() == "dark"
    background = "#111418" if dark else "#ffffff"
    foreground = "#f0f2f5" if dark else "#1f2328"
    muted = "#9aa1ab" if dark else "#5b6169"

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
  :root {{ color-scheme: {scheme}; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: {font}; background: {bg}; color: {fg}; }}
  .slide {{ display: none; min-height: 100vh; padding: 8vh 8vw; }}
  .slide.active {{ display: flex; flex-direction: column; justify-content: center; }}
  h1 {{ font-size: 40px; margin: 0 0 12px; }}
  h2 {{ font-size: 30px; margin: 0 0 18px; }}
  ul {{ font-size: 20px; line-height: 1.7; color: {muted}; }}
  .num {{ margin-top: 32px; font-size: 13px; color: {muted}; }}
  .hint {{ position: fixed; bottom: 14px; right: 18px; font-size: 12px; color: {muted}; }}
</style>
</head>
<body>
  <section class="slide active">
    <h1>{title}</h1>
    <ul><li>Generated outline — fill in the talking points.</li></ul>
    <span class="num">1 / {total}</span>
  </section>
{sections}
  <div class="hint">Use arrow keys or Page Up/Down to move between slides.</div>
<script>
  var slides = document.querySelectorAll('.slide');
  var index = 0;
  function show(n) {{
    slides[index].classList.remove('active');
    index = Math.max(0, Math.min(slides.length - 1, n));
    slides[index].classList.add('active');
  }}
  document.addEventListener('keydown', function (event) {{
    if (event.key === 'ArrowRight' || event.key === 'PageDown') {{ show(index + 1); }}
    if (event.key === 'ArrowLeft' || event.key === 'PageUp') {{ show(index - 1); }}
  }});
</script>
</body>
</html>
""".format(
        title=escape(title),
        scheme="dark" if dark else "light",
        font=FONT_STACK,
        bg=background,
        fg=foreground,
        muted=muted,
        total=len(names) + 1,
        sections="\n".join(sections),
    )

    return {
        "ok": True,
        "html": html,
        "slides": names,
        "problems": [],
    }
