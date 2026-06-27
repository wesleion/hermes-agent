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


def test_wpp_group_create_approved_routes_to_executor_with_resolved_raw_participants(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval, upsert_contact
    from tools.whatsapp_ops_tool import wpp_send_approved

    monkeypatch.setenv(
        "CONTACTS_JSON",
        json.dumps([
            {
                "alias": "Alpha",
                "display_name": "Alpha Lead",
                "target_ref": "5511999990000@s.whatsapp.net",
                "kind": "contact",
                "allow_send": True,
            }
        ]),
    )
    send_client = Mock(return_value={"ok": True, "transport": "quepasa_direct_group_create", "group_ref_hash": "abc123"})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True, "group_create_enabled": True},
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        upsert_contact(contact_id="c_1", display_name="Alpha Lead", aliases=["Alpha"], whitelisted=True)
        draft = create_draft(
            targets=[{"type": "group_create", "name": "Grupo Teste", "member_aliases": ["Alpha"]}],
            message="AÇÃO WHATSAPP OPS — CRIAR GRUPO",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(wpp_send_approved(draft["draft_id"], config=config, send_client=send_client))
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    payload, cfg = send_client.call_args.args
    assert payload["group_create"] == {
        "title": "Grupo Teste",
        "participants": ["5511999990000@s.whatsapp.net"],
    }
    assert cfg["quepasa"]["group_create_enabled"] is True
    # Raw participant refs go only to the transport client, not back to model/user output.
    assert "5511999990000" not in json.dumps(result, ensure_ascii=False)


def test_wpp_group_create_rejects_when_group_flag_disabled(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval, upsert_contact
    from tools.whatsapp_ops_tool import wpp_send_approved

    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True, "group_create_enabled": False},
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        upsert_contact(contact_id="c_1", display_name="Alpha", aliases=["Alpha"], whitelisted=True)
        draft = create_draft(
            targets=[{"type": "group_create", "name": "Grupo Teste", "member_aliases": ["Alpha"]}],
            message="AÇÃO WHATSAPP OPS — CRIAR GRUPO",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(wpp_send_approved(draft["draft_id"], config=config, send_client=send_client))
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is False
    assert "quepasa_group_create_disabled" in result["reasons"]
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


def test_wpp_registration_staging_status_has_diagnostics_and_profile_ttl(tmp_path):
    from datetime import datetime

    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import (
        wpp_ingest_inbound_event,
        wpp_register_alias,
        wpp_register_staging_status,
    )

    (tmp_path / "config.yaml").write_text(
        "whatsapp_ops:\n"
        "  registration_staging_ttl_seconds: 86400\n"
    )
    payload = {
        "id": "REG_MSG_001",
        "chat": {"id": "synthetic-contact@internal.invalid"},
        "participant": {"id": "synthetic-contact@internal.invalid", "title": "Lead Teste"},
        "text": "cadastro teste",
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        ingested = _parse(wpp_ingest_inbound_event(payload))
        status = _parse(wpp_register_staging_status())
        registered = _parse(wpp_register_alias(nome="Lead Teste"))
        empty_status = _parse(wpp_register_staging_status())
    finally:
        reset_hermes_home_override(token)

    assert ingested["ok"] is True
    assert status["ok"] is True
    assert status["staged_count"] == 1
    assert status["staging_ttl_seconds"] == 86400
    assert status["empty_reason"] == "none"
    created = datetime.fromisoformat(status["staged"][0]["created_at"])
    available = datetime.fromisoformat(status["staged"][0]["available_until"])
    assert (available - created).total_seconds() >= 23 * 60 * 60
    assert registered["ok"] is True
    assert empty_status["staged_count"] == 0
    assert empty_status["inbound_count"] == 1
    assert empty_status["empty_reason"] == "no_active_staging_or_expired"
    serialized = json.dumps({"status": status, "registered": registered, "empty": empty_status}, ensure_ascii=False)
    assert "synthetic-contact@internal.invalid" not in serialized
    assert "@internal.invalid" not in serialized



def test_wpp_registration_staging_is_actionable_for_group_and_participant(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_ingest_inbound_event, wpp_register_staging_status

    payload = {
        "id": "REG_GROUP_MSG_001",
        "chat": {"id": "120363430137938027@g.us", "subject": "Grupo Comercial Alpha"},
        "participant": {"id": "553199998765@s.whatsapp.net", "title": "João Cliente"},
        "text": "cadastro teste",
        "type": "text",
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        ingested = _parse(wpp_ingest_inbound_event(payload))
        status = _parse(wpp_register_staging_status())
    finally:
        reset_hermes_home_override(token)

    assert ingested["ok"] is True
    assert status["ok"] is True
    kinds = {item["kind"] for item in status["staged"]}
    assert {"group", "contact"}.issubset(kinds)
    group = next(item for item in status["staged"] if item["kind"] == "group")
    contact = next(item for item in status["staged"] if item["kind"] == "contact")
    assert group["display_name"] == "Grupo Comercial Alpha"
    assert group["safe_id"].startswith("grp_")
    assert group["last_message_type"] == "text"
    assert contact["display_name"] == "João Cliente"
    assert contact["safe_id"].startswith("ctt_")
    assert contact["phone_masked"].endswith("8765")
    assert contact["source_group_safe_id"] == group["safe_id"]
    serialized = json.dumps(status, ensure_ascii=False)
    assert "120363430137938027@g.us" not in serialized
    assert "553199998765" not in serialized
    assert "@s.whatsapp.net" not in serialized
    assert "@g.us" not in serialized



def test_wpp_registration_staging_deduplicates_and_counts_messages(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_ingest_inbound_event, wpp_register_staging_status

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        for idx in range(3):
            assert _parse(wpp_ingest_inbound_event({
                "id": f"REG_GROUP_DUP_{idx}",
                "chat": {"id": "120363430137938027@g.us", "subject": "Grupo Comercial Alpha"},
                "participant": {"id": "553199998765@s.whatsapp.net", "title": "João Cliente"},
                "type": "image" if idx == 1 else "text",
            }))["ok"] is True
        status = _parse(wpp_register_staging_status())
    finally:
        reset_hermes_home_override(token)

    groups = [item for item in status["staged"] if item["kind"] == "group"]
    contacts = [item for item in status["staged"] if item["kind"] == "contact"]
    assert len(groups) == 1
    assert len(contacts) == 1
    assert groups[0]["message_count"] == 3
    assert contacts[0]["message_count"] == 3
    assert groups[0]["last_message_type"] == "text"



def test_wpp_register_alias_can_consume_specific_staging_id(tmp_path):
    from tools.whatsapp_ops_store import hash_text, init_db
    from tools.whatsapp_ops_tool import (
        wpp_ingest_inbound_event,
        wpp_register_alias,
        wpp_register_staging_status,
    )

    chosen_ref = "553199992222@s.whatsapp.net"
    other_ref = "553199991111@s.whatsapp.net"
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        assert _parse(wpp_ingest_inbound_event({
            "id": "REG_CONTACT_OTHER",
            "chat": {"id": other_ref},
            "participant": {"id": other_ref, "title": "Outro Lead"},
            "type": "text",
        }))["ok"] is True
        assert _parse(wpp_ingest_inbound_event({
            "id": "REG_CONTACT_CHOSEN",
            "chat": {"id": chosen_ref},
            "participant": {"id": chosen_ref, "title": "Lead Escolhido"},
            "type": "text",
        }))["ok"] is True
        status = _parse(wpp_register_staging_status())
        chosen = next(item for item in status["staged"] if item.get("phone_masked", "").endswith("2222"))
        registered = _parse(wpp_register_alias(nome="Lead Operacional", tipo="contact", staging_id=chosen["staging_id"]))
        remaining = _parse(wpp_register_staging_status())
    finally:
        reset_hermes_home_override(token)

    assert registered["ok"] is True
    assert registered["contact_id"] == "contact_" + hash_text(chosen_ref)[:16]
    assert all(item["staging_id"] != chosen["staging_id"] for item in remaining["staged"])
    assert any(item.get("phone_masked", "").endswith("1111") for item in remaining["staged"])
    serialized = json.dumps({"registered": registered, "remaining": remaining}, ensure_ascii=False)
    assert chosen_ref not in serialized
    assert other_ref not in serialized
    assert "55319999" not in serialized



def test_wpp_ingest_inbound_event_supports_nested_quepasa_shape(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_ingest_inbound_event, wpp_inbound_lookup

    payload = {
        "event": "messages.upsert",
        "body": {
            "apikey": "synthetic-token-that-must-not-leak",
            "data": {
                "key": {
                    "id": "NESTED_MSG_001",
                    "remoteJid": "551188887777@s.whatsapp.net",
                    "fromMe": False,
                    "participant": "551177776666@s.whatsapp.net",
                },
                "messageType": "conversation",
                "message": {"conversation": "Lead nested QuePasa"},
            },
        },
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        ingested = _parse(wpp_ingest_inbound_event(payload))
        result = _parse(wpp_inbound_lookup(thread="551188887777@s.whatsapp.net"))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert ingested["ok"] is True
    assert len(result["events"]) == 1
    assert "Lead nested QuePasa" in serialized
    assert "NESTED_MSG_001" not in serialized
    assert "551188887777" not in serialized
    assert "551177776666" not in serialized
    assert "synthetic-token-that-must-not-leak" not in serialized


def test_wpp_ingest_inbound_event_supports_top_level_data_key_shape(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_ingest_inbound_event, wpp_inbound_lookup

    payload = {
        "event": "messages.upsert",
        "data": {
            "key": {
                "id": "TOP_LEVEL_MSG_001",
                "remoteJid": "551155554444@s.whatsapp.net",
                "fromMe": False,
                "participant": "551133332222@s.whatsapp.net",
            },
            "message": {"conversation": "Top level data QuePasa"},
        },
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        ingested = _parse(wpp_ingest_inbound_event(payload))
        result = _parse(wpp_inbound_lookup(thread="551155554444@s.whatsapp.net"))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert ingested["ok"] is True
    assert len(result["events"]) == 1
    assert "Top level data QuePasa" in serialized
    assert "TOP_LEVEL_MSG_001" not in serialized
    assert "551155554444" not in serialized
    assert "551133332222" not in serialized


def test_wpp_ingest_inbound_event_redacts_media_url_tokens(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_ingest_inbound_event, wpp_inbound_lookup

    payload = {
        "id": "MEDIA_MSG_001",
        "chat": {"id": "551122221111@s.whatsapp.net"},
        "participant": {"id": "551122221111@s.whatsapp.net"},
        "mediaUrl": "https://cdn.example.invalid/file.jpg?token=secret123&phone=5511999999999",
        "message": {"imageMessage": {"url": "https://cdn.example.invalid/img?auth=secret456&expires=999"}},
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        ingested = _parse(wpp_ingest_inbound_event(payload))
        result = _parse(wpp_inbound_lookup(contact="551122221111@s.whatsapp.net"))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert ingested["ok"] is True
    assert "secret123" not in serialized
    assert "secret456" not in serialized
    assert "5511999999999" not in serialized
    assert "cdn.example.invalid" not in serialized
    assert "<redacted-url>" in serialized


def test_wpp_sync_allowlist_tool_loads_infisical_env_safely(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_list_contacts, wpp_sync_allowlist

    monkeypatch.delenv("CONTACTS_JSON", raising=False)
    monkeypatch.delenv("GROUPS_JSON", raising=False)
    monkeypatch.delenv("ALIAS_MAP_JSON", raising=False)
    raw_target = "551188887777@s.whatsapp.net"
    monkeypatch.setenv(
        "WHATSAPP_OPS_ALLOWLIST_CONTACTS_JSON",
        json.dumps(
            [
                {
                    "alias": "weslei_ctt_teste",
                    "target_ref": raw_target,
                    "display_name": "Weslei Teste",
                    "allow_send": False,
                    "allow_receive": True,
                    "policy_group": "dm_test",
                }
            ]
        ),
    )

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        synced = _parse(wpp_sync_allowlist())
        contacts = _parse(wpp_list_contacts("weslei"))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps({"synced": synced, "contacts": contacts}, ensure_ascii=False)
    assert synced == {"ok": True, "source": "env", "contacts_synced": 1, "groups_synced": 0}
    assert contacts["contacts"][0]["display_name"] == "Weslei Teste"
    assert raw_target not in serialized
    assert "551188887777" not in serialized



def test_wpp_create_draft_resolves_alias_and_send_guardrail_uses_synced_allowlist(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import init_db, sync_allowlist_from_env
    from tools.whatsapp_ops_tool import wpp_create_draft, wpp_request_approval, wpp_resolve_approval, wpp_send_approved

    token = set_hermes_home_override(tmp_path)
    try:
        monkeypatch.setenv(
            "CONTACTS_JSON",
            json.dumps([
                {
                    "alias": "weslei_ctt_teste",
                    "target_ref": "172185238905034@lid",
                    "kind": "contact",
                    "allow_send": True,
                }
            ]),
        )
        monkeypatch.setenv("GROUPS_JSON", "[]")
        monkeypatch.setenv("ALIAS_MAP_JSON", "{}")
        init_db()
        assert sync_allowlist_from_env()["ok"] is True
        draft = _parse(wpp_create_draft(
            targets=[{"type": "contact", "ref": "weslei_ctt_teste"}],
            message="Mensagem com alias resolvido",
        ))
        approval = _parse(wpp_request_approval(draft["draft_id"]))
        monkeypatch.setenv("WHATSAPP_OPS_TRUSTED_APPROVAL_CONTEXT", "telegram_callback")
        resolved = _parse(wpp_resolve_approval(approval["approval_id"], "approved", approver_ref="telegram:test"))
        monkeypatch.delenv("WHATSAPP_OPS_TRUSTED_APPROVAL_CONTEXT", raising=False)
        send = _parse(wpp_send_approved(
            draft["draft_id"],
            config={
                "send_enabled": False,
                "kill_switch": False,
                "quepasa": {"send_enabled": False},
                "approval": {"required": True},
                "allowlists": {"contacts": [], "groups": []},
            },
        ))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps({"draft": draft, "send": send}, ensure_ascii=False)
    assert draft["ok"] is True
    assert resolved["ok"] is True
    assert send["ok"] is False
    assert "send_disabled" in send["reasons"]
    assert "quepasa_send_disabled" in send["reasons"]
    assert "target_not_whitelisted" not in send["reasons"]
    assert "@lid" not in serialized
    assert "172185238905034" not in serialized


def test_wpp_cockpit_overview_tool_returns_fail_closed_admin_summary(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, record_inbound_event, upsert_contact
    from tools.whatsapp_ops_tool import wpp_cockpit_overview

    monkeypatch.delenv("CONTACTS_JSON", raising=False)
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        upsert_contact(
            contact_id="172185238905034@lid",
            display_name="Weslei Legacy",
            aliases=["weslei_ctt_teste"],
            whitelisted=False,
            policy_group="dm_test",
        )
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "contact_safe", "display_name": "Safe"}],
            message="Mensagem cockpit",
        )
        create_approval(draft["draft_id"])
        record_inbound_event(
            source_event_id="evt-real-123",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={"id": "evt-real-123", "from": "172185238905034@lid", "body": "Olá cockpit"},
        )
        overview = _parse(wpp_cockpit_overview(limit=5))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(overview, ensure_ascii=False)
    assert overview["ok"] is True
    assert overview["send_flags"]["send_enabled"] is False
    assert overview["send_flags"]["quepasa_send_enabled"] is False
    assert overview["counts"]["contacts"] == 1
    assert overview["counts"]["inbound_events"] == 1
    assert overview["pending_approvals"]
    assert "@lid" not in serialized
    assert "@g.us" not in serialized
    assert "172185238905034" not in serialized
    assert "evt-real-123" not in serialized


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
