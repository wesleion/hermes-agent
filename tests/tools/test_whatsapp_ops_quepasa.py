import io
import json
from email.message import Message
from unittest.mock import patch


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, *_args):
        return self._body


def _base_payload(target=None, message="Mensagem teste"):
    return {
        "draft_id": "draft_1",
        "targets": [target or {"type": "group", "group_id": "120363375521827492@g.us"}],
        "message": message,
        "idempotency_key": "idem_1",
    }


def _request_body(req):
    return json.loads(req.data.decode("utf-8"))


def test_quepasa_client_fails_closed_when_disabled():
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    result = send_via_quepasa(
        payload={"message": "não enviar"},
        config={"quepasa": {"send_enabled": False}},
    )

    assert result == {"ok": False, "error": "quepasa_send_disabled"}


def test_quepasa_client_fails_closed_without_endpoint(monkeypatch):
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    monkeypatch.delenv("WHATSAPP_OPS_QUEPASA_SEND_URL", raising=False)
    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")

    result = send_via_quepasa(
        payload={"message": "não enviar"},
        config={"quepasa": {"send_enabled": True}},
    )

    assert result == {"ok": False, "error": "quepasa_send_url_missing"}


def test_quepasa_client_fails_closed_without_api_key(monkeypatch):
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    monkeypatch.delenv("WHATSAPP_OPS_QUEPASA_API_KEY", raising=False)

    with patch("urllib.request.urlopen") as urlopen:
        result = send_via_quepasa(
            payload=_base_payload(),
            config={"quepasa": {"send_enabled": True, "send_url": "https://quepasa.wesleion.com"}},
        )

    assert result == {"ok": False, "error": "quepasa_api_key_missing"}
    urlopen.assert_not_called()


def test_provider_history_pull_fails_closed_when_disabled(monkeypatch):
    from tools.whatsapp_ops_quepasa import pull_history_via_quepasa

    with patch("urllib.request.urlopen") as urlopen:
        result = pull_history_via_quepasa(
            thread="provider_history_thread_synthetic",
            config={"provider_history": {"enabled": False}, "quepasa": {"send_url": "https://quepasa.wesleion.com"}},
        )

    assert result["ok"] is False
    assert result["error"] == "provider_history_disabled"
    assert result["read_only"] is True
    assert result["provider_history_used"] is False
    assert result["send_performed"] is False
    assert result["summary_persisted"] is False
    urlopen.assert_not_called()


def test_provider_history_pull_reports_quepasa_unsupported_without_leaking_refs(monkeypatch):
    from tools.whatsapp_ops_quepasa import pull_history_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")
    captured = {}
    swagger = json.dumps({
        "paths": {
            "/message/{messageid}": {"get": {"summary": "Get message"}},
            "/groups/get": {"get": {"summary": "Get group information"}},
            "/send": {"post": {"summary": "Send message"}},
        }
    })

    def fake_urlopen(req, timeout):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse(swagger)

    with patch("urllib.request.urlopen", fake_urlopen):
        result = pull_history_via_quepasa(
            thread="provider_history_thread_synthetic",
            limit=5000,
            pages=99,
            config={
                "provider_history": {"enabled": True, "max_messages": 250, "max_pages": 3},
                "quepasa": {"send_url": "https://quepasa.wesleion.com/swagger/doc.json"},
            },
        )

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is False
    assert result["error"] == "provider_history_unsupported"
    assert result["provider_history_used"] is False
    assert result["read_only"] is True
    assert result["send_performed"] is False
    assert result["summary_persisted"] is False
    assert result["target"]["thread_filter_set"] is True
    assert result["request_limits"] == {"limit": 250, "pages": 3}
    assert result["capabilities"]["history_list_supported"] is False
    assert result["capabilities"]["single_message_lookup_supported"] is True
    assert captured["req"].get_method() == "GET"
    assert captured["req"].full_url == "https://quepasa.wesleion.com/swagger/doc.json"
    assert "provider_history_thread_synthetic" not in serialized
    assert "@g.us" not in serialized
    assert "secret-token" not in serialized


def test_quepasa_client_does_not_call_http_when_disabled():
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    with patch("urllib.request.urlopen") as urlopen:
        result = send_via_quepasa(
            payload={"message": "não enviar"},
            config={"quepasa": {"send_enabled": False, "send_url": "http://127.0.0.1/send"}},
        )

    assert result["ok"] is False
    urlopen.assert_not_called()


