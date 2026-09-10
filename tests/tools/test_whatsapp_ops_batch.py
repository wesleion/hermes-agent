from datetime import datetime, timedelta, timezone

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _prepared(now):
    from tools.whatsapp_ops_batch import prepare_friends_envelope

    contacts = [
        {"contact_id": f"contact_{n}", "channel_id": f"channel_{n}"}
        for n in range(1, 4)
    ]
    return prepare_friends_envelope(
        campaign_id="friends-campaign",
        contacts=contacts,
        offer={"id": "offer-a"},
        playbook={"id": "playbook-a"},
        issuer="telegram:operator",
        starts_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=30)).isoformat(),
    )


def test_friends_grant_is_exactly_three_immutable_and_requires_matching_trusted_operator(tmp_path):
    from tools.whatsapp_ops_batch import friends_grant_status, prepare_friends_envelope, trusted_activate_friends_grant

    token = set_hermes_home_override(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        preview = _prepared(now)
        assert preview["ok"] and preview["mutated"] is False
        assert prepare_friends_envelope(
            campaign_id="x", contacts=[{"contact_id": f"a{n}", "channel_id": f"b{n}"} for n in range(3)],
            offer={}, playbook={}, issuer="op", starts_at=now.isoformat(),
            expires_at=(now + timedelta(hours=3)).isoformat(),
        )["error"] == "grant_window_invalid"
        forged = trusted_activate_friends_grant(
            preview, decision="approved", operator_identity="telegram:forged",
        )
        assert forged["ok"] is False and forged["error"] == "operator_mismatch"
        activated = trusted_activate_friends_grant(
            preview, decision="approved", operator_identity="telegram:operator",
        )
        assert activated["ok"] is True
        safe = friends_grant_status(activated["grant_id"])
        assert safe["status"] == "active"
        assert "contacts" not in safe and "issuer" not in safe
    finally:
        reset_hermes_home_override(token)


def test_friends_plan_reservation_fence_and_reply_cancel_remaining_blocks(tmp_path):
    from tools.whatsapp_ops_batch import (
        acquire_friends_conversation_lease, freeze_friends_message_plan,
        note_friends_reply, reserve_friends_block, trusted_activate_friends_grant,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        grant = trusted_activate_friends_grant(
            _prepared(now), decision="approved", operator_identity="telegram:operator",
        )
        plan = freeze_friends_message_plan(
            grant["grant_id"], contact_id="contact_1", channel_id="channel_1",
            blocks=["first", "second"], action="offer", context_highwatermark="h1",
            offer_digest="a" * 64,
        )
        lease = acquire_friends_conversation_lease(plan["plan_id"], lease_seconds=60)
        first = reserve_friends_block(plan["plan_id"], lease["fence"])
        assert first["ok"] and first["block"]["index"] == 1
        assert reserve_friends_block(plan["plan_id"], "wrong")["error"] == "lease_fence_invalid"
        assert note_friends_reply("contact_1")["plans_cancelled"] == 1
        assert reserve_friends_block(plan["plan_id"], lease["fence"])["error"] == "plan_cancelled"
    finally:
        reset_hermes_home_override(token)
