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
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db
    from tools.whatsapp_ops_tool import wpp_send_approved

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
        result = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                approval_token=approval["approval_token"],
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
    from tools.whatsapp_ops_store import create_draft, init_db
    from tools.whatsapp_ops_tool import wpp_send_approved

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
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db
    from tools.whatsapp_ops_tool import wpp_send_approved

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
        result = _parse(
            wpp_send_approved(
                draft_id=draft["draft_id"],
                approval_token=approval["approval_token"],
                send_client=send_client,
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    send_client.assert_called_once()


def test_whatsapp_ops_toolset_is_registered():
    import tools.whatsapp_ops_tool  # noqa: F401
    from tools.registry import registry

    names = set(registry.get_tool_names_for_toolset("whatsapp_ops"))

    assert {
        "wpp_resolve_contact",
        "wpp_list_contacts",
        "wpp_create_draft",
        "wpp_request_approval",
        "wpp_schedule_draft",
        "wpp_send_approved",
        "wpp_cancel",
        "wpp_status",
        "wpp_inbound_lookup",
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
            phone_e164="+551199990000",
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