def test_quepasa_direct_send_uses_post_send_endpoint_and_token_header(monkeypatch):
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse('{"success":true,"status":"sent","message":{"id":"m1","chatId":"120363375521827492@g.us","wid":"w1","trackId":"t1"}}')

    with patch("urllib.request.urlopen", fake_urlopen):
        result = send_via_quepasa(
            payload=_base_payload(),
            config={"quepasa": {"send_enabled": True, "send_url": "https://quepasa.wesleion.com"}},
        )

    req = captured["req"]
    assert result["ok"] is True
    assert result["transport"] == "quepasa_direct"
    assert req.get_method() == "POST"
    assert req.full_url == "https://quepasa.wesleion.com/send"
    assert req.get_header("X-quepasa-token") == "secret-token"
    assert req.get_header("Authorization") is None
    assert _request_body(req) == {"chatId": "120363375521827492@g.us", "text": "Mensagem teste"}


def test_quepasa_direct_send_media_url_posts_send_without_leaking_response_body(monkeypatch):
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse('{"success":true,"status":"sent","message":{"id":"m1"}}')

    payload = _base_payload(message="Segue imagem")
    payload["media"] = {"type": "image", "url": "https://static.example.invalid/img.jpg", "filename": "img.jpg"}
    with patch("urllib.request.urlopen", fake_urlopen):
        result = send_via_quepasa(
            payload=payload,
            config={"quepasa": {"send_enabled": True, "send_url": "https://quepasa.wesleion.com"}},
        )

    assert result["ok"] is True
    assert result["media_sent"] is True
    assert captured["req"].full_url == "https://quepasa.wesleion.com/send"
    assert _request_body(captured["req"]) == {
        "chatId": "120363375521827492@g.us",
        "text": "Segue imagem",
        "url": "https://static.example.invalid/img.jpg",
        "fileName": "img.jpg",
    }


def test_quepasa_direct_send_document_uses_senddocument_endpoint(monkeypatch):
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["req"] = req
        return _FakeResponse('{"success":true,"status":"sent","message":{"id":"m1"}}')

    payload = _base_payload(message="Segue documento")
    payload["media"] = {
        "type": "document",
        "url": "https://static.example.invalid/material.pdf",
        "filename": "material.pdf",
        "as_document": True,
    }
    with patch("urllib.request.urlopen", fake_urlopen):
        result = send_via_quepasa(
            payload=payload,
            config={"quepasa": {"send_enabled": True, "send_url": "https://quepasa.wesleion.com/swagger/doc.json"}},
        )

    assert result["ok"] is True
    assert captured["req"].full_url == "https://quepasa.wesleion.com/senddocument"
    assert _request_body(captured["req"])["fileName"] == "material.pdf"



def test_quepasa_direct_send_normalizes_swagger_urls_to_send(monkeypatch):
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")
    seen_urls = []

    def fake_urlopen(req, timeout):
        seen_urls.append(req.full_url)
        return _FakeResponse('{"success":true,"status":"sent","message":{"id":"m1"}}')

    for input_url in [
        "https://quepasa.wesleion.com/swagger/index.html",
        "https://quepasa.wesleion.com/swagger/doc.json",
        "https://quepasa.wesleion.com/send",
    ]:
        with patch("urllib.request.urlopen", fake_urlopen):
            result = send_via_quepasa(
                payload=_base_payload(),
                config={"quepasa": {"send_enabled": True, "send_url": input_url}},
            )
        assert result["ok"] is True

    assert seen_urls == ["https://quepasa.wesleion.com/send"] * 3


def test_quepasa_direct_send_maps_success_false_to_failed(monkeypatch):
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")

    with patch("urllib.request.urlopen", lambda req, timeout: _FakeResponse('{"success":false,"status":"nope","debug":["x"]}')):
        result = send_via_quepasa(
            payload=_base_payload(),
            config={"quepasa": {"send_enabled": True, "send_url": "https://quepasa.wesleion.com"}},
        )

    assert result["ok"] is False
    assert result["transport"] == "quepasa_direct"
    assert result["status"] == 200
    assert result["error"] == "quepasa_success_false"
    assert "body" not in result


