"""Local integration oracle: real ledgers/dispatch/send, synthetic peers only."""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from tools import whatsapp_ops_batch as batch
from tools import whatsapp_ops_store as store
from gateway.whatsapp_ops_batch_dispatch import FriendsBatchDispatcher
from gateway.whatsapp_ops_batch_approval import issue_authenticated_friends_authority


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    token = set_hermes_home_override(tmp_path)
    now = [datetime.now(timezone.utc)]
    monkeypatch.setattr(batch, "_now", lambda: now[0])
    monkeypatch.setattr(store, "utc_now", lambda: now[0].isoformat())
    peers = []
    for i in range(3):
        contact = store.register_contact_local(
            alias=f"actor-{i}", raw_ref=f"5511888800{i}@s.whatsapp.net", allow_send=True
        )
        peers.append({
            "contact_id": contact["contact_id"],
            "channel_id": store.list_contact_channels(contact["contact_id"])[0][
                "channel_id"
            ],
        })
    envelope = batch.prepare_friends_envelope(
        campaign_id="friends-local-test",
        contacts=peers,
        offer={"id": "agents-infrastructure"},
        playbook={"id": "friends-v1"},
        issuer="telegram:7",
        starts_at=now[0].isoformat(),
        expires_at=(now[0] + timedelta(hours=2)).isoformat(),
        actions=["qualify", "present", "brief", "refer", "escalate", "stop"],
    )
    pending = batch.persist_friends_pending(
        envelope, profile_id=tmp_path.name, chat_id="42", thread_id="", operator_id="7"
    )
    authority = issue_authenticated_friends_authority(
        profile_id=tmp_path.name,
        chat_id="42",
        thread_id="",
        operator_id="7",
        pending_id=pending["pending_id"],
        envelope_digest=pending["envelope_digest"],
    )
    grant = batch.activate_friends_pending(
        pending["pending_id"], decision="approved", authority=authority
    )
    assert grant["ok"]
    sent, inputs = [], []

    class Generator:
        def generate(self, **kwargs):
            inputs.append(kwargs)
            return {
                "stage": "discovery",
                "qualification": {"problem": "unknown"},
                "action": "qualify",
                "blocks": ["Como funciona seu atendimento hoje?"],
                "next_step": "ouvir",
                "escalation": False,
            }

    cfg = {
        "send_enabled": True,
        "kill_switch": False,
        "quepasa": {"send_enabled": True},
        "friends_pilot": {"enabled": True},
    }

    def send(payload, _cfg):
        sent.append(payload)
        return {"ok": True, "message_id_hash": f"receipt-{len(sent)}"}

    def dispatcher(**kwargs):
        return FriendsBatchDispatcher(
            profile_id=tmp_path.name,
            generator=kwargs.pop("generator", Generator()),
            send_client=kwargs.pop("send_client", send),
            send_config=cfg,
            enabled=lambda: True,
            clock=lambda: now[0],
            **kwargs,
        )

    def inbound(index, text, event):
        result = store.record_inbound_event(
            source_event_id=event,
            payload={"text": text},
            resolved_contact_id=peers[index]["contact_id"],
        )
        assert result["ok"]
        return result

    try:
        yield {
            "now": now,
            "peers": peers,
            "grant": grant,
            "sent": sent,
            "inputs": inputs,
            "dispatcher": dispatcher,
            "inbound": inbound,
            "send": send,
            "profile": tmp_path.name,
        }
    finally:
        reset_hermes_home_override(token)


def test_open_three_once_then_followup_once_after_thirty_minutes(pilot):
    p = pilot
    assert p["dispatcher"]().run_once() == 3
    assert len(p["sent"]) == 3
    assert p["dispatcher"]().run_once() == 0
    p["now"][0] += timedelta(minutes=29, seconds=59)
    assert p["dispatcher"]().run_once() == 0
    p["now"][0] += timedelta(seconds=1)
    assert p["dispatcher"]().run_once() == 3
    assert len(p["sent"]) == 6
    p["now"][0] += timedelta(minutes=31)
    assert p["dispatcher"]().run_once() == 0
    assert len(p["sent"]) == 6


