from __future__ import annotations

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_media_classifier_is_url_free_and_recognizes_audio_sticker_and_document():
    from tools.whatsapp_ops_media import classify_inbound_media
    audio = classify_inbound_media({"message": {"audioMessage": {"mimetype": "audio/ogg", "ptt": True, "mediaUrl": "https://private.invalid/x"}}})
    sticker = classify_inbound_media({"message": {"stickerMessage": {"mimetype": "image/webp"}}})
    document = classify_inbound_media({"message": {"documentMessage": {"mimetype": "application/pdf"}}})
    assert audio["kind"] == "audio" and "url" not in audio
    assert sticker["kind"] == "sticker"
    assert document == {"kind": "unsupported", "reason": "document_unsupported"}


def test_durable_post_ack_audio_job_keeps_handle_private_and_writes_evidence(tmp_path):
    from tools.whatsapp_ops_store import _connect, init_db, lookup_inbound_events, record_inbound_event
    from gateway.whatsapp_ops_media_worker import WhatsAppOpsMediaWorker
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        recorded = record_inbound_event(source_event_id="private-provider-id", contact_ref="c", thread_ref="c", payload={"message": {"audioMessage": {"mimetype": "audio/wav"}}}, resolved_contact_id="known", media={"kind": "audio", "mime": "audio/wav", "size_hint": 16, "provider_handle": "private-provider-id"})
        assert recorded["ok"] and "private-provider-id" not in str(lookup_inbound_events(contact="c"))
        class Perception:
            def transcribe_audio(self, path):
                assert path.endswith("audio.wav")
                return {"ok": True, "text": "não quero duas frases", "language": "pt", "duration_seconds": 1}
        worker = WhatsAppOpsMediaWorker(config={"friends_pilot": {"enabled": True}, "media_perception": {"enabled": True}}, downloader=lambda handle, cap: (b"RIFFxxxxWAVEdata", "audio/wav"), perception=Perception())
        assert worker.run_once() is True
        with _connect() as conn:
            job = conn.execute("SELECT status,provider_handle FROM media_jobs WHERE event_id=?", (recorded["event_id"],)).fetchone()
            evidence = conn.execute("SELECT text,status FROM media_evidence WHERE event_id=?", (recorded["event_id"],)).fetchone()
        assert job["status"] == "completed" and job["provider_handle"] == "private-provider-id"
        assert evidence["status"] == "completed" and evidence["text"] == "não quero duas frases"
    finally:
        reset_hermes_home_override(token)


def test_vision_explicit_routes_do_not_fallback_or_need_network():
    from tools.whatsapp_ops_media_vision import describe_images
    calls = []
    def call(client, content, model):
        from types import SimpleNamespace
        calls.append((client, content, model))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="texto visível"))])
    for provider in ("gemini", "openrouter", "openai-codex"):
        result = describe_images([b"png"], {"auxiliary": {"vision": {"provider": provider, "model": "google/gemini-2.5-flash-lite"}}}, client=object(), call=call)
        assert result["ok"] and result["text"] == "texto visível"
    assert [row[2] for row in calls] == ["google/gemini-2.5-flash-lite"] * 3
    assert describe_images([b"png"], {"auxiliary": {"vision": {"provider": "auto", "model": "x"}}}, client=object(), call=call)["error"] == "vision_not_configured"