def test_quepasa_direct_send_maps_http_error_without_raw_body(monkeypatch):
    import urllib.error
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "Bad Request",
            hdrs=Message(),
            fp=io.BytesIO(b'{"success":false,"status":"bad","debug":["secret should not leak"]}'),
        )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = send_via_quepasa(
            payload=_base_payload(),
            config={"quepasa": {"send_enabled": True, "send_url": "https://quepasa.wesleion.com"}},
        )

    assert result["ok"] is False
    assert result["error"] == "http_error"
    assert result["status"] == 400
    assert "body" not in result
    assert "secret" not in json.dumps(result).lower()


def test_quepasa_direct_send_rejects_invalid_target_without_http(monkeypatch):
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")

    with patch("urllib.request.urlopen") as urlopen:
        result = send_via_quepasa(
            payload=_base_payload(target={"type": "group"}),
            config={"quepasa": {"send_enabled": True, "send_url": "https://quepasa.wesleion.com"}},
        )

    assert result == {"ok": False, "error": "target_invalid"}
    urlopen.assert_not_called()


def test_quepasa_group_create_fails_closed_without_group_flag(monkeypatch):
    from tools.whatsapp_ops_quepasa import create_group_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")
    payload = {"group_create": {"title": "Grupo Teste", "participants": ["5511999990000@s.whatsapp.net"]}}

    with patch("urllib.request.urlopen") as urlopen:
        result = create_group_via_quepasa(
            payload=payload,
            config={"quepasa": {"send_enabled": True, "group_create_enabled": False, "send_url": "https://quepasa.wesleion.com"}},
        )

    assert result == {"ok": False, "error": "quepasa_group_create_disabled"}
    urlopen.assert_not_called()


def test_quepasa_group_create_posts_groups_create_and_sanitizes_response(monkeypatch):
    from tools.whatsapp_ops_quepasa import create_group_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse('{"success":true,"status":"created","groupinfo":{"id":"120363999@g.us","Name":"Grupo Teste"}}')

    payload = {"group_create": {"title": "Grupo Teste", "participants": ["5511999990000@s.whatsapp.net"]}}
    with patch("urllib.request.urlopen", fake_urlopen):
        result = create_group_via_quepasa(
            payload=payload,
            config={"quepasa": {"send_enabled": True, "group_create_enabled": True, "send_url": "https://quepasa.wesleion.com/swagger/doc.json"}},
        )

    req = captured["req"]
    assert result["ok"] is True
    assert result["transport"] == "quepasa_direct_group_create"
    assert result["participant_count"] == 1
    assert result["group_ref_hash"]
    assert "120363999@g.us" not in json.dumps(result, ensure_ascii=False)
    assert req.get_method() == "POST"
    assert req.full_url == "https://quepasa.wesleion.com/groups/create"
    assert req.get_header("X-quepasa-token") == "secret-token"
    assert _request_body(req) == {"title": "Grupo Teste", "participants": ["5511999990000@s.whatsapp.net"]}


def test_quepasa_group_create_resolves_lid_participants_to_phone(monkeypatch):
    from tools.whatsapp_ops_quepasa import create_group_via_quepasa

    monkeypatch.setenv("WHATSAPP_OPS_QUEPASA_API_KEY", "secret-token")
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req)
        if req.full_url.startswith("https://quepasa.wesleion.com/useridentifier?"):
            return _FakeResponse('{"success":true,"status":"ok","phone":"5511999990000","lid":"172185238905034@lid"}')
        return _FakeResponse('{"success":true,"status":"created","groupinfo":{"id":"120363999@g.us"}}')

    payload = {"group_create": {"title": "Grupo Teste", "participants": ["172185238905034@lid"]}}
    with patch("urllib.request.urlopen", fake_urlopen):
        result = create_group_via_quepasa(
            payload=payload,
            config={"quepasa": {"send_enabled": True, "group_create_enabled": True, "send_url": "https://quepasa.wesleion.com"}},
        )

    assert result["ok"] is True
    assert len(seen) == 2
    assert seen[0].get_method() == "GET"
    assert seen[0].full_url.startswith("https://quepasa.wesleion.com/useridentifier?")
    assert seen[1].full_url == "https://quepasa.wesleion.com/groups/create"
    assert _request_body(seen[1]) == {"title": "Grupo Teste", "participants": ["5511999990000"]}
    assert "172185238905034" not in json.dumps(result, ensure_ascii=False)
