from __future__ import annotations
from datetime import datetime, timedelta, timezone

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_inbound_is_queued_transactionally_then_frozen_and_sent(tmp_path):
    from gateway.whatsapp_ops_batch_approval import (
        issue_authenticated_friends_authority,
    )
    from gateway.whatsapp_ops_batch_dispatch import FriendsBatchDispatcher
    from tools.whatsapp_ops_batch import (
        activate_friends_pending,
        persist_friends_pending,
        prepare_friends_envelope,
    )
    from tools.whatsapp_ops_store import (
        list_contact_channels,
        record_inbound_event,
        register_contact_local,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        rows = [
            register_contact_local(
                alias=f"friend-{i}",
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
        preview = prepare_friends_envelope(
            campaign_id="friends-campaign",
            contacts=contacts,
            offer={"id": "offer-a"},
            playbook={"id": "playbook-a"},
            issuer="telegram:7",
            starts_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=30)).isoformat(),
            actions=["qualify"],
        )
        pending = persist_friends_pending(
            preview, profile_id="p", chat_id="42", thread_id="9", operator_id="7"
        )
        authority = issue_authenticated_friends_authority(
            profile_id="p",
            chat_id="42",
            thread_id="9",
            operator_id="7",
            pending_id=pending["pending_id"],
            envelope_digest=pending["envelope_digest"],
        )
        grant = activate_friends_pending(
            pending["pending_id"], decision="approved", authority=authority
        )
        assert record_inbound_event(
            source_event_id="event-1",
            contact_ref="x@lid",
            payload={"text": "Quero entender melhor"},
            resolved_contact_id=contacts[0]["contact_id"],
        )["ok"]

        class Generator:
            def generate(self, **_kw):
                return {
                    "stage": "qualify",
                    "qualification": {"problem": "x"},
                    "action": "qualify",
                    "blocks": ["Posso entender seu processo atual?"],
                    "next_step": "conversar",
                    "escalation": False,
                }

        sent = []
        dispatcher = FriendsBatchDispatcher(
            generator=Generator(),
            enabled=lambda: True,
            clock=lambda: now + timedelta(seconds=10),
            send_config={
                "send_enabled": True,
                "kill_switch": False,
                "quepasa": {"send_enabled": True},
                "friends_pilot": {"enabled": True},
            },
            send_client=lambda payload, _cfg: sent.append(payload)
            or {"ok": True, "message_id_hash": "receipt-1"},
        )
        assert dispatcher.run_once() == 1
        assert (
            len(sent) == 1
            and sent[0]["message"] == "Posso entender seu processo atual?"
        )
    finally:
        reset_hermes_home_override(token)
