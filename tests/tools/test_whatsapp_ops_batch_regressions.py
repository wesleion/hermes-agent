from datetime import datetime, timedelta, timezone

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _contacts():
    from tools.whatsapp_ops_store import list_contact_channels, register_contact_local
    rows = [register_contact_local(alias=f"friend-{i}", raw_ref=f"5511888800{i}@s.whatsapp.net", allow_send=True) for i in range(3)]
    return [{"contact_id": row["contact_id"], "channel_id": list_contact_channels(row["contact_id"])[0]["channel_id"]} for row in rows]


def _pending(contacts):
    from tools.whatsapp_ops_batch import persist_friends_pending, prepare_friends_envelope
    now = datetime.now(timezone.utc)
    preview = prepare_friends_envelope(campaign_id="friends", contacts=contacts, offer={"id": "offer"}, playbook={"id": "playbook"}, issuer="telegram:7", starts_at=now.isoformat(), expires_at=(now + timedelta(minutes=20)).isoformat())
    assert preview["ok"]
    return persist_friends_pending(preview, profile_id="p", chat_id="42", thread_id="9", operator_id="7")


def test_public_strings_cannot_activate_pending_grant(tmp_path):
    from tools.whatsapp_ops_batch import activate_friends_pending, trusted_activate_friends_grant
    token = set_hermes_home_override(tmp_path)
    try:
        pending = _pending(_contacts())
        assert trusted_activate_friends_grant(pending, decision="approved", operator_identity="telegram:7")["ok"] is False
        denied = activate_friends_pending(pending["pending_id"], decision="denied", authority="telegram:7")
        assert denied["ok"] is False
        assert activate_friends_pending(pending["pending_id"], decision="wat", authority=None)["error"] == "trusted_decision_required"
    finally:
        reset_hermes_home_override(token)


def test_freeze_rejects_forged_offer_and_suppresses_future_plans(tmp_path):
    from gateway.whatsapp_ops_batch_approval import issue_authenticated_friends_authority
    from tools.whatsapp_ops_batch import activate_friends_pending, freeze_friends_message_plan, note_friends_optout
    token = set_hermes_home_override(tmp_path)
    try:
        contacts = _contacts(); pending = _pending(contacts)
        authority = issue_authenticated_friends_authority(profile_id="p", chat_id="42", thread_id="9", operator_id="7", pending_id=pending["pending_id"], envelope_digest=pending["envelope_digest"])
        grant = activate_friends_pending(pending["pending_id"], decision="approved", authority=authority)
        assert grant["ok"]
        bad = freeze_friends_message_plan(grant["grant_id"], contact_id=contacts[0]["contact_id"], channel_id=contacts[0]["channel_id"], blocks=["exact"], action="offer", context_highwatermark="h1", offer_digest="a" * 64)
        assert bad["error"] == "offer_digest_mismatch"
        assert note_friends_optout(contacts[0]["contact_id"], contacts[0]["channel_id"])["ok"]
        suppressed = freeze_friends_message_plan(grant["grant_id"], contact_id=contacts[0]["contact_id"], channel_id=contacts[0]["channel_id"], blocks=["exact"], action="offer", context_highwatermark="h2", offer_digest=grant["offer_digest"])
        assert suppressed["error"] == "contact_suppressed"
    finally:
        reset_hermes_home_override(token)


def test_strict_boolean_guard_matrix_and_grant_wide_inflight(tmp_path):
    from gateway.whatsapp_ops_batch_approval import issue_authenticated_friends_authority
    from tools.whatsapp_ops_batch import acquire_friends_conversation_lease, activate_friends_pending, freeze_friends_message_plan, reserve_friends_block
    from tools.whatsapp_ops_policy import evaluate_friends_batch_guardrails
    token = set_hermes_home_override(tmp_path)
    try:
        for kill in ("true", 1, None, False):
            guard = evaluate_friends_batch_guardrails(config={"send_enabled": True, "kill_switch": kill, "quepasa": {"send_enabled": True}, "friends_pilot": {"enabled": True}}, contact_authorized=True, target_is_one_to_one=True, has_media=False)
            assert guard.allowed is (kill is False)
        contacts = _contacts(); pending = _pending(contacts)
        authority = issue_authenticated_friends_authority(profile_id="p", chat_id="42", thread_id="9", operator_id="7", pending_id=pending["pending_id"], envelope_digest=pending["envelope_digest"])
        grant = activate_friends_pending(pending["pending_id"], decision="approved", authority=authority)
        plans = [freeze_friends_message_plan(grant["grant_id"], contact_id=c["contact_id"], channel_id=c["channel_id"], blocks=["x"], action="offer", context_highwatermark=f"h{i}", offer_digest=grant["offer_digest"]) for i, c in enumerate(contacts[:2])]
        leases = [acquire_friends_conversation_lease(p["plan_id"]) for p in plans]
        assert reserve_friends_block(plans[0]["plan_id"], leases[0]["fence"])["ok"]
        assert reserve_friends_block(plans[1]["plan_id"], leases[1]["fence"])["error"] == "grant_block_in_flight"
    finally:
        reset_hermes_home_override(token)
