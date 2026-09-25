"""The `caption_burn.py` render command (the `ugc-talking-head-video` skill's caption step).

Two constraints here each broke the step outright on a real machine, and neither is visible by
reading the skill. Both are pinned as tests so they cannot regress silently:

- **FFmpeg 9 removed `-filter_complex_script`.** Every burn died with "Unrecognized option" before
  a frame was encoded. The replacement the release notes point at, `-/filter_complex`, is absent
  from `ffmpeg -h full`, so no version or capability probe can find it by inspection — the graph is
  passed inline instead, which every version accepts.
- **A silent take has no audio stream.** The skill's own silent mode emits a video-only file, and
  the render mapped `0:a` unconditionally, so captions could not be burned onto exactly the file
  that mode produces: ffmpeg aborted with `Stream map '' matches no streams`.

The module needs Pillow to draw overlays and ffmpeg to render, neither of which this suite has, so
these tests exercise the pure command builder and the audio probe. They import the script by path
because it is a standalone skill asset, not part of the `treg` package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).parents[1] / ".agents" / "skills" / "ugc-talking-head-video"
          / "scripts" / "caption_burn.py")


def _load():
    spec = importlib.util.spec_from_file_location("caption_burn_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def burn():
    return _load()


def _cmd(burn, *, audio):
    return burn.ffmpeg_command("take.mp4", ["-i", "header.png", "-i", "c000.png"],
                               "[0:v][1:v]overlay=(W-w)/2:153[v0]", 1, "out.mp4", audio=audio)


def test_graph_is_passed_inline_not_via_a_script_file(burn):
    """`-filter_complex_script` is gone in FFmpeg 9; inline is the portable form."""
    args = _cmd(burn, audio=True)
    assert "-filter_complex" in args
    assert "-filter_complex_script" not in args
    assert "-/filter_complex" not in args
    # the graph itself travels as the very next argument, not as a path to a temp file
    assert args[args.index("-filter_complex") + 1].startswith("[0:v]")


def test_audio_is_not_mapped_for_a_silent_take(burn):
    """The silent mode's video-only output must still be captionable."""
    args = _cmd(burn, audio=False)
    assert "0:a" not in args
    assert "-c:a" not in args


def test_audio_is_copied_when_the_take_has_a_stream(burn):
    """A normal take keeps its audio untouched — re-encoding it would change the voice."""
    args = _cmd(burn, audio=True)
    assert args[args.index("-map", args.index("-map") + 1) + 1] == "0:a"
    assert args[args.index("-c:a") + 1] == "copy"


def test_output_maps_the_final_chained_overlay(burn):
    """The captions chain v0 -> vN; mapping anything but the last link drops captions."""
    args = burn.ffmpeg_command("take.mp4", ["-i", "header.png"], "g", 4, "out.mp4", audio=True)
    assert args[args.index("-map") + 1] == "[v4]"


def test_has_audio_reads_ffprobe_output(burn, monkeypatch):
    """Silent-take detection drives the conditional map, so it is worth pinning both answers."""
    class Result:
        def __init__(self, stdout): self.stdout = stdout

    monkeypatch.setattr(burn.subprocess, "run", lambda *a, **k: Result("0\n"))
    assert burn.has_audio("silent.mp4") is True

    monkeypatch.setattr(burn.subprocess, "run", lambda *a, **k: Result(""))
    assert burn.has_audio("silent.mp4") is False
