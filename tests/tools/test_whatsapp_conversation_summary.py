"""Contract tests for deterministic local WhatsApp conversation summaries."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


RAW_THREAD = "120363375521827492@g.us"
RAW_CONTACT = "172185238905034@lid"
RAW_PHONE = "5511999988888"
RAW_URL = "https://cdn.example.invalid/media/audio.ogg?token=abc123"
RAW_DATA_URL = "data:audio/ogg;base64,QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
RAW_BLOB_URL = "blob:https://example.invalid/abc"
RAW_B64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
RAW_SOURCE_ID = "source-real-event-001"
RAW_SECRET = "sk-testsecret123456"


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _snapshot_tables(db_path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def _seed_sensitive_events(tmp_path):
    from tools.whatsapp_ops_store import get_db_path, init_db, record_inbound_event

    token = set_hermes_home_override(tmp_path)
    init_db()
    first = record_inbound_event(
        source_event_id=RAW_SOURCE_ID,
        contact_ref=RAW_CONTACT,
        thread_ref=RAW_THREAD,
        payload={
            "messageType": "text",
            "text": (
                f"Olá {RAW_PHONE}; veja {RAW_URL}; auth token=abc123; "
                f"Bearer abcdefghijklmnop; {RAW_SECRET}; media {RAW_DATA_URL}; blob {RAW_BLOB_URL}; {RAW_B64}"
            ),
            "participant": RAW_CONTACT,
            "remoteJid": RAW_THREAD,
            "mediaKey": "unsafe-media-key",
            "directPath": "/unsafe/provider/path",
            "jpegThumbnail": RAW_B64,
        },
    )
    second = record_inbound_event(
        source_event_id="source-real-event-002",
        contact_ref=RAW_CONTACT,
        thread_ref=RAW_THREAD,
        payload={
            "messageType": "audio",
            "audioMessage": {
                "mimetype": "audio/ogg",
                "seconds": 4,
                "url": RAW_URL,
                "mediaKey": "unsafe-media-key-2",
                "fileSha256": "unsafe-sha",
            },
            "caption": "áudio recebido",
        },
    )
    db_path = get_db_path()
    # Stable chronology for timeline ASC test; lookup itself returns DESC.
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE inbound_events SET created_at=? WHERE id=?", ("2026-06-27T10:00:00+00:00", first["event_id"]))
        conn.execute("UPDATE inbound_events SET created_at=? WHERE id=?", ("2026-06-27T10:01:00+00:00", second["event_id"]))
    return token, db_path


def test_get_conversation_summary_brief_is_local_read_only_and_redacted(tmp_path):
    from tools.whatsapp_ops_store import get_conversation_summary

    token, db_path = _seed_sensitive_events(tmp_path)
    before = _snapshot_tables(db_path)
    try:
        summary = get_conversation_summary(thread=RAW_THREAD, limit=20, mode="brief", max_text_chars=500)
        after = _snapshot_tables(db_path)
    finally:
        reset_hermes_home_override(token)

    assert summary["ok"] is True
    assert summary["source"] == "local_inbound_store"
    assert summary["generated_by"] == "deterministic_local_v1"
    assert summary["llm_used"] is False
    assert summary["provider_history_used"] is False
    assert summary["send_performed"] is False
    assert summary["summary_persisted"] is False
    assert summary["read_only"] is True
    assert summary["local_store_only"] is True
    assert summary["sends_messages"] is False
    assert summary["fetches_provider_history"] is False
    assert summary["exposes_raw_refs"] is False
    assert summary["thread_filter_set"] is True
    assert summary["contact_filter_set"] is False
    assert summary["message_count"] == 2
    assert before == after

    serialized = _json_text(summary)
    for forbidden in (
        RAW_THREAD,
        RAW_CONTACT,
        "@g.us",
        "@lid",
        RAW_PHONE,
        RAW_URL,
        "cdn.example.invalid",
        RAW_DATA_URL,
        RAW_BLOB_URL,
        RAW_B64,
        RAW_SOURCE_ID,
        RAW_SECRET,
        "unsafe-media-key",
        "/unsafe/provider/path",
        "unsafe-sha",
        "token=abc123",
        "Bearer abcdefghijklmnop",
    ):
        assert forbidden not in serialized


def test_conversation_summary_stats_omits_text_timeline_and_payload(tmp_path):
    from tools.whatsapp_ops_store import get_conversation_summary

    token, _db_path = _seed_sensitive_events(tmp_path)
    try:
        summary = get_conversation_summary(thread=RAW_THREAD, mode="stats", max_text_chars=500)
    finally:
        reset_hermes_home_override(token)

    serialized = _json_text(summary)
    assert summary["mode"] == "stats"
    assert summary["message_count"] == 2
    assert "timeline" not in summary
    assert "evidence" not in summary
    assert "latest_previews" not in summary
    assert "text_preview" not in serialized
    assert "payload" not in serialized
    assert "Olá" not in serialized
    assert "áudio recebido" not in serialized


def test_conversation_summary_timeline_is_chronological_and_media_is_safe(tmp_path):
    from tools.whatsapp_ops_store import get_conversation_summary

    token, _db_path = _seed_sensitive_events(tmp_path)
    try:
        summary = get_conversation_summary(thread=RAW_THREAD, mode="timeline", max_text_chars=80)
    finally:
        reset_hermes_home_override(token)

    timeline = summary["timeline"]
    created = [event["created_at"] for event in timeline]
    assert created == sorted(created)
    assert summary["window"] == {
        "first_created_at": "2026-06-27T10:00:00+00:00",
        "last_created_at": "2026-06-27T10:01:00+00:00",
    }
    media_event = next(event for event in timeline if event["message_type"] == "audio")
    assert media_event["media"]["mimetype"] == "audio/ogg"
    assert media_event["media"]["seconds"] == 4
    serialized = _json_text(media_event)
    assert RAW_URL not in serialized
    assert "mediaKey" not in serialized
    assert "fileSha256" not in serialized


def test_conversation_summary_empty_invalid_mode_and_clamps(tmp_path):
    from tools.whatsapp_ops_store import get_conversation_summary, init_db

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        summary = get_conversation_summary(limit=999, mode="bad", max_text_chars=9999)
    finally:
        reset_hermes_home_override(token)

    assert summary["ok"] is True
    assert summary["mode"] == "brief"
    assert summary["message_count"] == 0
    assert summary["limit"] == 100
    assert summary["max_text_chars"] == 500
    assert set(summary["warnings"]) >= {
        "invalid_mode_defaulted_to_brief",
        "limit_clamped",
        "max_text_chars_clamped",
        "unscoped_local_scan",
        "no_local_events",
    }


def test_conversation_summary_evidence_is_bounded_and_not_payload_debug(tmp_path):
    from tools.whatsapp_ops_store import get_conversation_summary

    token, _db_path = _seed_sensitive_events(tmp_path)
    try:
        summary = get_conversation_summary(thread=RAW_THREAD, mode="evidence", max_text_chars=50)
    finally:
        reset_hermes_home_override(token)

    assert summary["mode"] == "evidence"
    assert len(summary["evidence"]) == 2
    serialized = _json_text(summary)
    assert "payload_redacted" not in serialized
    assert "payload" not in serialized
    assert "debug" not in serialized
    assert RAW_URL not in serialized
    assert RAW_THREAD not in serialized
    assert RAW_CONTACT not in serialized


def test_wpp_conversation_summary_wrapper_and_registry(tmp_path):
    import tools.whatsapp_ops_tool  # noqa: F401
    from tools.registry import registry
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_conversation_summary

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        parsed = json.loads(wpp_conversation_summary(mode="stats"))
        dispatched = json.loads(registry.dispatch("wpp_conversation_summary", {"mode": "stats"}))
    finally:
        reset_hermes_home_override(token)

    assert parsed["ok"] is True
    assert dispatched["ok"] is True
    assert "wpp_conversation_summary" in set(registry.get_tool_names_for_toolset("whatsapp_ops"))
    entry = registry.get_entry("wpp_conversation_summary")
    assert entry is not None
    assert entry.check_fn is not None
    description = entry.schema["description"].lower()
    for term in ("local", "read-only", "never sends", "never creates drafts", "never fetches provider history", "never persists"):
        assert term in description


def _seed_windowed_history_events(tmp_path):
    from tools.whatsapp_ops_store import get_db_path, init_db, record_inbound_event

    token = set_hermes_home_override(tmp_path)
    init_db()
    recent = record_inbound_event(
        source_event_id="synthetic-recent-window-event",
        contact_ref="synthetic-contact@lid",
        thread_ref="synthetic-thread@g.us",
        payload={"messageType": "text", "text": "sinal recente para follow-up comercial"},
    )
    older = record_inbound_event(
        source_event_id="synthetic-older-window-event",
        contact_ref="synthetic-contact@lid",
        thread_ref="synthetic-thread@g.us",
        payload={"messageType": "text", "text": "mensagem antiga fora da janela"},
    )
    very_old = record_inbound_event(
        source_event_id="synthetic-very-old-window-event",
        contact_ref="synthetic-contact@lid",
        thread_ref="synthetic-thread@g.us",
        payload={"messageType": "audio", "caption": "áudio antigo fora da janela"},
    )
    db_path = get_db_path()
    now = datetime.now(timezone.utc)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE inbound_events SET created_at=? WHERE id=?", ((now - timedelta(days=1)).isoformat(), recent["event_id"]))
        conn.execute("UPDATE inbound_events SET created_at=? WHERE id=?", ((now - timedelta(days=10)).isoformat(), older["event_id"]))
        conn.execute("UPDATE inbound_events SET created_at=? WHERE id=?", ((now - timedelta(days=40)).isoformat(), very_old["event_id"]))
    return token, db_path


def test_conversation_summary_window_days_filters_local_history_without_provider_pull(tmp_path):
    from tools.whatsapp_ops_store import get_conversation_summary

    token, db_path = _seed_windowed_history_events(tmp_path)
    before = _snapshot_tables(db_path)
    try:
        summary = get_conversation_summary(
            thread="synthetic-thread@g.us",
            limit=20,
            mode="brief",
            window_days=7,
            max_text_chars=120,
        )
        after = _snapshot_tables(db_path)
    finally:
        reset_hermes_home_override(token)

    assert summary["ok"] is True
    assert summary["message_count"] == 1
    assert summary["history_window"]["requested_days"] == 7
    assert summary["history_window"]["applied"] is True
    assert summary["history_window"]["excluded_by_window"] == 2
    assert summary["provider_history_used"] is False
    assert summary["send_performed"] is False
    assert summary["summary_persisted"] is False
    assert before == after
    serialized = _json_text(summary)
    assert "sinal recente" in serialized
    assert "mensagem antiga fora da janela" not in serialized
    assert "áudio antigo fora da janela" not in serialized


def test_conversation_summary_chunks_are_bounded_chronological_and_safe(tmp_path):
    from tools.whatsapp_ops_store import get_db_path, get_conversation_summary, init_db, record_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        now = datetime.now(timezone.utc)
        db_path = get_db_path()
        event_ids = []
        for idx in range(5):
            result = record_inbound_event(
                source_event_id=f"synthetic-chunk-event-{idx}",
                contact_ref="synthetic-contact@lid",
                thread_ref="synthetic-thread@g.us",
                payload={
                    "messageType": "text",
                    "text": f"mensagem {idx} com conteúdo comercial bounded e token=abc123 https://example.invalid/{idx}",
                },
            )
            event_ids.append(result["event_id"])
        with sqlite3.connect(db_path) as conn:
            for idx, event_id in enumerate(event_ids):
                conn.execute(
                    "UPDATE inbound_events SET created_at=? WHERE id=?",
                    ((now - timedelta(minutes=5 - idx)).isoformat(), event_id),
                )
        before = _snapshot_tables(db_path)
        summary = get_conversation_summary(
            thread="synthetic-thread@g.us",
            limit=20,
            mode="chunks",
            window_days=30,
            chunk_size=2,
            max_text_chars=48,
        )
        after = _snapshot_tables(db_path)
    finally:
        reset_hermes_home_override(token)

    assert before == after
    assert summary["mode"] == "chunks"
    assert summary["chunk_size"] == 2
    assert summary["chunk_count"] == 3
    assert [chunk["event_count"] for chunk in summary["chunks"]] == [2, 2, 1]
    chunk_starts = [chunk["window"]["first_created_at"] for chunk in summary["chunks"]]
    assert chunk_starts == sorted(chunk_starts)
    assert summary["llm_used"] is False
    assert summary["provider_history_used"] is False
    assert summary["summary_persisted"] is False
    serialized = _json_text(summary)
    assert "https://" not in serialized
    assert "token=abc123" not in serialized
    assert "@g.us" not in serialized
    for chunk in summary["chunks"]:
        assert len(chunk.get("highlights", [])) <= 2
        assert "payload" not in chunk


def test_wpp_conversation_summary_wrapper_accepts_window_and_chunks(tmp_path):
    import tools.whatsapp_ops_tool  # noqa: F401
    from tools.registry import registry
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_conversation_summary

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        parsed = json.loads(wpp_conversation_summary(mode="chunks", window_days=30, chunk_size=2))
        dispatched = json.loads(
            registry.dispatch(
                "wpp_conversation_summary",
                {"mode": "chunks", "window_days": 30, "chunk_size": 2},
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert parsed["ok"] is True
    assert parsed["mode"] == "chunks"
    assert parsed["history_window"]["requested_days"] == 30
    assert dispatched["mode"] == "chunks"
    entry = registry.get_entry("wpp_conversation_summary")
    assert entry is not None
    params = entry.schema["parameters"]["properties"]
    assert "window_days" in params
    assert "chunk_size" in params
    assert "chunks" in params["mode"]["enum"]
