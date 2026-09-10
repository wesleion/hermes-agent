from datetime import datetime, timedelta, timezone

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _grant(now):
    from gateway.whatsapp_ops_batch_approval import issue_authenticated_friends_authority
    from tools.whatsapp_ops_batch import activate_friends_pending, persist_friends_pending, prepare_friends_envelope
    from tools.whatsapp_ops_store import list_contact_channels, register_contact_local
    rows = [register_contact_local(alias=f"friend-{i}", raw_ref=f"5511888800{i}@s.whatsapp.net", allow_send=True) for i in range(3)]
    contacts = [{"contact_id": r["contact_id"], "channel_id": list_contact_channels(r["contact_id"])[0]["channel_id"]} for r in rows]
    preview = prepare_friends_envelope(campaign_id="friends-campaign", contacts=contacts, offer={"id":"offer-a"}, playbook={"id":"playbook-a"}, issuer="telegram:7", starts_at=now.isoformat(), expires_at=(now+timedelta(minutes=30)).isoformat())
    pending = persist_friends_pending(preview, profile_id="p", chat_id="42", thread_id="9", operator_id="7")
    authority = issue_authenticated_friends_authority(profile_id="p", chat_id="42", thread_id="9", operator_id="7", pending_id=pending["pending_id"], envelope_digest=pending["envelope_digest"])
    return activate_friends_pending(pending["pending_id"], decision="approved", authority=authority), contacts


def test_friends_grant_is_exactly_three_immutable_and_requires_callback_authority(tmp_path):
    from tools.whatsapp_ops_batch import friends_grant_status, prepare_friends_envelope, trusted_activate_friends_grant
    token = set_hermes_home_override(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        assert prepare_friends_envelope(campaign_id="x", contacts=[{"contact_id":f"a{n}","channel_id":f"b{n}"} for n in range(3)], offer={}, playbook={}, issuer="op", starts_at=now.isoformat(), expires_at=(now+timedelta(hours=3)).isoformat())["error"] == "grant_window_invalid"
        assert trusted_activate_friends_grant({}, decision="approved", operator_identity="telegram:7")["error"] == "trusted_callback_required"
        grant, _ = _grant(now)
        assert grant["ok"]
        safe = friends_grant_status(grant["grant_id"])
        assert safe["status"] == "active" and "contacts" not in safe and "issuer" not in safe
    finally:
        reset_hermes_home_override(token)


def test_friends_plan_reservation_fence_and_reply_cancel_remaining_blocks(tmp_path):
    from tools.whatsapp_ops_batch import acquire_friends_conversation_lease, freeze_friends_message_plan, note_friends_reply, reserve_friends_block
    token = set_hermes_home_override(tmp_path)
    try:
        grant, contacts = _grant(datetime.now(timezone.utc)); contact = contacts[0]
        plan = freeze_friends_message_plan(grant["grant_id"], contact_id=contact["contact_id"], channel_id=contact["channel_id"], blocks=["first","second"], action="offer", context_highwatermark="h1", offer_digest=grant["offer_digest"])
        lease = acquire_friends_conversation_lease(plan["plan_id"], lease_seconds=60)
        assert reserve_friends_block(plan["plan_id"], lease["fence"])["block"]["index"] == 1
        assert reserve_friends_block(plan["plan_id"], "wrong")["error"] == "lease_fence_invalid"
        assert note_friends_reply(contact["contact_id"], contact["channel_id"])["plans_cancelled"] == 1
        assert reserve_friends_block(plan["plan_id"], lease["fence"])["error"] == "plan_cancelled"
    finally:
        reset_hermes_home_override(token)
