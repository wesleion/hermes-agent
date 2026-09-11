"""Contract tests for the isolated, local-only audio perception helper."""
from __future__ import annotations

import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))


def _wav(path: Path, *, normalized: bool = True) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000 if normalized else 8_000)
        handle.writeframes(b"\x00\x00" * 800)


def _config(tmp_path: Path) -> dict[str, object]:
    stt = tmp_path / "stt"
    stt.mkdir()
    return {
        "stt_model_path": str(stt),
        "ffmpeg_binary": "/private/ffmpeg",
        "ffprobe_binary": "/private/ffprobe",
        "max_threads": 99,
    }


def test_unconfigured_is_fail_closed(tmp_path: Path) -> None:
    from whatsapp_ops_local_perception import LocalMediaPerception

    audio = tmp_path / "sound.wav"
    _wav(audio)
    result = LocalMediaPerception({}).transcribe_audio(str(audio))
    assert result == {
        "ok": False, "text": "", "language": "", "duration_seconds": 0.0,
        "truncated": False, "error": "not_configured",
    }


def test_only_normalized_regular_wav_is_a_candidate(tmp_path: Path) -> None:
    from whatsapp_ops_local_perception import LocalMediaPerception

    perception = LocalMediaPerception(_config(tmp_path))
    invalid = tmp_path / "invalid.wav"
    invalid.write_text("not wav", encoding="utf-8")
    assert perception.transcribe_audio(str(invalid))["error"] == "invalid_audio_input"
    wrong_rate = tmp_path / "wrong-rate.wav"
    _wav(wrong_rate, normalized=False)
    assert perception.transcribe_audio(str(wrong_rate))["error"] == "invalid_audio_input"
    assert perception.transcribe_audio(str(tmp_path / "missing.wav"))["error"] == "invalid_audio_input"


def test_model_failure_is_fail_closed_without_path_disclosure(tmp_path: Path) -> None:
    from whatsapp_ops_local_perception import LocalMediaPerception

    audio = tmp_path / "fixture.wav"
    _wav(audio)
    result = LocalMediaPerception(_config(tmp_path)).transcribe_audio(str(audio))
    assert result["ok"] is False
    assert result["error"] == "stt_failed"
    assert str(tmp_path) not in repr(result)


def test_image_api_is_deliberately_not_part_of_local_helper() -> None:
    from whatsapp_ops_local_perception import LocalMediaPerception

    assert not hasattr(LocalMediaPerception, "describe_images")
