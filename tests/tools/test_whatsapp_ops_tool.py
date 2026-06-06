import json
from unittest.mock import Mock

from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def _parse(result):
    return json.loads(result)


def test_wpp_create_draft_tool_creates_draft_without_send(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_create_draft

    send_client = Mock()
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        result = _parse(
            wpp_create_draft(
                targets=[{"type": "contact", "contact_id": "c_1"}],
                message="Smoke sem envio",
                send_client=send_client,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    assert result["status"] == "draft"
    assert result["draft_id"]
    send_client.assert_not_called()


def test_wpp_send_approved_rejects_with_send_enabled_false_and_does_not_call_http(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    send_client = Mock()
    config = {
        "send_enabled": False,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Bloqueado por send_enabled false",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                config=config,
                send_client=send_client,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is False
    assert "send_disabled" in result["reasons"]
    send_client.assert_not_called()


def test_wpp_send_approved_rejects_without_approval_token(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    send_client = Mock()
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Sem token",
        )
        result = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                approval_token="",
                config=config,
                send_client=send_client,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is False
    assert "approval_missing" in result["reasons"]
    send_client.assert_not_called()


def test_wpp_send_approved_uses_profile_config_when_no_explicit_config(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    (tmp_path / "config.yaml").write_text(
        "whatsapp_ops:\n"
        "  send_enabled: true\n"
        "  allowlists:\n"
        "    contacts:\n"
        "      - c_1\n"
        "    groups: []\n"
        "  quepasa:\n"
        "    send_enabled: true\n",
        encoding="utf-8",
    )

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Config do profile habilita envio mockado",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                send_client=send_client,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    send_client.assert_called_once()


def test_wpp_send_approved_records_success_for_idempotency(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Envio unico com idempotencia",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        first = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                config=config,
                send_client=send_client,
            )
        )
        second = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                config=config,
                send_client=send_client,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert first["ok"] is True
    assert second["ok"] is False
    assert "idempotency_duplicate" in second["reasons"]
    send_client.assert_called_once()


def test_wpp_send_approved_wrong_token_does_not_consume_idempotency(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Token errado nao consome idempotencia",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        wrong = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                approval_token="token-errado",
                config=config,
                send_client=send_client,
            )
        )
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        correct = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                config=config,
                send_client=send_client,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert wrong["ok"] is False
    assert "approval_missing" in wrong["reasons"]
    assert correct["ok"] is True
    send_client.assert_called_once()


def test_wpp_send_approved_marks_draft_failed_when_transport_fails(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    send_client = Mock(return_value={"ok": False, "error": "http_error", "status": 400})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Falha transporte marca failed",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                config=config,
                send_client=send_client,
            )
        )
        status = _parse(wpp_status(draft["draft_id"]))
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is False
    assert status["status"] == "failed"


def test_wpp_request_approval_does_not_expose_token_or_mark_approved(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Aprovação não vaza token",
        )
        approval = _parse(wpp_request_approval(draft["draft_id"]))
        status = _parse(wpp_status(draft["draft_id"]))
    finally:
        reset_hermes_home_override(token)

    assert approval["ok"] is True
    assert approval["approval_id"].startswith("approval_")
    assert approval["status"] == "pending"
    assert "approval_token" not in approval
    assert status["status"] == "pending_approval"
    assert status["approval"]["status"] == "pending"
    assert approval["notification"]["ok"] is False


def test_wpp_request_approval_sends_telegram_inline_card_without_plaintext_token(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_draft, init_db
    from tools.whatsapp_ops_tool import wpp_request_approval

    sent = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok": true, "result": {"message_id": 123}}'

    def fake_urlopen(req, timeout=0):
        sent["url"] = req.full_url
        sent["payload"] = json.loads(req.data.decode("utf-8"))
        sent["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret-token")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-100123")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "456")
    monkeypatch.setattr("tools.whatsapp_ops_tool.urllib.request.urlopen", fake_urlopen)
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Card inline sem token plaintext",
        )
        approval = _parse(wpp_request_approval(draft["draft_id"]))
    finally:
        reset_hermes_home_override(token)

    payload = sent["payload"]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert approval["ok"] is True
    assert approval["notification"] == {"ok": True, "message_id": "123"}
    assert payload["chat_id"] == "-100123"
    assert payload["message_thread_id"] == 456
    assert payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"wpp:a:{approval['approval_id']}"
    assert payload["reply_markup"]["inline_keyboard"][0][1]["callback_data"] == f"wpp:d:{approval['approval_id']}"
    assert "approval_token" not in approval
    assert "telegram-secret-token" not in serialized


def test_wpp_resolve_approval_public_tool_requires_trusted_context(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_draft, init_db
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_OPS_TRUSTED_APPROVAL_CONTEXT", raising=False)
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Modelo não pode se autoaprovar",
        )
        approval = _parse(wpp_request_approval(draft["draft_id"]))
        resolved = _parse(
            wpp_resolve_approval(
                approval_id=approval["approval_id"],
                decision="approved",
                approver_ref="telegram:12345",
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert resolved["ok"] is False
    assert resolved["error"] == "trusted_approval_context_required"


def test_wpp_resolve_approval_approve_then_send_separately(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
    }

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("WHATSAPP_OPS_TRUSTED_APPROVAL_CONTEXT", "telegram_callback")
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Aprovar separado do envio",
        )
        approval = _parse(wpp_request_approval(draft["draft_id"]))
        pending_send = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                config=config,
                send_client=send_client,
            )
        )
        resolved = _parse(
            wpp_resolve_approval(
                approval_id=approval["approval_id"],
                decision="approved",
                approver_ref="telegram:12345",
            )
        )
        sent = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                config=config,
                send_client=send_client,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert pending_send["ok"] is False
    assert "approval_missing" in pending_send["reasons"] or "approval_invalid" in pending_send["reasons"]
    assert resolved["ok"] is True
    assert resolved["status"] == "approved"
    assert sent["ok"] is True
    send_client.assert_called_once()


def test_wpp_resolve_approval_deny_blocks_send(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
    }

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("WHATSAPP_OPS_TRUSTED_APPROVAL_CONTEXT", "telegram_callback")
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Negado bloqueia",
        )
        approval = _parse(wpp_request_approval(draft["draft_id"]))
        denied = _parse(
            wpp_resolve_approval(
                approval_id=approval["approval_id"],
                decision="denied",
                approver_ref="telegram:12345",
            )
        )
        result = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                config=config,
                send_client=send_client,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert denied["ok"] is True
    assert denied["status"] == "denied"
    assert result["ok"] is False
    assert "draft_status_invalid" in result["reasons"]
    send_client.assert_not_called()


def test_wpp_inbound_lookup_reads_store_and_sanitizes(tmp_path):
    from tools.whatsapp_ops_store import init_db, record_inbound_event
    from tools.whatsapp_ops_tool import wpp_inbound_lookup

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        record_inbound_event(
            source_event_id="evt-real-456",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={"text": "Resposta real", "api_key": "secret-key"},
        )
        result = _parse(wpp_inbound_lookup(contact="172185238905034@lid"))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is True
    assert len(result["events"]) == 1
    assert "Resposta real" in serialized
    assert "evt-real-456" not in serialized
    assert "172185238905034@lid" not in serialized
    assert "secret-key" not in serialized


def test_wpp_ingest_inbound_event_records_quepasa_payload_safely(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_ingest_inbound_event, wpp_inbound_lookup

    payload = {
        "id": "3A994860A1E5E17C49C6",
        "timestamp": "2026-06-06T12:37:34-03:00",
        "type": "text",
        "chat": {"id": "120363430137938027@g.us", "title": "Grupo Real"},
        "participant": {"id": "157475059830806@lid", "phone": "+352****6457", "title": "Pessoa"},
        "text": "Oiii inbound QuePasa",
        "fromme": False,
        "wid": "554591119001:32@s.whatsapp.net",
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        ingested = _parse(wpp_ingest_inbound_event(payload))
        result = _parse(wpp_inbound_lookup(thread="120363430137938027@g.us"))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert ingested["ok"] is True
    assert len(result["events"]) == 1
    assert "Oiii inbound QuePasa" in serialized
    assert "3A994860A1E5E17C49C6" not in serialized
    assert "157475059830806@lid" not in serialized
    assert "120363430137938027@g.us" not in serialized
    assert "+352****6457" not in serialized


def test_whatsapp_ops_toolset_is_registered():
    import tools.whatsapp_ops_tool  # noqa: F401
    from tools.registry import registry

    names = set(registry.get_tool_names_for_toolset("whatsapp_ops"))

    assert {
        "wpp_resolve_contact",
        "wpp_list_contacts",
        "wpp_create_draft",
        "wpp_request_approval",
        "wpp_resolve_approval",
        "wpp_schedule_draft",
        "wpp_send_approved",
        "wpp_cancel",
        "wpp_status",
        "wpp_inbound_lookup",
        "wpp_ingest_inbound_event",
    }.issubset(names)


def test_wpp_resolve_and_list_contacts_use_sanitized_synthetic_seed(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.whatsapp_ops_store import init_db, upsert_contact
    from tools.whatsapp_ops_tool import wpp_list_contacts, wpp_resolve_contact

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        upsert_contact(
            contact_id="synthetic_alice",
            display_name="Alice Synthetic",
            phone_e164="+551****0000",
            aliases=["alice", "arquiteta teste"],
            whitelisted=True,
            metadata={"seed": "synthetic"},
        )

        resolved = _parse(wpp_resolve_contact("alice"))
        listed = _parse(wpp_list_contacts("synthetic"))
    finally:
        reset_hermes_home_override(token)

    assert resolved["ok"] is True
    assert resolved["ambiguous"] is False
    assert resolved["match"]["contact_id"] == "synthetic_alice"
    assert resolved["match"]["phone_masked"].startswith("+55")
    assert "99990000" not in json.dumps(resolved)
    assert listed["ok"] is True
    assert listed["contacts"][0]["contact_id"] == "synthetic_alice"
    assert "99990000" not in json.dumps(listed)
