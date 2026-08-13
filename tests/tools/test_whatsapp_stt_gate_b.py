"""Gate B local STT, persistence, and explicit-read contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeInfo:
    language = "pt"
    duration = 1.25


def _seed_audio_event(tmp_path: Path) -> tuple[object, str]:
    from tools.whatsapp_ops_store import init_db, record_inbound_event

    token = set_hermes_home_override(tmp_path)
    init_db()
    recorded = record_inbound_event(
        source_event_id="synthetic-audio-event",
        contact_ref="synthetic-contact@internal.invalid",
        thread_ref="synthetic-thread@internal.invalid",
        payload={
            "id": "synthetic-audio-event",
            "type": "audio",
            "message": {
                "audioMessage": {
                    "mimetype": "audio/ogg",
                    "seconds": 1,
                }
            },
        },
    )
    return token, recorded["event_id"]


def test_backend_contract_is_offline_bounded_and_hides_transcript_from_repr(tmp_path):
    from tools.whatsapp_ops_stt import LocalSttRequest, transcribe_local_audio

    model_dir = tmp_path / "small-model"
    model_dir.mkdir()
    media = tmp_path / "audio.wav"
    media.write_bytes(b"synthetic")
    secret_text = "conteudo privado " + ("x" * 5_000)
    calls = {}

    class FakeModel:
        def transcribe(self, media_path, **kwargs):
            calls["transcribe"] = (media_path, kwargs)
            return iter([FakeSegment(secret_text)]), FakeInfo()

    def factory(model_path, **kwargs):
        calls["model"] = (model_path, kwargs)
        return FakeModel()

    request = LocalSttRequest(
        media_path=str(media), model_path=str(model_dir), language="pt"
    )
    result = transcribe_local_audio(request, enabled=True, model_factory=factory)

    assert result.ok is True
    assert result.status == "completed"
    assert len(result.transcript_text) == 4_000
    assert result.transcript_truncated is True
    assert result.transcript_sha256 == hashlib.sha256(
        result.transcript_text.encode("utf-8")
    ).hexdigest()
    assert secret_text not in repr(result)
    assert result.transcript_text not in repr(result)
    assert calls["model"] == (
        str(model_dir),
        {"device": "cpu", "compute_type": "int8", "local_files_only": True},
    )
    assert calls["transcribe"] == (
        str(media),
        {"language": "pt", "vad_filter": True},
    )
    with pytest.raises(FrozenInstanceError):
        request.language = "en"


@pytest.mark.parametrize(
    ("enabled", "model_name", "media_name", "expected"),
    [
        (False, "model", "audio.wav", "disabled"),
        (True, "missing", "audio.wav", "model_not_cached"),
        (True, "model", "missing.wav", "media_not_available"),
    ],
)
def test_backend_fails_closed_before_factory(
    tmp_path, enabled, model_name, media_name, expected
):
    from tools.whatsapp_ops_stt import LocalSttRequest, transcribe_local_audio

    model = tmp_path / model_name
    media = tmp_path / media_name
    if model_name == "model":
        model.mkdir()
    if media_name == "audio.wav":
        media.write_bytes(b"synthetic")

    result = transcribe_local_audio(
        LocalSttRequest(media_path=str(media), model_path=str(model)),
        enabled=enabled,
        model_factory=lambda *args, **kwargs: pytest.fail("factory must not run"),
    )
    assert result.ok is False
    assert result.status == expected


def test_ffmpeg_normalizes_synthetic_audio_without_shell(tmp_path):
    from tools.whatsapp_ops_stt import normalize_audio_ffmpeg

    source = tmp_path / "source.wav"
    normalized = tmp_path / "normalized.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.2",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    result = normalize_audio_ffmpeg(source, normalized)

    assert result["ok"] is True
    assert normalized.is_file()
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels",
            "-of",
            "json",
            str(normalized),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream == {"sample_rate": "16000", "channels": 1}


def test_persisted_transcript_requires_exact_event_read_and_never_appears_in_status(tmp_path):
    from tools.whatsapp_ops_store import (
        get_db_path,
        get_media_transcript,
        get_media_transcription_status,
        persist_media_transcription,
    )

    token, event_id = _seed_audio_event(tmp_path)
    private_text = "transcricao privada sintetica"
    try:
        first = persist_media_transcription(
            event_id=event_id,
            transcript_text=private_text,
            transcript_sha256=hashlib.sha256(private_text.encode()).hexdigest(),
            transcript_truncated=False,
            detected_language="pt",
            duration_seconds=1.25,
            provider="local_faster_whisper",
            model="small",
        )
        second = persist_media_transcription(
            event_id=event_id,
            transcript_text=private_text,
            transcript_sha256=hashlib.sha256(private_text.encode()).hexdigest(),
            transcript_truncated=False,
            detected_language="pt",
            duration_seconds=1.25,
            provider="local_faster_whisper",
            model="small",
        )
        generic = get_media_transcription_status(limit=20)
        explicit = get_media_transcript(event_id)
        invalid = get_media_transcript("provider-message-id")
        with sqlite3.connect(get_db_path()) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM media_transcriptions WHERE event_id=?", (event_id,)
            ).fetchone()[0]
    finally:
        reset_hermes_home_override(token)

    assert first["ok"] is True and second["ok"] is True
    assert count == 1
    assert private_text not in json.dumps(generic, ensure_ascii=False)
    assert explicit["ok"] is True
    assert explicit["transcript"] == private_text
    assert invalid["ok"] is False
    assert private_text not in repr(first)
    assert private_text not in repr(second)


def test_synthetic_pipeline_acquires_transcodes_transcribes_persists_reads_and_cleans(tmp_path):
    from tools.whatsapp_ops_store import get_media_transcript
    from tools.whatsapp_ops_stt import run_inbound_stt_pipeline

    token, event_id = _seed_audio_event(tmp_path)
    model_dir = tmp_path / "small-model"
    model_dir.mkdir()
    temp_root = tmp_path / "stt-temp"
    private_text = "pipeline sintetico concluido"

    class FakeModel:
        def transcribe(self, media_path, **kwargs):
            assert Path(media_path).is_file()
            return iter([FakeSegment(private_text)]), FakeInfo()

    def download(_provider_message_id: str, max_bytes: int):
        source = tmp_path / "download-source.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=660:duration=0.2",
                str(source),
            ],
            check=True,
            timeout=30,
        )
        data = source.read_bytes()
        assert len(data) <= max_bytes
        return data, "audio/wav"

    try:
        result = run_inbound_stt_pipeline(
            event_id=event_id,
            provider_message_id="synthetic-provider-message",
            enabled=True,
            model_path=str(model_dir),
            language="pt",
            downloader=download,
            model_factory=lambda *args, **kwargs: FakeModel(),
            temp_root=temp_root,
        )
        explicit = get_media_transcript(event_id)
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["download_performed"] is True
    assert result["transcription_performed"] is True
    assert result["transcript_persisted"] is True
    assert result["send_performed"] is False
    assert result["crm_write_performed"] is False
    assert result["provider_history_used"] is False
    assert private_text not in serialized
    assert "synthetic-provider-message" not in serialized
    assert explicit["transcript"] == private_text
    assert not temp_root.exists() or list(temp_root.iterdir()) == []


def test_explicit_transcript_tool_is_registered_and_generic_tool_hides_text(tmp_path):
    from tools.registry import registry
    from tools.whatsapp_ops_store import persist_media_transcription
    from tools.whatsapp_ops_tool import (
        wpp_media_transcription_status,
        wpp_read_media_transcript,
    )

    token, event_id = _seed_audio_event(tmp_path)
    private_text = "leitura explicita apenas"
    try:
        persisted = persist_media_transcription(
            event_id=event_id,
            transcript_text=private_text,
            transcript_sha256=hashlib.sha256(private_text.encode()).hexdigest(),
            transcript_truncated=False,
            detected_language="pt",
            duration_seconds=1.0,
            provider="local_faster_whisper",
            model="small",
        )
        generic = json.loads(wpp_media_transcription_status(limit=20))
        explicit = json.loads(wpp_read_media_transcript(event_id))
        entry = registry._tools.get("wpp_read_media_transcript")
    finally:
        reset_hermes_home_override(token)

    assert persisted["ok"] is True
    assert private_text not in json.dumps(generic, ensure_ascii=False)
    assert explicit["transcript"] == private_text
    assert explicit["explicit_read"] is True
    assert entry is not None
    assert "only transcript-content read surface" in entry.schema["description"]


def test_explicit_purge_removes_text_and_declares_plaintext_backup_risk(tmp_path):
    from tools.whatsapp_ops_store import (
        get_media_transcript,
        persist_media_transcription,
        purge_media_transcript,
    )

    token, event_id = _seed_audio_event(tmp_path)
    private_text = "conteudo a expurgar"
    try:
        persisted = persist_media_transcription(
            event_id=event_id,
            transcript_text=private_text,
            transcript_sha256=hashlib.sha256(private_text.encode()).hexdigest(),
            transcript_truncated=False,
            detected_language="pt",
            duration_seconds=1.0,
            provider="local_faster_whisper",
            model="small",
        )
        before = get_media_transcript(event_id)
        purged = purge_media_transcript(event_id)
        after = get_media_transcript(event_id)
    finally:
        reset_hermes_home_override(token)

    assert persisted["ok"] is True
    assert before["transcript"] == private_text
    assert before["retention_policy"] == "manual_explicit_purge"
    assert before["backup_contains_plaintext_transcript"] is True
    assert purged == {
        "ok": True,
        "status": "purged",
        "event_id": event_id,
        "purged": True,
        "retention_policy": "manual_explicit_purge",
    }
    assert after["ok"] is False
    assert after["transcript"] == ""
