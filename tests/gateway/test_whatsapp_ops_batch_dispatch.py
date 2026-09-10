from __future__ import annotations
from datetime import datetime, timedelta, timezone
import time

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _campaign(tmp_path, now, profile="p"):
    from gateway.whatsapp_ops_batch_approval import (
        issue_authenticated_friends_authority,
    )
    from tools.whatsapp_ops_batch import (
        activate_friends_pending,
        persist_friends_pending,
        prepare_friends_envelope,
    )
    from tools.whatsapp_ops_store import list_contact_channels, register_contact_local

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
            "contact_id": row["contact_id"],
            "channel_id": list_contact_channels(row["contact_id"])[0]["channel_id"],
        }
        for row in rows
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
        preview, profile_id=profile, chat_id="42", thread_id="9", operator_id="7"
    )
    authority = issue_authenticated_friends_authority(
        profile_id=profile,
        chat_id="42",
        thread_id="9",
        operator_id="7",
        pending_id=pending["pending_id"],
        envelope_digest=pending["envelope_digest"],
    )
    grant = activate_friends_pending(
        pending["pending_id"], decision="approved", authority=authority
    )
    # These tests focus on replies. The complete lifecycle suite separately
    # proves the initial, once-only cohort opening through the real sender.
    from gateway.whatsapp_ops_batch_dispatch import FriendsBatchDispatcher

    opener = FriendsBatchDispatcher(
        profile_id=profile,
        generator=_generator(),
        enabled=lambda: True,
        clock=lambda: now,
        send_config=_cfg(),
        send_client=lambda *_: {"ok": True, "message_id_hash": "opening-receipt"},
    )
    assert opener.run_once() == 3
    return grant, contacts


def _generator():
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

    return Generator()


def _cfg():
    return {
        "send_enabled": True,
        "kill_switch": False,
        "quepasa": {"send_enabled": True},
        "friends_pilot": {"enabled": True},
    }


def test_inbound_is_queued_transactionally_then_frozen_and_sent(tmp_path):
    from gateway.whatsapp_ops_batch_dispatch import FriendsBatchDispatcher
    from tools.whatsapp_ops_store import record_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        _grant, contacts = _campaign(tmp_path, now)
        assert record_inbound_event(
            source_event_id="event-1",
            contact_ref="x@lid",
            payload={"text": "Quero entender melhor"},
            resolved_contact_id=contacts[0]["contact_id"],
        )["ok"]
        sent = []
        dispatcher = FriendsBatchDispatcher(
            profile_id="p",
            generator=_generator(),
            enabled=lambda: True,
            clock=lambda: now + timedelta(seconds=10),
            send_config=_cfg(),
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


def test_optout_in_any_debounced_event_pauses_without_provider_send(tmp_path):
    from gateway.whatsapp_ops_batch_dispatch import FriendsBatchDispatcher
    from tools.whatsapp_ops_batch import _conn
    from tools.whatsapp_ops_store import record_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        grant, contacts = _campaign(tmp_path, now)
        contact = contacts[0]
        record_inbound_event(
            source_event_id="stop-first",
            payload={"text": "stop"},
            resolved_contact_id=contact["contact_id"],
        )
        record_inbound_event(
            source_event_id="normal-second",
            payload={"text": "e agora?"},
            resolved_contact_id=contact["contact_id"],
        )
        sent = []
        dispatcher = FriendsBatchDispatcher(
            profile_id="p",
            generator=_generator(),
            enabled=lambda: True,
            clock=lambda: now + timedelta(seconds=10),
            send_config=_cfg(),
            send_client=lambda *_: sent.append(1) or {"ok": True},
        )
        assert dispatcher.run_once() == 0 and sent == []
        with _conn() as conn:
            state = conn.execute(
                "SELECT status FROM friends_conversations WHERE grant_id=? AND contact_id=?",
                (grant["grant_id"], contact["contact_id"]),
            ).fetchone()["status"]
            suppressed = conn.execute(
                "SELECT 1 FROM friends_suppressions WHERE contact_id=? AND channel_id=?",
                (contact["contact_id"], contact["channel_id"]),
            ).fetchone()
        assert state == "paused" and suppressed
    finally:
        reset_hermes_home_override(token)


def test_generation_timeout_discards_late_result_without_send(tmp_path):
    from gateway.whatsapp_ops_batch_dispatch import FriendsBatchDispatcher
    from tools.whatsapp_ops_batch import _conn
    from tools.whatsapp_ops_store import record_inbound_event

    class Slow:
        def generate(self, **_kw):
            time.sleep(0.08)
            return _generator().generate()

    token = set_hermes_home_override(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        grant, contacts = _campaign(tmp_path, now)
        record_inbound_event(
            source_event_id="slow",
            payload={"text": "oi"},
            resolved_contact_id=contacts[0]["contact_id"],
        )
        sent = []
        dispatcher = FriendsBatchDispatcher(
            profile_id="p",
            generator=Slow(),
            enabled=lambda: True,
            clock=lambda: now + timedelta(seconds=10),
            generation_timeout_seconds=0.01,
            send_config=_cfg(),
            send_client=lambda *_: sent.append(1) or {"ok": True},
        )
        assert dispatcher.run_once() == 1 and sent == []
        with _conn() as conn:
            state = conn.execute(
                "SELECT status FROM friends_conversations WHERE grant_id=?",
                (grant["grant_id"],),
            ).fetchone()["status"]
        assert state == "paused"
    finally:
        reset_hermes_home_override(token)


def test_three_contacts_are_profile_scoped_and_do_not_leak_payloads(tmp_path):
    from gateway.whatsapp_ops_batch_dispatch import FriendsBatchDispatcher
    from tools.whatsapp_ops_store import record_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        _grant, contacts = _campaign(tmp_path, now)
        for index, contact in enumerate(contacts):
            record_inbound_event(
                source_event_id=f"three-{index}",
                payload={"text": f"turn-{index}"},
                resolved_contact_id=contact["contact_id"],
            )
        sent = []
        dispatcher = FriendsBatchDispatcher(
            profile_id="p",
            generator=_generator(),
            enabled=lambda: True,
            clock=lambda: now + timedelta(seconds=10),
            send_config=_cfg(),
            send_client=lambda payload, _cfg: sent.append(payload)
            or {"ok": True, "message_id_hash": str(len(sent))},
        )
        assert dispatcher.run_once(limit=3) == 3
        assert len(sent) == 3 and len({item["idempotency_key"] for item in sent}) == 3
        assert (
            FriendsBatchDispatcher(
                profile_id="other",
                generator=_generator(),
                enabled=lambda: True,
                clock=lambda: now + timedelta(seconds=10),
                send_config=_cfg(),
            ).run_once()
            == 0
        )
    finally:
        reset_hermes_home_override(token)