def test_reply_cancels_due_followup_and_uses_only_own_confirmed_history(pilot):
    p = pilot
    p["dispatcher"]().run_once()
    p["now"][0] += timedelta(minutes=30)
    p["inbound"](0, "Meu problema: agenda", "actor-0-a")
    # Other actors may receive their one due followup; actor 0 waits for silence.
    p["dispatcher"]().run_once()
    p["now"][0] += timedelta(seconds=5)
    p["dispatcher"]().run_once()
    own = [x for x in p["inputs"] if x["contact_id"] == p["peers"][0]["contact_id"]]
    assert len(own) == 2
    assert any(m["role"] == "assistant" for m in own[-1]["messages"])
    assert any("agenda" in m["text"] for m in own[-1]["messages"])
    assert all(
        "agenda" not in json.dumps(x)
        for x in p["inputs"]
        if x["contact_id"] != p["peers"][0]["contact_id"]
    )


def test_reply_between_blocks_is_not_lost_or_paused_and_replays_no_old_blocks(pilot):
    p = pilot
    p["dispatcher"]().run_once()
    p["inbound"](0, "Detalhe a solucao", "first")
    p["now"][0] += timedelta(seconds=5)

    class Blocks:
        def generate(self, **kwargs):
            return {
                "stage": "discovery",
                "qualification": {},
                "action": "present",
                "blocks": ["Parte um", "Parte dois", "Parte tres"],
                "next_step": "ouvir",
                "escalation": False,
            }

    def interrupted(payload, cfg):
        result = p["send"](payload, cfg)
        p["inbound"](0, "Quero falar sobre agenda", "during-send")
        return result

    before = len(p["sent"])
    p["dispatcher"](generator=Blocks(), send_client=interrupted).run_once()
    assert len(p["sent"]) == before + 1
    with batch._conn() as conn:
        row = conn.execute(
            "SELECT * FROM friends_conversations WHERE contact_id=?",
            (p["peers"][0]["contact_id"],),
        ).fetchone()
        assert row["status"] == "pending"
    p["now"][0] += timedelta(seconds=5)
    p["dispatcher"]().run_once()
    assert len(p["sent"]) == before + 2
    last = p["inputs"][-1]["messages"]
    assert any(m["text"] == "Parte um" for m in last)
    assert not any(m["text"] in ("Parte dois", "Parte tres") for m in last)


def test_optout_is_immediate_durable_and_normal_negation_not_optout(pilot):
    p = pilot
    p["dispatcher"]().run_once()
    p["inbound"](0, "Por favor, nao me envie mais mensagens.", "stop-now")
    with batch._conn() as conn:
        assert conn.execute(
            "SELECT 1 FROM friends_suppressions WHERE contact_id=?",
            (p["peers"][0]["contact_id"],),
        ).fetchone()
    p["inbound"](0, "obrigado", "later")
    p["inbound"](1, "Nao conheco essa parte da ferramenta", "ordinary-negation")
    p["now"][0] += timedelta(seconds=5)
    before = len(p["sent"])
    p["dispatcher"]().run_once()
    assert len(p["sent"]) == before + 1


def test_silence_deadline_is_capped_at_twenty_seconds(pilot):
    p = pilot
    p["dispatcher"]().run_once()
    first = p["now"][0]
    for i in range(7):
        p["inbound"](0, f"pedaco {i}", f"burst-{i}")
        p["now"][0] += timedelta(seconds=3)
    with batch._conn() as conn:
        due = conn.execute(
            "SELECT due_at FROM friends_conversations WHERE contact_id=?",
            (p["peers"][0]["contact_id"],),
        ).fetchone()[0]
    assert datetime.fromisoformat(due) <= first + timedelta(seconds=20)
    assert p["dispatcher"]().run_once() == 1


def test_frozen_plan_survives_crash_without_regeneration(pilot):
    p = pilot
    d = p["dispatcher"]()

    class Crash(BaseException):
        pass

    real_send = d._send_plan
    d._send_plan = lambda *args: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        d.run_once(limit=1)
    assert len(p["inputs"]) == 1 and not p["sent"]
    p["now"][0] += timedelta(seconds=200)
    p["dispatcher"]().run_once()
    assert len(p["sent"]) == 3 and len(p["inputs"]) == 3


