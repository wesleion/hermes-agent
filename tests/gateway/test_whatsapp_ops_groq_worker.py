"""Groq selection is an explicit, worker-local perception route."""

from __future__ import annotations

from gateway.whatsapp_ops_media_worker import WhatsAppOpsMediaWorker
from tools import whatsapp_ops_groq_perception as groq


def _settings():
    return {
        "audio": {
            "provider": "groq",
            "model": "whisper-large-v3-turbo",
            "timeout": 30,
            "language": "",
        },
        "local": {"max_threads": 2},
    }


def test_explicit_groq_audio_config_routes_actual_worker_without_send(monkeypatch):
    created = []

    class FakeGroq:
        def __init__(self, config):
            created.append(config)

        def transcribe_audio(self, path):
            assert path == "/tmp/normalized.wav"
            return {
                "ok": True,
                "text": "transcrição Groq",
                "language": "pt",
                "duration_seconds": 1,
                "truncated": False,
            }

    monkeypatch.setattr(groq, "GroqMediaPerception", FakeGroq)
    result = WhatsAppOpsMediaWorker(config={})._perceive(
        {"audio_path": "/tmp/normalized.wav"}, "audio", _settings()
    )
    assert result["text"] == "transcrição Groq" and created == [_settings()["audio"]]


def test_injected_perception_precedes_explicit_groq_selection():
    seen = []

    class Injected:
        def transcribe_audio(self, path):
            seen.append(path)
            return {"ok": True, "text": "injected", "truncated": False}

    result = WhatsAppOpsMediaWorker(config={}, perception=Injected())._perceive(
        {"audio_path": "/tmp/normalized.wav"}, "audio", _settings()
    )
    assert result["text"] == "injected" and seen == ["/tmp/normalized.wav"]


def test_explicit_null_audio_config_is_invalid_not_legacy_fallback(monkeypatch):
    from tools import whatsapp_ops_local_perception as local

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid explicit audio configuration fell back to local")

    monkeypatch.setattr(local, "LocalMediaPerception", forbidden)
    result = WhatsAppOpsMediaWorker(config={})._perceive(
        {"audio_path": "/tmp/not-read.wav"}, "audio", {"audio": None}
    )
    assert not result["ok"] and result["error"] == "groq_configuration_invalid"


def test_safe_error_keeps_only_allowlisted_groq_errors():
    assert (
        WhatsAppOpsMediaWorker._safe_error("groq_not_configured")
        == "groq_not_configured"
    )
    assert (
        WhatsAppOpsMediaWorker._safe_error("secret /private/path")
        == "media_perception_failed"
    )
