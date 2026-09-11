"""Offline contracts for the bounded Groq audio adapter."""

from __future__ import annotations

import json
import sys
import urllib.error
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _wav(path: Path, *, seconds: int = 1, rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\0\0" * rate * seconds)


def _config(**changes):
    value = {
        "provider": "groq",
        "model": "whisper-large-v3-turbo",
        "timeout": 30,
        "language": "",
    }
    value.update(changes)
    return value


class _Response:
    status = 200

    def __init__(self, value: bytes):
        self.value = value
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        self.read_sizes.append(size)
        return self.value


def test_transcribes_normalized_wav_with_narrow_request(tmp_path, monkeypatch):
    from tools.whatsapp_ops_groq_perception import GroqMediaPerception

    audio = tmp_path / "private-name.wav"
    _wav(audio)
    calls = []
    response = _Response(json.dumps({"text": "Olá", "language": "pt"}).encode())

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return response

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    result = GroqMediaPerception(_config(), opener=opener).transcribe_audio(str(audio))
    assert result == {
        "ok": True,
        "text": "Olá",
        "language": "pt",
        "duration_seconds": 1.0,
        "truncated": False,
    }
    assert len(calls) == 1 and calls[0][1] == 30
    request = calls[0][0]
    assert request.full_url == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert request.get_header("User-agent") == "Hunter-Audio-Eval/1"
    assert (
        b'filename="audio.wav"' in request.data
        and str(audio).encode() not in request.data
    )
    assert (
        b"whisper-large-v3-turbo" in request.data and b"response_format" in request.data
    )
    assert b'name="temperature"\r\n\r\n0\r\n' in request.data
    assert response.read_sizes == [64 * 1024 + 1]


def test_invalid_conditions_fail_before_opening_or_request(tmp_path, monkeypatch):
    from tools.whatsapp_ops_groq_perception import GroqMediaPerception

    audio = tmp_path / "sound.wav"
    _wav(audio)
    calls = []
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert (
        GroqMediaPerception(
            _config(), opener=lambda *a, **k: calls.append(1)
        ).transcribe_audio(str(audio))["error"]
        == "groq_not_configured"
    )
    monkeypatch.setenv("GROQ_API_KEY", "key")
    for config in (
        _config(model="other"),
        _config(provider="other"),
        _config(timeout=0),
        _config(language="en"),
        "bad",
    ):
        assert (
            GroqMediaPerception(
                config, opener=lambda *a, **k: calls.append(1)
            ).transcribe_audio(str(audio))["error"]
            == "groq_configuration_invalid"
        )
    wrong = tmp_path / "wrong.wav"
    _wav(wrong, rate=8_000)
    assert (
        GroqMediaPerception(
            _config(), opener=lambda *a, **k: calls.append(1)
        ).transcribe_audio(str(wrong))["error"]
        == "invalid_audio_input"
    )
    large = tmp_path / "large.wav"
    _wav(large)
    large.write_bytes(large.read_bytes() + b"x" * (20 * 1024 * 1024))
    assert (
        GroqMediaPerception(
            _config(), opener=lambda *a, **k: calls.append(1)
        ).transcribe_audio(str(large))["error"]
        == "invalid_audio_input"
    )
    link = tmp_path / "link.wav"
    link.symlink_to(audio)
    assert (
        GroqMediaPerception(
            _config(), opener=lambda *a, **k: calls.append(1)
        ).transcribe_audio(str(link))["error"]
        == "invalid_audio_input"
    )
    assert calls == []


def test_duration_and_bad_provider_responses_are_opaque(tmp_path, monkeypatch):
    from tools.whatsapp_ops_groq_perception import GroqMediaPerception

    long = tmp_path / "long.wav"
    _wav(long, seconds=301)
    monkeypatch.setenv("GROQ_API_KEY", "secret-value")
    calls = []
    assert (
        GroqMediaPerception(
            _config(), opener=lambda *a, **k: calls.append(1)
        ).transcribe_audio(str(long))["error"]
        == "audio_duration_limit"
    )
    malformed = tmp_path / "fixture.wav"
    _wav(malformed)
    result = GroqMediaPerception(
        _config(), opener=lambda *a, **k: _Response(b"not json")
    ).transcribe_audio(str(malformed))
    assert result["error"] == "groq_invalid_response"
    assert (
        str(tmp_path) not in repr(result)
        and "secret-value" not in repr(result)
        and calls == []
    )


def test_transport_failure_has_one_call_and_no_fallback(tmp_path, monkeypatch):
    from tools.whatsapp_ops_groq_perception import GroqMediaPerception

    audio = tmp_path / "sound.wav"
    _wav(audio)
    monkeypatch.setenv("GROQ_API_KEY", "key")
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("private failure")

    result = GroqMediaPerception(_config(), opener=fail).transcribe_audio(str(audio))
    assert result["error"] == "groq_transcription_failed" and calls == [1]

    def rate_limited(*args, **kwargs):
        calls.append(2)
        raise urllib.error.HTTPError(
            "https://private.example", 429, "private", {}, None
        )

    result = GroqMediaPerception(_config(), opener=rate_limited).transcribe_audio(
        str(audio)
    )
    assert result["error"] == "groq_transcription_failed" and calls == [1, 2]


def test_redirect_and_oversized_response_are_denied_without_disclosure(
    tmp_path, monkeypatch
):
    from tools.whatsapp_ops_groq_perception import GroqMediaPerception

    audio = tmp_path / "sound.wav"
    _wav(audio)
    monkeypatch.setenv("GROQ_API_KEY", "key")

    def redirect(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://private.redirect", 302, "redirect", {}, None
        )

    assert (
        GroqMediaPerception(_config(), opener=redirect).transcribe_audio(str(audio))[
            "error"
        ]
        == "groq_transcription_failed"
    )
    result = GroqMediaPerception(
        _config(), opener=lambda *a, **k: _Response(b"x" * (64 * 1024 + 1))
    ).transcribe_audio(str(audio))
    assert result["error"] == "groq_invalid_response" and "private" not in repr(result)