def test_expiry_cancels_idle_and_reserved_orphan_only_after_lease(pilot):
    p = pilot
    d = p["dispatcher"]()
    d.run_once()
    with batch._conn() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM friends_conversations WHERE followup_due_at IS NOT NULL"
            ).fetchone()[0]
            == 3
        )
    p["now"][0] += timedelta(hours=2)
    d.run_once()
    with batch._conn() as conn:
        assert (
            conn.execute("SELECT status FROM friends_grants").fetchone()[0] == "expired"
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM friends_conversations WHERE followup_due_at IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
    assert len(p["sent"]) == 3


def test_valid_reservation_is_untouched_then_expired_reservation_pauses_with_outbox(
    pilot,
):
    p = pilot
    d = p["dispatcher"]()
    d.run_once()
    peer = p["peers"][0]
    with batch._conn() as conn:
        digest = conn.execute("SELECT offer_digest FROM friends_grants").fetchone()[0]
    frozen = batch.freeze_friends_message_plan(
        p["grant"]["grant_id"],
        **peer,
        blocks=["reserved"],
        action="qualify",
        context_highwatermark="manual-recovery",
        offer_digest=digest,
    )
    lease = batch.acquire_friends_conversation_lease(frozen["plan_id"])
    block = batch.reserve_friends_block(frozen["plan_id"], lease["fence"])
    assert block["ok"]
    d.run_once()
    with batch._conn() as conn:
        assert (
            conn.execute(
                "SELECT status FROM friends_blocks WHERE plan_id=?",
                (frozen["plan_id"],),
            ).fetchone()[0]
            == "reserved"
        )
    p["now"][0] += timedelta(minutes=5)
    d.run_once()
    with batch._conn() as conn:
        assert (
            conn.execute(
                "SELECT status FROM friends_outbox WHERE plan_id=?",
                (frozen["plan_id"],),
            ).fetchone()[0]
            == "uncertain"
        )
        assert (
            conn.execute("SELECT status FROM friends_grants").fetchone()[0] == "paused"
        )
    assert len(p["sent"]) == 3


def test_stale_worker_cannot_commit_after_competing_claim(pilot):
    p = pilot
    d = p["dispatcher"]()
    d.run_once()
    p["inbound"](0, "pergunta", "fenced")
    p["now"][0] += timedelta(seconds=5)
    with batch._conn() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM friends_conversations WHERE contact_id=?",
                (p["peers"][0]["contact_id"],),
            ).fetchone()
        )
    first = d._claim(row)
    assert first and not p["dispatcher"]()._claim(row)
    with batch._conn() as conn:
        conn.execute(
            "UPDATE friends_conversations SET lease_fence_hash='different-owner' WHERE contact_id=?",
            (row["contact_id"],),
        )
    assert not d._current(row, first)
    d._pause(row, "must_not_apply", first)
    with batch._conn() as conn:
        assert (
            conn.execute(
                "SELECT status FROM friends_conversations WHERE contact_id=?",
                (row["contact_id"],),
            ).fetchone()[0]
            == "generating"
        )


def test_inbound_transaction_rolls_back_if_queue_insert_fails(pilot, monkeypatch):
    import sqlite3

    p = pilot

    def fail(*args, **kwargs):
        raise sqlite3.IntegrityError("queue-sentinel")

    monkeypatch.setattr(batch, "enqueue_friends_inbound", fail)
    with pytest.raises(sqlite3.IntegrityError):
        p["inbound"](0, "hello", "atomic-fail")
    with batch._conn() as conn:
        assert not conn.execute(
            "SELECT 1 FROM inbound_events WHERE source_event_id_hash=?",
            (store.hash_text("atomic-fail"),),
        ).fetchone()


