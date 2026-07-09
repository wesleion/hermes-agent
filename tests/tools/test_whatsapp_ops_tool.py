import json
from unittest.mock import Mock

from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def _parse(result):
    return json.loads(result)


def _allow_raw_contact(monkeypatch, contact_id="c_1", alias="c_1", raw_ref="5511999990000@s.whatsapp.net"):
    monkeypatch.setenv(
        "CONTACTS_JSON",
        json.dumps([
            {
                "alias": alias,
                "contact_id": contact_id,
                "display_name": alias,
                "target_ref": raw_ref,
                "kind": "contact",
                "allow_send": True,
            }
        ]),
    )


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


def test_wpp_create_draft_accepts_token_free_media_url_without_exposing_url(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_create_draft, wpp_request_approval, wpp_send_approved, wpp_status
    from tools.whatsapp_ops_store import resolve_approval

    _allow_raw_contact(monkeypatch)
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
    }
    send_client = Mock(return_value={"ok": True, "transport": "mock", "media_sent": True})
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        created = _parse(
            wpp_create_draft(
                targets=[{"type": "contact", "contact_id": "c_1"}],
                message="Segue o material combinado.",
                media={
                    "type": "document",
                    "url": "https://static.example.invalid/material.pdf",
                    "filename": "material.pdf",
                    "mime": "application/pdf",
                },
            )
        )
        approval = _parse(wpp_request_approval(created["draft_id"]))
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        sent = _parse(wpp_send_approved(created["draft_id"], config=config, send_client=send_client))
        status = _parse(wpp_status(created["draft_id"]))
    finally:
        reset_hermes_home_override(token)

    serialized_public = json.dumps([created, approval, sent, status], ensure_ascii=False)
    assert created["ok"] is True
    assert created["media"] == {
        "type": "document",
        "filename": "material.pdf",
        "mime": "application/pdf",
        "as_document": True,
        "url_present": True,
    }
    assert "https://static.example.invalid/material.pdf" not in serialized_public
    assert sent["ok"] is True
    payload = send_client.call_args.args[0]
    assert payload["media"]["url"] == "https://static.example.invalid/material.pdf"
    assert payload["media"]["as_document"] is True
    assert status["media"]["url_present"] is True


