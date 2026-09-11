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


def test_unbound_contact_cannot_create_durable_private_job(tmp_path):
    from tools.whatsapp_ops_store import _connect, init_db, record_inbound_event
    token=set_hermes_home_override(tmp_path)
    try:
        init_db()
        recorded=record_inbound_event(source_event_id="private-provider-id",contact_ref="c",thread_ref="c",payload={"message":{"audioMessage":{"mimetype":"audio/wav"}}},resolved_contact_id="known",media={"kind":"audio","provider_handle":"private-provider-id"})
        assert recorded["ok"]
        with _connect() as conn:
            assert conn.execute("SELECT count(*) FROM media_jobs").fetchone()[0]==0
    finally:reset_hermes_home_override(token)



def test_vision_explicit_routes_do_not_fallback_or_need_network():
    from tools.whatsapp_ops_media_vision import describe_images
    import io,json
    from PIL import Image
    image=io.BytesIO();Image.new("RGB",(8,8),"blue").save(image,format="PNG")
    calls = []
    def call(client, content, model):
        from types import SimpleNamespace
        calls.append((client, content, model))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"ocr_text":"texto visível","description":"azul","uncertainty":""})))])
    for provider in ("gemini", "openrouter", "openai-codex"):
        result = describe_images([image.getvalue()], {"auxiliary": {"vision": {"provider": provider, "model": "google/gemini-2.5-flash-lite"}}}, client=object(), call=call)
        assert result["ok"] and result["ocr_text"] == "texto visível"
    assert [row[2] for row in calls] == ["google/gemini-2.5-flash-lite"] * 3
    assert describe_images([image.getvalue()], {"auxiliary": {"vision": {"provider": "auto", "model": "x"}}}, client=object(), call=call)["error"] == "vision_not_configured"
