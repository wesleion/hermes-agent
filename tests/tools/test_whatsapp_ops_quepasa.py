from unittest.mock import patch


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

    result = send_via_quepasa(
        payload={"message": "não enviar"},
        config={"quepasa": {"send_enabled": True}},
    )

    assert result == {"ok": False, "error": "quepasa_send_url_missing"}


def test_quepasa_client_does_not_call_http_when_disabled():
    from tools.whatsapp_ops_quepasa import send_via_quepasa

    with patch("urllib.request.urlopen") as urlopen:
        result = send_via_quepasa(
            payload={"message": "não enviar"},
            config={"quepasa": {"send_enabled": False, "send_url": "http://127.0.0.1/send"}},
        )

    assert result["ok"] is False
    urlopen.assert_not_called()
