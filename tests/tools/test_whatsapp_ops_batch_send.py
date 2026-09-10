import json
from datetime import datetime, timedelta, timezone

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_canonical_sender_consumes_frozen_block_with_real_receipt_and_flags(tmp_path):
    from tools.whatsapp_ops_batch import (
        acquire_friends_conversation_lease, freeze_friends_message_plan,
        prepare_friends_envelope, trusted_activate_friends_grant,
    )
    from tools.whatsapp_ops_store import list_contact_channels, register_contact_local
    from tools.whatsapp_ops_tool import wpp_send_approved

    token = set_hermes_home_override(tmp_path)
    try:
        registered = [register_contact_local(alias=f"friend-{i}", raw_ref=f"5511888800{i}@s.whatsapp.net", allow_send=True) for i in range(3)]
        contacts = [{"contact_id": row["contact_id"], "channel_id": list_contact_channels(row["contact_id"])[0]["channel_id"]} for row in registered]
        now = datetime.now(timezone.utc)
        preview = prepare_friends_envelope(campaign_id="friends", contacts=contacts, offer={"offer": "a"}, playbook={"p": "a"}, issuer="telegram:op", starts_at=now.isoformat(), expires_at=(now + timedelta(minutes=10)).isoformat())
        grant = trusted_activate_friends_grant(preview, decision="approved", operator_identity="telegram:op")
        plan = freeze_friends_message_plan(grant["grant_id"], contact_id=contacts[0]["contact_id"], channel_id=contacts[0]["channel_id"], blocks=["frozen one", "frozen two"], action="offer", context_highwatermark="ctx", offer_digest="b" * 64)
        lease = acquire_friends_conversation_lease(plan["plan_id"])
        calls = []
        result = json.loads(wpp_send_approved("", config={"send_enabled": True, "kill_switch": False, "quepasa": {"send_enabled": True}, "friends_pilot": {"enabled": True}}, batch_plan_id=plan["plan_id"], batch_lease_fence=lease["fence"], send_client=lambda payload, cfg: calls.append(payload) or {"ok": True, "message_id": "provider-1"}))
        assert result["ok"] is True and result["status"] == "sent"
        assert calls[0]["message"] == "frozen one"
        no_flags = json.loads(wpp_send_approved("", config={"send_enabled": False, "kill_switch": False, "quepasa": {"send_enabled": False}, "friends_pilot": {"enabled": False}}, batch_plan_id=plan["plan_id"], batch_lease_fence=lease["fence"], send_client=lambda *_: (_ for _ in ()).throw(AssertionError("provider called"))))
        assert no_flags["ok"] is False
        assert len(calls) == 1
        resumed = json.loads(wpp_send_approved("", config={"send_enabled": True, "kill_switch": False, "quepasa": {"send_enabled": True}, "friends_pilot": {"enabled": True}}, batch_plan_id=plan["plan_id"], batch_lease_fence=lease["fence"], send_client=lambda payload, cfg: calls.append(payload) or {"ok": True, "message_id": "provider-2"}))
        assert resumed["ok"] is True and calls[1]["message"] == "frozen two"
    finally:
        reset_hermes_home_override(token)
