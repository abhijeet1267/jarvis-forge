"""Vision Agent tests (v1.3).

Two properties matter most:

1. **OCR and measurement are real.** Text returned from an image was actually
   recognised by the macOS Vision framework, not invented.
2. **Vision can never act.** The plan requires ``SEE → UNDERSTAND → PROPOSE →
   PERMISSION → ACT`` and forbids acting merely because something was seen. That
   is enforced by giving the agent no action tools at all, asserted here
   structurally.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarvis.core.models import PlannedAction, PolicyDecision, RiskLevel
from jarvis.tools.base import ToolContext
from jarvis.tools.vision import (
    ACTION_TOOLS,
    capture_screen,
    compare_images,
    find_image_text,
    inspect_image,
    read_image_text,
    vision_capabilities,
)
from jarvis.vision.diff import available as diff_available
from jarvis.vision.diff import compare
from jarvis.vision.image import available as pil_available
from jarvis.vision.image import inspect
from jarvis.vision.ocr import available as ocr_available
from jarvis.vision.ocr import find_text, read_text
from jarvis.vision.providers import active_provider, describe as describe_providers


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext()


@pytest.fixture()
def sample_image(tmp_area: Path) -> Path:
    """An image containing known text."""
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    path = tmp_area / "sample.png"
    image = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 40), "Project AMR status report", fill="black")
    draw.text((30, 90), "Battery 74 percent charging", fill="black")
    image.save(path)
    return path


# ---------------------------------------------------------------------------
# Image measurement
# ---------------------------------------------------------------------------
def test_inspection_reports_real_measurements(sample_image: Path) -> None:
    pytest.importorskip("PIL")
    result = inspect(str(sample_image))

    assert result["ok"]
    assert result["width"] == 600
    assert result["height"] == 200
    assert result["format"] == "PNG"
    # A white page with black text: bright, and not flat.
    assert result["brightness"] > 200
    assert result["contrast"] > 0
    assert result["dominant_colors"]


def test_inspection_of_a_missing_file_is_honest() -> None:
    result = inspect("/tmp/definitely-not-here.png")
    assert not result["ok"]
    assert "could not find" in result["error"]


def test_inspection_rejects_a_non_image(tmp_area: Path) -> None:
    text_file = tmp_area / "notes.txt"
    text_file.write_text("not an image", encoding="utf-8")

    result = inspect(str(text_file))
    assert not result["ok"]


def test_pillow_availability_is_reported() -> None:
    assert isinstance(pil_available(), bool)


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_ocr_reads_real_text(sample_image: Path) -> None:
    """The recognised text must match what was actually drawn."""
    if not ocr_available():
        pytest.skip("OCR helper unavailable")

    result = read_text(str(sample_image))
    assert result["ok"]
    assert result["backend"] == "macos_vision"

    combined = " ".join(line["text"] for line in result["lines"]).lower()
    assert "amr" in combined, combined
    assert "battery" in combined, combined


@pytest.mark.slow
def test_ocr_returns_bounding_boxes(sample_image: Path) -> None:
    if not ocr_available():
        pytest.skip("OCR helper unavailable")

    result = read_text(str(sample_image))
    for line in result["lines"]:
        assert 0.0 <= line["x"] <= 1.0
        assert 0.0 <= line["y"] <= 1.0
        assert line["width"] > 0


@pytest.mark.slow
def test_find_text_locates_a_word(sample_image: Path) -> None:
    if not ocr_available():
        pytest.skip("OCR helper unavailable")

    result = find_text(str(sample_image), "battery")
    assert result["ok"]
    assert result["match_count"] >= 1
    assert "battery" in result["matches"][0]["text"].lower()


def test_ocr_on_a_missing_file_is_honest() -> None:
    result = read_text("/tmp/definitely-not-here.png")
    assert not result["ok"]
    assert "could not find" in result["error"]


def test_find_text_needs_a_query(sample_image: Path) -> None:
    result = find_text(str(sample_image), "")
    assert not result["ok"]


def test_ocr_availability_is_a_boolean() -> None:
    assert isinstance(ocr_available(), bool)


# ---------------------------------------------------------------------------
# Image diff
# ---------------------------------------------------------------------------
def test_identical_images_report_no_change(sample_image: Path) -> None:
    pytest.importorskip("PIL")
    result = compare(str(sample_image), str(sample_image))

    assert result["ok"]
    assert result["changed"] is False
    assert result["changed_percent"] == 0.0


def test_a_modified_image_reports_change(tmp_area: Path, sample_image: Path) -> None:
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    modified = tmp_area / "modified.png"
    with Image.open(sample_image) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle([400, 20, 580, 80], fill="red")
        image.save(modified)

    result = compare(str(sample_image), str(modified))
    assert result["ok"]
    assert result["changed"] is True
    assert result["changed_percent"] > 0
    assert result["region"] is not None, "the changed region should be located"


def test_diff_of_a_missing_file_is_honest() -> None:
    result = compare("/tmp/a-nope.png", "/tmp/b-nope.png")
    assert not result["ok"]


def test_diff_availablity_is_reported() -> None:
    assert isinstance(diff_available(), bool)


# ---------------------------------------------------------------------------
# The structural guarantee: vision cannot act
# ---------------------------------------------------------------------------
def test_the_vision_agent_has_no_action_tools(core) -> None:
    """SEE -> UNDERSTAND -> PROPOSE -> PERMISSION -> ACT.

    Vision stops at PROPOSE. There is no click, type, move or delete tool in
    the vision agent, so it cannot act on what it sees even in principle.
    """
    agent = core.agents["vision"]
    owned = agent.capabilities()

    forbidden = {
        "click", "type", "press", "keyboard", "mouse", "move_file", "delete_file",
        "run_allowlisted_command", "open_application", "close_app", "send_email",
        "apply_code_change", "commit_workspace",
    }
    assert owned & forbidden == set(), owned & forbidden
    assert ACTION_TOOLS == []


def test_every_vision_tool_is_read_only(core) -> None:
    """All vision tools are tier 0 -- no confirmation needed, nothing modified."""
    specs = [s for s in core.tools.all() if s.agent == "vision"]
    assert specs, "the vision agent must own tools"
    for spec in specs:
        assert spec.risk is RiskLevel.READ, spec.name
        assert spec.requires_confirmation is False, spec.name


def test_no_vision_tool_is_destructive_or_external(core) -> None:
    specs = [s for s in core.tools.all() if s.agent == "vision"]
    for spec in specs:
        assert spec.risk not in (RiskLevel.DESTRUCTIVE, RiskLevel.PHYSICAL), spec.name


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def test_active_provider_reports_whether_it_understands_content() -> None:
    provider = active_provider()
    assert provider.name
    # With no vision model configured this must be the local measurement backend.
    assert isinstance(provider.understands_content, bool)


def test_provider_description_reports_ocr_state() -> None:
    info = describe_providers()
    assert "active" in info
    assert "ocr" in info
    assert "providers" in info


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_inspect_image_tool(ctx: ToolContext, sample_image: Path) -> None:
    pytest.importorskip("PIL")
    result = await inspect_image(ctx, path=str(sample_image))
    assert result.ok
    assert "600x200" in result.message


@pytest.mark.asyncio
async def test_inspect_image_needs_a_path(ctx: ToolContext) -> None:
    assert not (await inspect_image(ctx)).ok


@pytest.mark.asyncio
async def test_read_image_text_tool(ctx: ToolContext, sample_image: Path) -> None:
    if not ocr_available():
        pytest.skip("OCR helper unavailable")

    result = await read_image_text(ctx, path=str(sample_image))
    assert result.ok
    assert "amr" in result.output["text"].lower()


@pytest.mark.asyncio
async def test_read_image_text_needs_a_path(ctx: ToolContext) -> None:
    assert not (await read_image_text(ctx)).ok


@pytest.mark.asyncio
async def test_find_image_text_tool(ctx: ToolContext, sample_image: Path) -> None:
    if not ocr_available():
        pytest.skip("OCR helper unavailable")

    result = await find_image_text(ctx, path=str(sample_image), query="battery")
    assert result.ok
    assert result.output["match_count"] >= 1


@pytest.mark.asyncio
async def test_find_image_text_needs_a_query(ctx: ToolContext, sample_image: Path) -> None:
    assert not (await find_image_text(ctx, path=str(sample_image))).ok


@pytest.mark.asyncio
async def test_compare_images_tool(ctx: ToolContext, sample_image: Path) -> None:
    pytest.importorskip("PIL")
    result = await compare_images(ctx, first=str(sample_image), second=str(sample_image))
    assert result.ok
    assert result.output["changed"] is False


@pytest.mark.asyncio
async def test_compare_images_needs_two_paths(ctx: ToolContext, sample_image: Path) -> None:
    assert not (await compare_images(ctx, first=str(sample_image))).ok


@pytest.mark.asyncio
async def test_capabilities_tool_states_the_caveat(ctx: ToolContext) -> None:
    result = await vision_capabilities(ctx)
    assert result.ok
    assert "never acts" in result.message
    assert "understands_content" in result.output["vision"]


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tool,params",
    [
        ("inspect_image", {"path": "/tmp/a.png"}),
        ("read_image_text", {"path": "/tmp/a.png"}),
        ("find_image_text", {"path": "/tmp/a.png", "query": "x"}),
        ("capture_screen", {}),
        ("compare_images", {"first": "/tmp/a.png", "second": "/tmp/b.png"}),
        ("vision_capabilities", {}),
    ],
)
def test_vision_tools_execute_without_confirmation(core, tool: str, params: dict) -> None:
    action = PlannedAction(tool=tool, params=params, device="laptop_main")
    assert core.policy.evaluate(action).decision is PolicyDecision.EXECUTE


def test_vision_agent_is_registered(core) -> None:
    assert "vision" in core.agents
    assert core.agents["vision"].owns("read_image_text")


# ---------------------------------------------------------------------------
# OCR latency: a stalled recogniser must degrade, not stall every question
#
# Measured on this machine: the same image returns in ~0.6 s standalone and
# exceeds any bound when other work is running. Both runs are honest, so the
# code must (a) not wait a minute for it, (b) say plainly that nothing was read,
# and (c) not make every later question pay the wait again.
# ---------------------------------------------------------------------------

class _TimeoutRun:
    """A subprocess.run stand-in that never answers."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, args, **kwargs):
        self.calls += 1
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 20.0))


