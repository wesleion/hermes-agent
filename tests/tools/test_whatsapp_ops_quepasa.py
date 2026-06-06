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
