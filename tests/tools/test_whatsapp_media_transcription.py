"""Fail-closed contract tests for WhatsApp Ops media transcription phase 1."""

from __future__ import annotations

import json
import sqlite3

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


SAFETY_FLAGS = {
    "transcription_performed",
    "download_performed",
    "stt_provider_called",
    "provider_history_used",
    "send_performed",
    "llm_used",
    "transcript_persisted",
    "raw_media_exposed",
}


def _counts(db_path):
    with sqlite3.connect(db_path) as conn:
        tables = ["inbound_events", "media_transcriptions", "drafts", "approvals", "outbox", "audit_log"]
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def _assert_safety_flags_false(payload: dict) -> None:
    for flag in SAFETY_FLAGS:
        assert payload[flag] is False


def test_wpp_transcribe_media_phase1_records_blocked_status_without_side_effects(tmp_path):
    from tools import whatsapp_ops_tool as tool
    from tools.whatsapp_ops_store import get_db_path, init_db, record_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        db_path = get_db_path()
        before = _counts(db_path)
        recorded = record_inbound_event(
            source_event_id="provider-event-audio-001",
            contact_ref="551199998888@s.whatsapp.net",
            thread_ref="120363375521827492@g.us",
            payload={
                "id": "provider-event-audio-001",
                "type": "audio",
                "message": {
                    "audioMessage": {
                        "mimetype": "audio/ogg",
                        "seconds": 4,
                        "mediaKey": "SECRET_MEDIA_KEY",
                        "directPath": "https://media.example/path",
                        "fileSha256": "ABCDEFBASE64ABCDEFBASE64ABCDEFBASE64",
                    }
                },
                "caption": "fone +551199998888 token=abc123",
            },
        )
        result = json.loads(getattr(tool, "wpp_transcribe_media")(event_id=recorded["event_id"]))
        status = json.loads(getattr(tool, "wpp_media_transcription_status")(event_id=recorded["event_id"]))
        after = _counts(db_path)
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps({"result": result, "status": status}, ensure_ascii=False)
    assert recorded["event_id"].startswith("inbound_")
    assert result["ok"] is True
    assert result["source"] == "local_inbound_store"
    assert result["status"] == "blocked"
    assert result["reason"] == "provider_not_configured"
    assert result["local_status_only"] is True
    assert result["transcript_available"] is False
    assert result["status_persisted"] is True
    assert result["media_type"] == "audio"
    assert status["count"] == 1
    _assert_safety_flags_false(result)
    _assert_safety_flags_false(status)

    assert after["inbound_events"] == before["inbound_events"] + 1
    assert after["media_transcriptions"] == before["media_transcriptions"] + 1
    assert after["drafts"] == before["drafts"]
    assert after["approvals"] == before["approvals"]
    assert after["outbox"] == before["outbox"]
    assert after["audit_log"] == before["audit_log"]

    for forbidden in {
        "@g.us",
        "@lid",
        "@s.whatsapp.net",
        "551199998888",
        "120363375521827492",
        "provider-event-audio-001",
        "https://media.example",
        "SECRET_MEDIA_KEY",
        "ABCDEFBASE64",
        "data:",
        "base64",
    }:
        assert forbidden not in serialized


def test_wpp_transcribe_media_rejects_provider_refs_without_persisting(tmp_path):
    from tools import whatsapp_ops_tool as tool
    from tools.whatsapp_ops_store import get_db_path, init_db

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        db_path = get_db_path()
        before = _counts(db_path)
        result = json.loads(getattr(tool, "wpp_transcribe_media")(event_id="551199998888@s.whatsapp.net"))
        after = _counts(db_path)
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is False
    assert result["status"] == "invalid_event_id"
    assert result["error"] == "event_id_must_be_internal_inbound_id"
    assert result["event_id_accepted"] is False
    assert result["status_persisted"] is False
    _assert_safety_flags_false(result)
    assert after == before
    serialized = json.dumps(result, ensure_ascii=False)
    assert "@s.whatsapp.net" not in serialized
    assert "551199998888" not in serialized


def test_wpp_transcribe_media_non_media_event_is_not_persisted(tmp_path):
    from tools import whatsapp_ops_tool as tool
    from tools.whatsapp_ops_store import get_db_path, init_db, record_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        db_path = get_db_path()
        recorded = record_inbound_event(
            source_event_id="provider-event-text-001",
            contact_ref="551199998888@s.whatsapp.net",
            thread_ref="120363375521827492@g.us",
            payload={"id": "provider-event-text-001", "type": "text", "text": "olá"},
        )
        before = _counts(db_path)
        result = json.loads(getattr(tool, "wpp_transcribe_media")(event_id=recorded["event_id"]))
        after = _counts(db_path)
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is False
    assert result["status"] == "no_transcribable_media"
    assert result["reason"] == "event_has_no_audio_voice_or_video_media"
    assert result["status_persisted"] is False
    _assert_safety_flags_false(result)
    assert after == before