@pytest.mark.asyncio
async def test_real_webhook_ack_and_lifecycle_use_current_profile_without_generic_dispatch(
    pilot, monkeypatch
):
    import hashlib
    import hmac
    from unittest.mock import AsyncMock
    from aiohttp import ClientSession
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    import gateway.whatsapp_ops_batch_dispatch as dispatch_module

    p = pilot
    dispatcher = p["dispatcher"]()
    monkeypatch.setattr(dispatch_module, "FriendsBatchDispatcher", lambda: dispatcher)
    import tools.whatsapp_ops_tool as tool

    monkeypatch.setattr(
        tool, "_notify_operator_of_recognized_inbound", lambda *args: None
    )
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "friends": {"kind": "quepasa_inbound", "secret": "fixture-key"}
                },
            },
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._direct_deliver = AsyncMock()
    assert await adapter.connect()
    task = adapter._friends_dispatch_task
    try:
        port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
        ref = "55118888000@s.whatsapp.net"
        body = json.dumps({
            "id": "webhook-1",
            "chat": {"id": ref},
            "participant": {"id": ref},
            "message": {"conversation": "Quero entender melhor"},
        }).encode()
        signature = hmac.new(b"fixture-key", body, hashlib.sha256).hexdigest()
        async with ClientSession() as client:
            for _ in range(2):
                async with client.post(
                    f"http://127.0.0.1:{port}/webhooks/friends",
                    data=body,
                    headers={"X-Webhook-Signature": signature},
                ) as response:
                    assert response.status == 200
        with batch._conn() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM friends_inbound_queue WHERE contact_id=?",
                    (p["peers"][0]["contact_id"],),
                ).fetchone()[0]
                == 1
            )
        adapter.handle_message.assert_not_called()
        adapter._direct_deliver.assert_not_called()
    finally:
        await adapter.disconnect()
    assert task.done() and adapter._friends_dispatch_task is None
    with pytest.raises(ConnectionRefusedError):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)


def test_current_profile_flag_reaches_send_guard_without_nested_config(
    pilot, monkeypatch
):
    import tools.whatsapp_ops_tool as tool

    monkeypatch.setattr(
        tool,
        "load_config",
        lambda: {
            "friends_pilot": {"enabled": True},
            "whatsapp_ops": {
                "send_enabled": True,
                "kill_switch": False,
                "quepasa": {"send_enabled": True},
            },
        },
    )
    assert tool._runtime_config()["friends_pilot"]["enabled"] is True


def test_real_agent_constructor_has_no_tools_memory_or_fallback(pilot, monkeypatch):
    from run_agent import AIAgent
    from tools.whatsapp_ops_conversation import FriendsHermesGenerator

    seen = []

    def model(self, *args, **kwargs):
        assert self.tools == []
        assert self._memory_enabled is False and self._memory_manager is None
        assert self._fallback_chain == []
        seen.append(kwargs["system_message"])
        return {
            "final_response": json.dumps({
                "stage": "discovery",
                "qualification": {},
                "action": "qualify",
                "blocks": ["Como posso ajudar?"],
                "next_step": "ouvir",
                "escalation": False,
            })
        }

    monkeypatch.setattr(AIAgent, "run_conversation", model)
    generator = FriendsHermesGenerator(
        config_loader=lambda: {
            "model": {"provider": "openai", "default": "gpt-4.1-mini"}
        },
        runtime_resolver=lambda **kwargs: {
            "provider": "openai",
            "api_key": "fixture-key",
            "base_url": "https://fixture.invalid/v1",
            "api_mode": "chat_completions",
        },
    )
    for wm in ("first", "second"):
        assert (
            generator.generate(
                grant_id="g",
                contact_id="c",
                channel_id="ch",
                messages=[],
                offer={},
                highwatermark=wm,
            )["action"]
            == "qualify"
        )
    assert seen[0] == seen[1]


@pytest.mark.asyncio
async def test_service_does_not_block_event_loop_and_stop_discards_late_model(pilot):
    p = pilot
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def generate(self, **kwargs):
            entered.set()
            release.wait(3)
            return {
                "stage": "discovery",
                "qualification": {},
                "action": "qualify",
                "blocks": ["oi"],
                "next_step": "ouvir",
                "escalation": False,
            }

    d = p["dispatcher"](generator=Slow(), generation_timeout_seconds=2)
    service = asyncio.create_task(d.serve())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        d.stop()
        release.set()
        await asyncio.wait_for(service, 3)
        assert not p["sent"]
    finally:
        release.set()
        d.stop()
        await asyncio.gather(service, return_exceptions=True)