def _stalled_backend(monkeypatch, runner):
    from jarvis.vision import ocr as ocr_module

    backend = ocr_module.OCRBackend()
    backend._binary = Path("/usr/bin/true")      # the helper is present...
    monkeypatch.setattr(ocr_module.subprocess, "run", runner)
    monkeypatch.setattr(ocr_module, "_COOLDOWN_SECONDS", 5.0)
    return backend


def test_a_timed_out_read_reports_that_nothing_was_read(
    monkeypatch, sample_image: Path
) -> None:
    runner = _TimeoutRun()
    backend = _stalled_backend(monkeypatch, runner)
    result = backend.read_text(str(sample_image))

    assert result["ok"] is False
    assert "did not answer" in result["error"]
    assert "no text was read" in result["error"].lower()
    assert "text" not in result, "a stalled recogniser must not produce text"
    assert result["timed_out"] is True


def test_a_stalled_recogniser_is_left_alone_rather_than_re_asked(
    monkeypatch, sample_image: Path
) -> None:
    runner = _TimeoutRun()
    backend = _stalled_backend(monkeypatch, runner)

    backend.read_text(str(sample_image))          # first try times out
    assert runner.calls == 1
    assert backend.cooling_down() > 0

    second = backend.read_text(str(sample_image))  # second must not wait again
    assert runner.calls == 1, "the cooldown should short-circuit the second read"
    assert "not queueing more work" in second["error"]
    assert second["degraded"] is True


