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