def test_wpp_send_approved_resolves_local_registered_contact_for_transport_only(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, register_contact_local, resolve_approval
    from tools.whatsapp_ops_tool import wpp_send_approved

    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": [], "groups": []},
        "quepasa": {"send_enabled": True},
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        contact = register_contact_local(
            alias="Lead Local",
            raw_ref="551188887777@s.whatsapp.net",
            allow_send=True,
        )
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": contact["contact_id"]}],
            message="Mensagem controlada para contato local",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(wpp_send_approved(draft["draft_id"], config=config, send_client=send_client))
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    assert send_client.call_args.args[0]["targets"] == [
        {"type": "contact", "contact_id": "551188887777@s.whatsapp.net"}
    ]
    assert "551188887777" not in json.dumps(result, ensure_ascii=False)


def test_wpp_send_approved_resolves_config_allowlist_ref_when_db_cache_has_only_safe_id(tmp_path):
    from tools.whatsapp_ops_store import (
        create_approval,
        create_draft,
        hash_text,
        init_db,
        resolve_approval,
        upsert_contact,
    )
    from tools.whatsapp_ops_tool import wpp_send_approved

    raw_ref = "551188887700@s.whatsapp.net"
    contact_id = "contact_" + hash_text(raw_ref)[:16]
    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        # Runtime config may still carry provider refs while SQLite has only the
        # safe contact id/hash. Transport must be able to bridge the two.
        "allowlists": {"contacts": [raw_ref], "groups": []},
        "quepasa": {"send_enabled": True},
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        upsert_contact(
            contact_id=contact_id,
            display_name="Lead Cache",
            phone_e164="",
            aliases=["lead_cache"],
            whitelisted=True,
            policy_group="pilot",
            metadata={"source": "test", "target_ref_hash": hash_text(raw_ref)},
        )
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": contact_id}],
            message="Mensagem controlada para contato com cache incompleto",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(wpp_send_approved(draft["draft_id"], config=config, send_client=send_client))
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    assert send_client.call_args.args[0]["targets"] == [{"type": "contact", "contact_id": raw_ref}]
    assert "551188887700" not in json.dumps(result, ensure_ascii=False)


def test_wpp_import_contact_list_sanitizes_and_defaults_no_send(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import (
        wpp_import_contact_list,
        wpp_list_contact_segment_members,
        wpp_list_contact_segments,
        wpp_send_approved,
    )

    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": [], "groups": []},
        "quepasa": {"send_enabled": True},
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        imported = _parse(wpp_import_contact_list(
            list_name="Leads Junho",
            contacts=[{"alias": "Lead A", "phone": "+55 11 7777-1234"}],
        ))
        segments = _parse(wpp_list_contact_segments())
        members = _parse(wpp_list_contact_segment_members(imported["list_id"]))
        contact_id = imported["contacts"][0]["contact_id"]
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": contact_id}],
            message="Draft para lead importado sem allow_send",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        send_result = _parse(wpp_send_approved(draft["draft_id"], config=config, send_client=send_client))
    finally:
        reset_hermes_home_override(token)

    public = json.dumps([imported, segments, members, send_result], ensure_ascii=False)
    assert imported["ok"] is True
    assert imported["imported_count"] == 1
    assert segments["segments"][0]["member_count"] == 1
    assert members["contacts"][0]["whitelisted"] is False
    assert "551177771234" not in public
    assert "@s.whatsapp.net" not in public
    assert send_result["ok"] is False
    assert "target_not_whitelisted" in send_result["reasons"]
    send_client.assert_not_called()



def test_wpp_create_draft_rejects_persistent_media_blobs_and_token_urls(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_create_draft

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        data_url = _parse(
            wpp_create_draft(
                targets=[{"type": "contact", "contact_id": "c_1"}],
                message="blob não pode persistir",
                media={"type": "image", "content": "data:image/png;base64,AAAA"},
            )
        )
        token_url = _parse(
            wpp_create_draft(
                targets=[{"type": "contact", "contact_id": "c_1"}],
                message="url com token não pode persistir",
                media={"type": "document", "url": "https://static.example.invalid/a.pdf?token=secret"},
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert data_url == {"ok": False, "error": "media_content_not_allowed_in_draft"}
    assert token_url == {"ok": False, "error": "media_url_must_be_token_free"}



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


def test_wpp_send_approved_uses_profile_config_when_no_explicit_config(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    _allow_raw_contact(monkeypatch)
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


def test_wpp_send_approved_records_success_for_idempotency(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    _allow_raw_contact(monkeypatch)
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


def test_wpp_send_approved_humanized_sequence_splits_text_and_preserves_idempotency(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_send_approved

    _allow_raw_contact(monkeypatch)
    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    sleep_fn = Mock()
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
        "humanized_send": {"enabled": True, "delay_seconds": 1.25, "max_blocks": 4, "max_block_chars": 360},
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message=(
                "Oi, João. Vi aqui o contexto do projeto e acho que faz sentido conversarmos.\n\n"
                "A ideia é entender onde estão os gargalos hoje e ver se conseguimos simplificar a operação sem aumentar o time.\n\n"
                "Se fizer sentido, posso te mandar uma proposta de diagnóstico rápido."
            ),
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        first = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                config=config,
                send_client=send_client,
                sleep_fn=sleep_fn,
            )
        )
        second = _parse(wpp_send_approved(draft_id=draft["draft_id"], config=config, send_client=send_client, sleep_fn=sleep_fn))
    finally:
        reset_hermes_home_override(token)

    assert first["ok"] is True
    assert first["send_result"]["transport"] == "humanized_sequence"
    assert first["send_result"]["blocks_sent"] == 3
    assert [call.args[0]["message"] for call in send_client.call_args_list] == [
        "Oi, João. Vi aqui o contexto do projeto e acho que faz sentido conversarmos.",
        "A ideia é entender onde estão os gargalos hoje e ver se conseguimos simplificar a operação sem aumentar o time.",
        "Se fizer sentido, posso te mandar uma proposta de diagnóstico rápido.",
    ]
    assert sleep_fn.call_count == 2
    sleep_fn.assert_any_call(1.25)
    assert second["ok"] is False
    assert "idempotency_duplicate" in second["reasons"]


def test_wpp_send_approved_humanized_sequence_uses_adaptive_delay_by_block_size(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_send_approved

    _allow_raw_contact(monkeypatch)
    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    sleep_fn = Mock()
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
        "humanized_send": {
            "enabled": True,
            "delay_mode": "adaptive",
            "min_delay_seconds": 0.5,
            "max_delay_seconds": 4.0,
            "chars_per_second": 20,
            "max_blocks": 4,
            "max_block_chars": 360,
        },
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message=(
                "Curto.\n\n"
                "Segundo bloco tem tamanho intermediário para simular digitação.\n\n"
                "Terceiro bloco é propositalmente maior, com mais contexto, vírgulas e detalhes para saturar o teto adaptativo."
            ),
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(wpp_send_approved(draft["draft_id"], config=config, send_client=send_client, sleep_fn=sleep_fn))
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    assert result["send_result"]["delay_mode"] == "adaptive"
    sleeps = [call.args[0] for call in sleep_fn.call_args_list]
    assert len(sleeps) == 2
    assert sleeps[0] < sleeps[1]
    assert sleeps[0] >= 0.5
    assert sleeps[1] <= 4.0


def test_wpp_send_approved_humanized_sequence_auto_blocks_can_use_five_blocks(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_send_approved

    _allow_raw_contact(monkeypatch)
    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    sleep_fn = Mock()
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
        "humanized_send": {
            "enabled": True,
            "delay_mode": "adaptive",
            "min_delay_seconds": 0.1,
            "max_delay_seconds": 1.0,
            "chars_per_second": 80,
            "max_blocks": "auto",
            "max_blocks_max": 5,
            "target_block_chars": 180,
            "max_block_chars": 360,
        },
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message=(
                "Bloco um contextual.\n\n"
                "Bloco dois com uma ideia própria.\n\n"
                "Bloco três avança a conversa.\n\n"
                "Bloco quatro mantém naturalidade.\n\n"
                "Bloco cinco fecha com call to action."
            ),
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(wpp_send_approved(draft["draft_id"], config=config, send_client=send_client, sleep_fn=sleep_fn))
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    assert result["send_result"]["max_blocks_mode"] == "dynamic"
    assert result["send_result"]["blocks_sent"] == 5
    assert result["send_result"]["max_blocks_cap"] == 5
    assert send_client.call_count == 5
    assert sleep_fn.call_count == 4


def test_wpp_send_approved_humanized_sequence_auto_blocks_caps_tail_at_five(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_send_approved

    _allow_raw_contact(monkeypatch)
    send_client = Mock(return_value={"ok": True, "transport": "mock"})
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
        "humanized_send": {
            "enabled": True,
            "delay_seconds": 0,
            "max_blocks": "auto",
            "max_blocks_max": 5,
            "target_block_chars": 120,
            "max_block_chars": 360,
        },
    }
    message = "\n\n".join(f"Bloco {idx} com texto natural." for idx in range(1, 8))

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(targets=[{"type": "contact", "contact_id": "c_1"}], message=message)
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(wpp_send_approved(draft["draft_id"], config=config, send_client=send_client))
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    assert result["send_result"]["blocks_sent"] == 5
    assert send_client.call_count == 5
    assert "Bloco 6" in send_client.call_args_list[-1].args[0]["message"]
    assert "Bloco 7" in send_client.call_args_list[-1].args[0]["message"]


def test_wpp_send_approved_humanized_sequence_sends_typing_presence_before_blocks(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_send_approved

    _allow_raw_contact(monkeypatch)
    events = []

    def send_client(payload, _cfg):
        events.append(("send", payload["humanized"]["block_index"], payload["message"]))
        return {"ok": True, "transport": "mock"}

    def presence_client(payload, _cfg):
        events.append(("presence", payload["humanized"]["block_index"], payload["presence_type"], payload["duration_ms"]))
        return {"ok": True, "transport": "presence_mock"}

    def sleep_fn(seconds):
        events.append(("sleep", seconds))

    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
        "humanized_send": {
            "enabled": True,
            "delay_mode": "adaptive",
            "min_delay_seconds": 0.25,
            "max_delay_seconds": 1.0,
            "chars_per_second": 80,
            "max_blocks": 4,
            "max_block_chars": 360,
            "typing": {"enabled": True, "presence_type": "text"},
        },
    }

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Primeiro bloco.\n\nSegundo bloco com mais conteúdo.",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(
            wpp_send_approved(
                draft["draft_id"],
                config=config,
                send_client=send_client,
                sleep_fn=sleep_fn,
                presence_client=presence_client,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    assert result["send_result"]["typing_attempted"] is True
    assert [event[0] for event in events] == ["presence", "sleep", "send", "presence", "sleep", "send"]
    assert events[0][1:3] == (1, "text")
    assert events[3][1:3] == (2, "text")


def test_wpp_send_approved_humanized_sequence_does_not_split_media_payload(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_send_approved

    _allow_raw_contact(monkeypatch)
    send_client = Mock(return_value={"ok": True, "transport": "mock", "media_sent": True})
    sleep_fn = Mock()
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
        "humanized_send": {"enabled": True, "delay_seconds": 1, "max_blocks": 4, "max_block_chars": 80},
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Legenda curta.\n\nOutra frase que seria dividida se fosse texto puro.",
            media={"type": "document", "url": "https://static.example.invalid/a.pdf", "filename": "a.pdf", "as_document": True},
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(wpp_send_approved(draft["draft_id"], config=config, send_client=send_client, sleep_fn=sleep_fn))
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    send_client.assert_called_once()
    sleep_fn.assert_not_called()
    assert send_client.call_args.args[0]["media"]["url"] == "https://static.example.invalid/a.pdf"


def test_wpp_send_approved_humanized_sequence_stops_on_failed_block(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_send_approved, wpp_status

    _allow_raw_contact(monkeypatch)
    send_client = Mock(side_effect=[{"ok": True, "transport": "mock"}, {"ok": False, "error": "http_error"}])
    sleep_fn = Mock()
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
        "humanized_send": {"enabled": True, "delay_seconds": 0.5, "max_blocks": 4, "max_block_chars": 160},
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message=(
                "Primeiro bloco controlado com contexto suficiente para passar do limite mínimo e sair sozinho.\n\n"
                "Segundo bloco falha de propósito, também longo o bastante para não ser agrupado ao anterior.\n\n"
                "Terceiro bloco não deve sair porque a sequência precisa parar na falha do segundo."
            ),
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        resolve_approval(approval["approval_id"], "approved", approver_ref="test:approver")
        result = _parse(wpp_send_approved(draft["draft_id"], config=config, send_client=send_client, sleep_fn=sleep_fn))
        status = _parse(wpp_status(draft["draft_id"]))
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is False
    assert result["send_result"]["transport"] == "humanized_sequence"
    assert result["send_result"]["failed_block_index"] == 2
    assert send_client.call_count == 2
    sleep_fn.assert_called_once_with(0.5)
    assert status["status"] == "failed"


def test_wpp_send_approved_wrong_token_does_not_consume_idempotency(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    _allow_raw_contact(monkeypatch)
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


def test_wpp_send_approved_marks_draft_failed_when_transport_fails(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, resolve_approval
    from tools.whatsapp_ops_tool import wpp_request_approval, wpp_resolve_approval, wpp_send_approved, wpp_status

    _allow_raw_contact(monkeypatch)
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
    keyboard = payload["reply_markup"]["inline_keyboard"][0]
    assert keyboard[0]["text"] == "✅ Aprovar e Enviar"
    assert keyboard[0]["callback_data"] == f"wpp:a:{approval['approval_id']}"
    assert keyboard[1]["text"] == "✏️ Editar"
    assert keyboard[1]["callback_data"] == f"wpp:e:{approval['approval_id']}"
    assert keyboard[2]["text"] == "❌ Negar"
    assert keyboard[2]["callback_data"] == f"wpp:d:{approval['approval_id']}"
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

    _allow_raw_contact(monkeypatch)
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
    assert "+352****6457" in serialized
    assert "2026-06-06T12:37:34-03:00" in serialized


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



def test_wpp_ingest_lid_participant_without_explicit_phone_is_unresolved(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_ingest_inbound_event, wpp_register_staging_status

    payload = {
        "id": "REG_GROUP_LID_NO_PHONE_001",
        "chat": {"id": "120363430137938027@g.us", "title": "Grupo Comercial Alpha"},
        "participant": {"id": "172185238905034@lid", "title": "Weslei W."},
        "text": "cadastro teste lid",
        "type": "text",
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        ingested = _parse(wpp_ingest_inbound_event(payload))
        status = _parse(wpp_register_staging_status())
    finally:
        reset_hermes_home_override(token)

    contact = next(item for item in status["staged"] if item["kind"] == "contact")
    serialized = json.dumps(status, ensure_ascii=False)
    assert ingested["ok"] is True
    assert contact["display_name"] == "Weslei W."
    assert contact["phone_status"] == "unresolved"
    assert contact["identity_note"] == "número não resolvido"
    assert "phone_masked" not in contact
    assert "last4" not in contact
    assert "+17***5034" not in serialized
    assert "172185238905034" not in serialized
    assert "120363430137938027" not in serialized
    assert "@lid" not in serialized
    assert "@g.us" not in serialized


def test_wpp_ingest_lid_participant_uses_explicit_phone_field_when_present(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_ingest_inbound_event, wpp_register_staging_status

    payload = {
        "id": "REG_GROUP_LID_PHONE_001",
        "chat": {"id": "120363430137938027@g.us", "title": "Grupo Comercial Alpha"},
        "participant": {"id": "172185238905034@lid", "phone": "553199998765", "title": "João Cliente"},
        "text": "cadastro teste phone",
        "type": "text",
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        ingested = _parse(wpp_ingest_inbound_event(payload))
        status = _parse(wpp_register_staging_status())
    finally:
        reset_hermes_home_override(token)

    contact = next(item for item in status["staged"] if item["kind"] == "contact")
    serialized = json.dumps(status, ensure_ascii=False)
    assert ingested["ok"] is True
    assert contact["display_name"] == "João Cliente"
    assert contact["phone_masked"] == "+55***8765"
    assert contact["last4"] == "8765"
    assert "phone_status" not in contact
    assert "553199998765" not in serialized
    assert "172185238905034" not in serialized
    assert "@lid" not in serialized


def test_wpp_ingest_system_events_do_not_create_registration_staging(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_actionable_queue, wpp_ingest_inbound_event, wpp_register_staging_status

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        ingested = _parse(wpp_ingest_inbound_event({
            "id": "REG_SYSTEM_EVENT_001",
            "chat": {"id": "system@g.us", "title": "Internal System Message"},
            "text": '{"event":"connected","phone":"+5511999990000","timestamp":"2026-07-08T19:20:33Z"}',
            "type": "system",
            "fromme": False,
        }))
        status = _parse(wpp_register_staging_status())
        queue = _parse(wpp_actionable_queue(limit=5))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps({"status": status, "queue": queue}, ensure_ascii=False)
    assert ingested["ok"] is True
    assert status["staged_count"] == 0
    assert queue["counts"]["active_staging"] == 0
    assert queue["counts"]["registration_items"] == 0
    assert queue["operator_summary"]["latest_inbound_created_at"] is None
    assert "Internal System Message" not in serialized
    assert "+5511999990000" not in serialized


def test_wpp_registration_staging_ignores_self_contact_from_group(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_actionable_queue, wpp_ingest_inbound_event, wpp_register_staging_status

    payload = {
        "id": "REG_GROUP_SELF_MSG_001",
        "chat": {"id": "120363430137938027@g.us", "title": "Grupo Comercial Alpha"},
        "participant": {"id": "553199998765@s.whatsapp.net", "title": "Weslei ON"},
        "text": "Mensagem enviada pelo próprio número conectado",
        "type": "text",
        "fromme": True,
    }
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        ingested = _parse(wpp_ingest_inbound_event(payload))
        status = _parse(wpp_register_staging_status())
        queue = _parse(wpp_actionable_queue(limit=5))
    finally:
        reset_hermes_home_override(token)

    assert ingested["ok"] is True
    assert status["ok"] is True
    staged = status["staged"]
    assert [item["kind"] for item in staged] == ["group"]
    assert staged[0]["display_name"] == "Grupo Comercial Alpha"
    assert all(item["kind"] != "contact" for item in queue["items"])
    assert all(item["kind"] != "context" for item in queue["items"])
    group_item = next(item for item in queue["items"] if item.get("subtype") == "group")
    assert group_item["recent_messages"][-1]["from_self"] is True
    assert group_item["recent_messages"][-1]["direction"] == "eu"
    assert group_item["recent_messages"][-1]["sender_label"] == "Eu"
    assert "Mensagem enviada" in group_item["recent_messages"][-1]["text_preview"]
    assert group_item["ignore_action"] == "/ignorar 1"
    serialized = json.dumps({"status": status, "queue": queue}, ensure_ascii=False)
    assert "Weslei ON" not in serialized
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



def test_wpp_queue_recent_messages_show_sender_name_and_masked_phone(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_actionable_queue, wpp_ingest_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        assert _parse(wpp_ingest_inbound_event({
            "id": "REG_GROUP_SENDER_LABEL_001",
            "chat": {"id": "120363430137938027@g.us", "title": "Grupo Comercial Alpha"},
            "participant": {
                "id": "553199998765@s.whatsapp.net",
                "phone": "553199998765",
                "title": "Studio Wanda Wanderlei",
            },
            "text": "@553199991111 (Weslei ON), como faz pra acessar a conta de anúncios?",
            "type": "text",
        }))["ok"] is True
        queue = _parse(wpp_actionable_queue(limit=5))
    finally:
        reset_hermes_home_override(token)

    group_item = next(item for item in queue["items"] if item.get("subtype") == "group")
    message = group_item["recent_messages"][-1]
    assert message["sender_label"] == "Studio Wanda Wanderlei (+55***8765)"
    assert message["sender_display_name"] == "Studio Wanda Wanderlei"
    assert message["sender_phone_masked"] == "+55***8765"
    assert "@Weslei ON" in message["text_preview"]
    assert "<redacted-phone>" not in message["text_preview"]
    serialized = json.dumps(queue, ensure_ascii=False)
    assert "553199998765" not in serialized
    assert "553199991111" not in serialized
    assert "@s.whatsapp.net" not in serialized
    assert "@g.us" not in serialized


def test_wpp_ignore_staging_item_removes_and_suppresses_future_rows(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_actionable_queue, wpp_ignore_staging_item, wpp_ingest_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        first_payload = {
            "id": "REG_GROUP_IGNORE_001",
            "chat": {"id": "120363430137938027@g.us", "subject": "Grupo Comercial Alpha"},
            "participant": {"id": "553199998765@s.whatsapp.net", "title": "João Cliente"},
            "text": "primeira mensagem",
            "type": "text",
        }
        assert _parse(wpp_ingest_inbound_event(first_payload))["ok"] is True
        before = _parse(wpp_actionable_queue(limit=5))
        ignored = _parse(wpp_ignore_staging_item(item=1))
        after = _parse(wpp_actionable_queue(limit=5))
        second_payload = dict(first_payload, id="REG_GROUP_IGNORE_002", text="segunda mensagem")
        assert _parse(wpp_ingest_inbound_event(second_payload))["ok"] is True
        after_reingest = _parse(wpp_actionable_queue(limit=5))
    finally:
        reset_hermes_home_override(token)

    assert any(item.get("kind") == "registration" for item in before["items"])
    assert ignored["ok"] is True
    assert ignored["ignored"] is True
    assert ignored["send_performed"] is False
    assert not any(item.get("staging_id") == ignored["staging_id"] for item in after["items"])
    assert not any(item.get("staging_id") == ignored["staging_id"] for item in after_reingest["items"])
    serialized = json.dumps({"ignored": ignored, "after": after_reingest}, ensure_ascii=False)
    assert "@g.us" not in serialized
    assert "@s.whatsapp.net" not in serialized
    assert "553199998765" not in serialized


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


def test_wpp_ingest_inbound_event_redacts_embedded_urls_and_media_blobs(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_ingest_inbound_event, wpp_inbound_lookup

    payload = {
        "id": "MEDIA_TEXT_MSG_001",
        "chat": {"id": "551122221111@s.whatsapp.net"},
        "participant": {"id": "551122221111@s.whatsapp.net"},
        "text": (
            "Veja https://cdn.example.invalid/file.jpg?token=secret123 "
            "e data:image/png;base64,AAAAABBBBBCCCCCDDDDDEEEEEFFFFFGGGGGHHHHH "
            "ref 551122221111:17@s.whatsapp.net telefone 5511999999999"
        ),
        "attachment": {
            "mime": "image/png",
            "thumbnail": {
                "urlprefix": "data:image/png;base64,",
                "data": "AAAAABBBBBCCCCCDDDDDEEEEEFFFFFGGGGGHHHHH",
            },
        },
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
    assert "cdn.example.invalid" not in serialized
    assert "secret123" not in serialized
    assert "data:image" not in serialized
    assert "AAAAABBBBB" not in serialized
    assert "5511999999999" not in serialized
    assert "551122221111" not in serialized
    assert "@s.whatsapp.net" not in serialized
    assert "<redacted-url>" in serialized
    assert "+55***9999" in serialized
    assert "<redacted-wa-ref>" in serialized



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


def test_wpp_actionable_queue_tool_returns_contextual_queue_without_raw_refs(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db, record_inbound_event, stage_raw_ref
    from tools.whatsapp_ops_tool import wpp_actionable_queue

    monkeypatch.delenv("CONTACTS_JSON", raising=False)
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "contact_safe", "display_name": "Safe"}],
            message="Texto sensível do draft não deve entrar na fila",
        )
        create_approval(draft["draft_id"])
        record_inbound_event(
            source_event_id="queue-tool-001",
            contact_ref="551199998888@s.whatsapp.net",
            thread_ref="120363375521827492@g.us",
            payload={
                "id": "queue-tool-001",
                "type": "image",
                "caption": "Imagem com telefone 551199998888 e https://cdn.example.invalid/i.jpg?token=secret",
                "message": {"imageMessage": {"mimetype": "image/jpeg", "url": "https://cdn.example.invalid/i.jpg"}},
            },
        )
        stage_raw_ref(
            contact_ref="551199998888@s.whatsapp.net",
            thread_ref="120363375521827492@g.us",
            display_name="Lead Foto",
            kind="contact",
            safe_hint={"display_name": "Lead Foto", "last_message_type": "image", "has_media": True},
        )
        result = _parse(wpp_actionable_queue(limit=5))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is True
    assert result["send_flags"]["send_enabled"] is False
    assert result["read_only"] is True
    assert any(item["kind"] == "approval" for item in result["items"])
    assert any(item["kind"] == "registration" for item in result["items"])
    assert any(item["kind"] == "context" for item in result["items"])
    assert "wpp_send_approved" in result["operator_actions"]
    assert "@s.whatsapp.net" not in serialized
    assert "@g.us" not in serialized
    assert "551199998888" not in serialized
    assert "120363375521827492" not in serialized
    assert "queue-tool-001" not in serialized
    assert "cdn.example.invalid" not in serialized
    assert "secret" not in serialized
    assert "Texto sensível" not in serialized


def test_wpp_thread_context_tool_returns_operator_summary_without_raw_refs(tmp_path):
    from tools.whatsapp_ops_store import init_db, record_inbound_event
    from tools.whatsapp_ops_tool import wpp_thread_context

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        record_inbound_event(
            source_event_id="ctx-tool-001",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={
                "id": "ctx-tool-001",
                "type": "audio",
                "text": "Áudio recebido do 551199998888",
                "message": {"audioMessage": {"mimetype": "audio/ogg", "seconds": 3}},
            },
        )
        result = _parse(
            wpp_thread_context(
                thread="120363375521827492@g.us",
                mode="operator",
                limit=5,
            )
        )
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is True
    assert result["message_count"] == 1
    assert result["events"][0]["message_type"] == "audio"
    assert "wpp_transcribe_media" in result["events"][0]["suggested_actions"]
    assert "@lid" not in serialized
    assert "@g.us" not in serialized
    assert "172185238905034" not in serialized
    assert "120363375521827492" not in serialized
    assert "551199998888" not in serialized
    assert "ctx-tool-001" not in serialized


def test_wpp_resolve_conversation_target_tool_is_read_only_and_sanitized(tmp_path):
    from tools.whatsapp_ops_store import init_db, record_inbound_event, register_group_local
    from tools.whatsapp_ops_tool import wpp_resolve_conversation_target

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        raw_group = "120363375521827492@g.us"
        register_group_local(alias="H-Ops", raw_ref=raw_group, allow_send=False)
        record_inbound_event(
            source_event_id="resolve-tool-001",
            contact_ref="172185238905034@lid",
            thread_ref=raw_group,
            payload={"id": "resolve-tool-001", "type": "text", "text": "Olá H-Ops"},
        )
        result = _parse(wpp_resolve_conversation_target(query="H-Ops"))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is True
    assert result["target_kind"] == "group"
    assert result["target_label"] == "H-Ops"
    assert result["thread_filter_set"] is True
    assert "_thread_ref" not in result
    assert "@g.us" not in serialized
    assert "@lid" not in serialized
    assert "120363375521827492" not in serialized
    assert "172185238905034" not in serialized


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
        "wpp_inbound_burst_status",
        "wpp_resolve_conversation_target",
        "wpp_thread_context",
        "wpp_actionable_queue",
        "wpp_ingest_inbound_event",
    }.issubset(names)


def test_wpp_inbound_burst_status_tool_is_default_disabled_and_read_only(tmp_path):
    from tools.whatsapp_ops_store import init_db
    from tools.whatsapp_ops_tool import wpp_ingest_inbound_event, wpp_inbound_burst_status

    raw_group = "120363430137938027@g.us"
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        for idx, text in enumerate(("primeira", "segunda"), 1):
            assert _parse(wpp_ingest_inbound_event({
                "id": f"TOOL_BURST_{idx}",
                "chat": {"id": raw_group, "title": "Grupo Comercial Alpha"},
                "participant": {"id": "553199998765@s.whatsapp.net", "title": "Lead Rajada"},
                "type": "text",
                "text": text,
            }))["ok"] is True
        status = _parse(wpp_inbound_burst_status(thread=raw_group, limit=10))
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(status, ensure_ascii=False)
    assert status["ok"] is True
    assert status["read_only"] is True
    assert status["coalesce_config"]["enabled"] is False
    assert status["coalescing"]["window_seconds"] == 60
    assert status["coalescing"]["quorum"] == 2
    assert status["counts"]["quorum_bursts"] == 1
    assert status["draft_created"] is False
    assert status["send_performed"] is False
    assert status["crm_write_performed"] is False
    assert status["provider_history_used"] is False
    assert raw_group not in serialized
    assert "553199998765" not in serialized
    assert "TOOL_BURST" not in serialized


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