def test_the_recogniser_is_asked_again_once_the_cooldown_passes(
    monkeypatch, sample_image: Path
) -> None:
    runner = _TimeoutRun()
    backend = _stalled_backend(monkeypatch, runner)
    backend.read_text(str(sample_image))
    backend._cooldown_until = 0.0                 # time passes

    backend.read_text(str(sample_image))
    assert runner.calls == 2


def test_present_and_answering_are_different_claims(
    monkeypatch, sample_image: Path
) -> None:
    """available() is about the binary; responsive() is about asking it now."""
    backend = _stalled_backend(monkeypatch, _TimeoutRun())
    assert backend.available() is True
    assert backend.responsive() is True

    backend.read_text(str(sample_image))
    assert backend.available() is True, "the helper did not vanish, it stalled"
    assert backend.responsive() is False

    state = backend.state()
    assert state["present"] is True and state["responsive"] is False
    assert state["cooling_down_seconds"] > 0 and "stalled" not in state["reason"]


def test_a_successful_read_clears_the_stall(monkeypatch, sample_image: Path) -> None:
    from jarvis.vision import ocr as ocr_module

    runner = _TimeoutRun()
    backend = _stalled_backend(monkeypatch, runner)
    backend.read_text(str(sample_image))
    assert backend.cooling_down() > 0
    backend._cooldown_until = 0.0                 # the cooldown has passed

    class _GoodRun:
        def __call__(self, args, **kwargs):
            return subprocess.CompletedProcess(
                args, 0, stdout='{"lines": [{"text": "HELLO", "confidence": 0.9}]}', stderr=""
            )

    monkeypatch.setattr(ocr_module.subprocess, "run", _GoodRun())
    result = backend.read_text(str(sample_image))
    assert result["ok"] is True and result["text"] == "HELLO"
    assert backend.cooling_down() == 0.0
    assert backend.state()["responsive"] is True


@pytest.mark.parametrize(
    "raw,expected",
    [("5", 5.0), ("", 20.0), ("nonsense", 20.0), ("0.1", 20.0), ("900", 20.0), ("37.5", 37.5)],
)
def test_the_read_timeout_is_configurable_and_clamped(
    monkeypatch, raw: str, expected: float
) -> None:
    from jarvis.vision.ocr import _configured_timeout

    monkeypatch.setenv("JARVIS_OCR_TIMEOUT", raw)
    assert _configured_timeout() == expected


async def test_capabilities_disclose_a_stalled_recogniser(monkeypatch) -> None:
    """'OCR available: yes' must not mean 'OCR will answer you right now'."""
    from jarvis.tools import vision as vision_tools

    def _stalled(settings=None):
        info = {
            "active": "local_measurement",
            "understands_content": False,
            "ocr": True,
            "ocr_state": {
                "present": True, "responsive": False, "cooling_down_seconds": 12.0,
                "reason": "the recogniser did not answer within 20s and is left alone for another 12s",
            },
            "providers": [],
        }
        return info

    monkeypatch.setattr(vision_tools, "describe_providers", _stalled)
    result = await vision_capabilities(ToolContext())
    assert "not answering right now" in result.message
    assert "rather than guessing" in result.message


def test_provider_posture_separates_present_from_responsive() -> None:
    info = describe_providers()
    assert "ocr_state" in info
    assert set(info["ocr_state"]) >= {"present", "responsive", "timeout_seconds"}
