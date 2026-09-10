"""Integration boundaries missed by the first batch regression suite."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from gateway.whatsapp_ops_batch_approval import issue_authenticated_friends_authority
from tools import whatsapp_ops_batch as batch
from tools.whatsapp_ops_store import list_contact_channels, register_contact_local


@pytest.fixture
def campaign(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        rows = [
            register_contact_local(
                alias=f"actor-{i}",
                raw_ref=f"5511888800{i}@s.whatsapp.net",
                allow_send=True,
            )
            for i in range(3)
        ]
        contacts = [
            {
                "contact_id": r["contact_id"],
                "channel_id": list_contact_channels(r["contact_id"])[0]["channel_id"],
            }
            for r in rows
        ]
        now = datetime.now(timezone.utc)
        preview = batch.prepare_friends_envelope(
            campaign_id="fixture-batch",
            contacts=contacts,
            offer={"id": "offer"},
            playbook={"id": "book"},
            issuer="telegram:7",
            starts_at=now.isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
        )
        yield tmp_path, contacts, preview
    finally:
        reset_hermes_home_override(token)


def activate(campaign, thread: str | None = "9"):
    home, contacts, preview = campaign
    pending = batch.persist_friends_pending(
        preview, profile_id=str(home), chat_id="42", thread_id=thread, operator_id="7"
    )
    authority = issue_authenticated_friends_authority(
        profile_id=str(home),
        chat_id="42",
        thread_id=thread,
        operator_id="7",
        pending_id=pending["pending_id"],
        envelope_digest=pending["envelope_digest"],
    )
    return batch.activate_friends_pending(
        pending["pending_id"], decision="approved", authority=authority
    )


def freeze(campaign, grant, index=0, watermark="open", blocks=None):
    contact = campaign[1][index]
    return batch.freeze_friends_message_plan(
        grant["grant_id"],
        **contact,
        blocks=blocks or ["Same approved opening"],
        action="offer",
        context_highwatermark=watermark,
        offer_digest=grant["offer_digest"],
    )


def test_dm_without_topic_can_activate(campaign):
    assert activate(campaign, thread=None)["ok"]


def test_identical_openings_are_distinct_per_recipient_but_not_per_generated_text(
    campaign,
):
    grant = activate(campaign)
    plans = [freeze(campaign, grant, index=i) for i in range(3)]
    assert all(p["ok"] for p in plans)
    duplicate = freeze(campaign, grant, blocks=["Changed generated opening"])
    assert duplicate["error"] == "plan_duplicate"


def test_unreserved_plans_do_not_consume_messages_and_completed_lease_is_released(
    campaign,
):
    grant = activate(campaign)
    plans = [freeze(campaign, grant, watermark=f"h-{i}") for i in range(31)]
    first = plans[0]["plan_id"]
    lease = batch.acquire_friends_conversation_lease(first)
    reserved = batch.reserve_friends_block(first, lease["fence"])
    assert reserved["ok"], reserved
    assert batch.finish_friends_block(
        first, reserved["reservation_id"], outcome="sent", receipt="receipt-1"
    )["ok"]
    assert batch.acquire_friends_conversation_lease(plans[1]["plan_id"])["ok"]


def test_mutated_block_cannot_reach_reservation(campaign):
    grant = activate(campaign)
    plan = freeze(campaign, grant)
    lease = batch.acquire_friends_conversation_lease(plan["plan_id"])
    with batch._conn() as conn:
        conn.execute(
            "UPDATE friends_blocks SET text='injected' WHERE plan_id=?",
            (plan["plan_id"],),
        )
    result = batch.reserve_friends_block(plan["plan_id"], lease["fence"])
    assert result["ok"] is False
    assert result["error"] == "plan_integrity_invalid"


def test_turn_and_message_caps_span_plans_and_one_grant(campaign):
    grant = activate(campaign)
    for turn in range(10):
        plan = freeze(
            campaign,
            grant,
            watermark=f"turn-{turn}",
            blocks=[f"a-{turn}", f"b-{turn}", f"c-{turn}"],
        )
        lease = batch.acquire_friends_conversation_lease(plan["plan_id"])
        assert lease["ok"], lease
        for block in range(3):
            reserved = batch.reserve_friends_block(plan["plan_id"], lease["fence"])
            assert reserved["ok"], reserved
            assert batch.finish_friends_block(
                plan["plan_id"],
                reserved["reservation_id"],
                outcome="sent",
                receipt=f"receipt-{turn}-{block}",
            )["ok"]
    eleventh = freeze(campaign, grant, watermark="turn-11")
    lease = batch.acquire_friends_conversation_lease(eleventh["plan_id"])
    denied = batch.reserve_friends_block(eleventh["plan_id"], lease["fence"])
    assert denied["error"] in {"message_cap_reached", "turn_cap_reached"}
    assert batch.friends_grant_status(grant["grant_id"])["messages_reserved"] == 30


def test_optout_during_provider_preserves_receipt_without_reopening(campaign):
    grant = activate(campaign)
    plan = freeze(campaign, grant, blocks=["First", "Second"])
    lease = batch.acquire_friends_conversation_lease(plan["plan_id"])
    reserved = batch.reserve_friends_block(plan["plan_id"], lease["fence"])
    batch.note_friends_optout(**campaign[1][0])
    assert batch.finish_friends_block(
        plan["plan_id"],
        reserved["reservation_id"],
        outcome="sent",
        receipt="already-started",
    )["ok"]
    assert batch.reserve_friends_block(plan["plan_id"], lease["fence"])["ok"] is False
    assert (
        freeze(campaign, grant, watermark="after-stop")["error"] == "contact_suppressed"
    )


@pytest.mark.parametrize("bad", [None, 4, True, [], {}])
def test_malformed_contact_types_return_refusal_not_exception(campaign, bad):
    _, contacts, preview = campaign
    contacts = [dict(c) for c in contacts]
    contacts[0]["contact_id"] = bad
    value = batch.prepare_friends_envelope(
        campaign_id="bad",
        contacts=contacts,
        offer={},
        playbook={},
        issuer="op",
        starts_at=preview["envelope"]["starts_at"],
        expires_at=preview["envelope"]["expires_at"],
    )
    assert value["ok"] is False
