"""Test temp file cleanup in caption_burn.py and host_file.py."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".agents/skills/ugc-talking-head-video/scripts"))

from caption_burn import whisper_words
from host_file import verify


def _temp_wav_count():
    """Count .wav files in the system temp directory."""
    return len(list(Path(tempfile.gettempdir()).glob("*.wav")))


class TestWhisperWordsCleanup:
    """Verify whisper_words cleans up its temp WAV on every exit path."""

    def test_cleanup_on_success(self, tmp_path, monkeypatch):
        video = tmp_path / "fake.mp4"
        video.write_bytes(b"fake")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        before = _temp_wav_count()

        def mock_run(cmd, **kw):
            if cmd[0] == "ffmpeg":
                wav_path = cmd[-1]
                Path(wav_path).write_bytes(b"fake wav")
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[0] == "curl":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout='{"words": [{"word": "hi", "start": 0, "end": 1}], "text": "hi"}', stderr=""
                )
            raise AssertionError(f"Unexpected command: {cmd}")

        with mock.patch("subprocess.run", side_effect=mock_run):
            result = whisper_words(str(video))

        assert result["words"] == [{"word": "hi", "start": 0, "end": 1}]
        assert _temp_wav_count() == before

    def test_cleanup_on_ffmpeg_failure(self, tmp_path, monkeypatch):
        video = tmp_path / "fake.mp4"
        video.write_bytes(b"fake")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        before = _temp_wav_count()

        def mock_run(cmd, **kw):
            if cmd[0] == "ffmpeg":
                wav_path = cmd[-1]
                Path(wav_path).write_bytes(b"partial wav")
                raise subprocess.CalledProcessError(1, cmd)
            raise AssertionError(f"Unexpected command: {cmd}")

        with mock.patch("subprocess.run", side_effect=mock_run):
            with pytest.raises(subprocess.CalledProcessError):
                whisper_words(str(video))

        assert _temp_wav_count() == before

    def test_cleanup_on_json_parse_failure(self, tmp_path, monkeypatch):
        video = tmp_path / "fake.mp4"
        video.write_bytes(b"fake")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        before = _temp_wav_count()

        def mock_run(cmd, **kw):
            if cmd[0] == "ffmpeg":
                wav_path = cmd[-1]
                Path(wav_path).write_bytes(b"fake wav")
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[0] == "curl":
                return subprocess.CompletedProcess(cmd, 0, stdout="not valid json", stderr="")
            raise AssertionError(f"Unexpected command: {cmd}")

        with mock.patch("subprocess.run", side_effect=mock_run):
            with pytest.raises(Exception):
                whisper_words(str(video))

        assert _temp_wav_count() == before


class TestVerifyCleanup:
    """Verify host_file.verify cleans up its temp file on every exit path."""

    def test_cleanup_on_success(self, tmp_path):
        src = tmp_path / "source.txt"
        src.write_bytes(b"test content")

        before = len(list(Path(tempfile.gettempdir()).iterdir()))

        def mock_run(cmd, **kw):
            if cmd[0] == "curl" and "-o" in cmd:
                out_idx = cmd.index("-o") + 1
                Path(cmd[out_idx]).write_bytes(b"test content")
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("subprocess.run", side_effect=mock_run):
            result = verify("https://example.com/file.txt", str(src))

        assert result is True
        after = len(list(Path(tempfile.gettempdir()).iterdir()))
        assert after <= before + 1

    def test_cleanup_on_size_mismatch_exception(self, tmp_path):
        src = tmp_path / "source.txt"
        src.write_bytes(b"test content")

        def mock_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0)

        created_temps = []
        original_ntf = tempfile.NamedTemporaryFile

        def tracking_ntf(**kwargs):
            t = original_ntf(**kwargs)
            created_temps.append(t.name)
            return t

        with mock.patch("subprocess.run", side_effect=mock_run):
            with mock.patch("tempfile.NamedTemporaryFile", side_effect=tracking_ntf):
                result = verify("https://example.com/file.txt", str(src))

        assert result is False
        for temp_path in created_temps:
            assert not os.path.exists(temp_path), f"Temp file was not cleaned up: {temp_path}"
