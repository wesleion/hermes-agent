"""Persistent, human-granted three-contact WhatsApp friends pilot ledger.

There is no public approval shortcut here.  A prepared card is persisted first,
then only the authenticated Telegram callback boundary can consume that card.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

_MAX_TURNS, _MAX_MESSAGES, _MAX_BLOCKS, _GLOBAL_CAP = 10, 30, 3, 90
_MAX_WINDOW = timedelta(hours=2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _valid_string(value: Any, maximum: int = 160) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _conn() -> sqlite3.Connection:
    from tools.whatsapp_ops_store import _connect

    return _connect()


def _init() -> None:
    from tools.whatsapp_ops_store import init_db

    init_db()
    with _conn() as conn:
        migrate_friends_batch_ledger(conn)


def migrate_friends_batch_ledger(conn: sqlite3.Connection) -> None:
    """Add-only migration using a savepoint; never executescript in a caller transaction."""
    statements = (
        "CREATE TABLE IF NOT EXISTS friends_notifications(event_key TEXT PRIMARY KEY,status TEXT NOT NULL,receipt_hash TEXT,created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS friends_pending_envelopes (pending_id TEXT PRIMARY KEY,envelope_digest TEXT NOT NULL UNIQUE,envelope_json TEXT NOT NULL,card_digest TEXT NOT NULL,profile_id TEXT NOT NULL,chat_id TEXT NOT NULL,thread_id TEXT NOT NULL,operator_id TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('pending','approved','denied','consumed','expired')),expires_at TEXT NOT NULL,created_at TEXT NOT NULL,resolved_at TEXT)",
        "CREATE TABLE IF NOT EXISTS friends_grants (grant_id TEXT PRIMARY KEY,campaign_id TEXT NOT NULL,envelope_digest TEXT NOT NULL UNIQUE,issuer_hash TEXT NOT NULL,contacts_json TEXT NOT NULL,actions_json TEXT NOT NULL,starts_at TEXT NOT NULL,expires_at TEXT NOT NULL,revocation_version INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL CHECK(status IN ('active','revoked','expired','paused')),max_turns INTEGER NOT NULL,max_messages INTEGER NOT NULL,max_blocks INTEGER NOT NULL,global_cap INTEGER NOT NULL,messages_reserved INTEGER NOT NULL DEFAULT 0,payload_json TEXT NOT NULL,offer_digest TEXT NOT NULL,created_at TEXT NOT NULL,activated_at TEXT NOT NULL,revoked_at TEXT)",
        "CREATE TABLE IF NOT EXISTS friends_plans (plan_id TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES friends_grants(grant_id),contact_id TEXT NOT NULL,channel_id TEXT NOT NULL,action TEXT NOT NULL,offer_digest TEXT NOT NULL,context_highwatermark TEXT NOT NULL,blocks_digest TEXT NOT NULL,blocks_json TEXT NOT NULL,replay_key TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('ready','sending','completed','cancelled','paused','failed_unknown')),lease_fence_hash TEXT,lease_expires_at TEXT,turns_reserved INTEGER NOT NULL DEFAULT 0,messages_reserved INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(grant_id,replay_key))",
        "CREATE TABLE IF NOT EXISTS friends_blocks (block_id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES friends_plans(plan_id),block_index INTEGER NOT NULL,text TEXT NOT NULL,block_digest TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('pending','reserved','sent','failed','uncertain','cancelled')),reservation_id TEXT,attempts INTEGER NOT NULL DEFAULT 0,receipt_hash TEXT,error_code TEXT,reserved_at TEXT,finished_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(plan_id,block_index))",
        "CREATE TABLE IF NOT EXISTS friends_outbox (outbox_id TEXT PRIMARY KEY,block_id TEXT NOT NULL UNIQUE REFERENCES friends_blocks(block_id),plan_id TEXT NOT NULL,status TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,receipt_hash TEXT,error_code TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS friends_conversation_leases (grant_id TEXT NOT NULL,contact_id TEXT NOT NULL,channel_id TEXT NOT NULL,fence_hash TEXT NOT NULL,expires_at TEXT NOT NULL,plan_id TEXT NOT NULL UNIQUE,PRIMARY KEY(grant_id,contact_id,channel_id))",
        "CREATE TABLE IF NOT EXISTS friends_suppressions (contact_id TEXT NOT NULL,channel_id TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(contact_id,channel_id))",
        "CREATE TABLE IF NOT EXISTS friends_conversations (profile_id TEXT NOT NULL,grant_id TEXT NOT NULL,contact_id TEXT NOT NULL,channel_id TEXT NOT NULL,processed_cursor TEXT,highwatermark TEXT,first_event_at TEXT,last_event_at TEXT,due_at TEXT,generation_version INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'idle',followup_due_at TEXT,followup_sent INTEGER NOT NULL DEFAULT 0,lease_fence_hash TEXT,lease_expires_at TEXT,PRIMARY KEY(profile_id,grant_id,contact_id,channel_id))",
        "CREATE TABLE IF NOT EXISTS friends_inbound_queue (event_id TEXT NOT NULL UNIQUE,profile_id TEXT NOT NULL,grant_id TEXT NOT NULL,contact_id TEXT NOT NULL,channel_id TEXT NOT NULL,text TEXT NOT NULL,received_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS ix_friends_blocks_plan_state ON friends_blocks(plan_id,status,block_index)",
        "CREATE INDEX IF NOT EXISTS ix_friends_queue_ready ON friends_inbound_queue(profile_id,grant_id,contact_id,channel_id,status,received_at)",
        "CREATE TABLE IF NOT EXISTS friends_qualifications (grant_id TEXT NOT NULL,contact_id TEXT NOT NULL,channel_id TEXT NOT NULL,outcome TEXT NOT NULL,simulation INTEGER NOT NULL DEFAULT 1,detail_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(grant_id,contact_id,channel_id))",
    )
    conn.execute("SAVEPOINT friends_batch_v2")
    try:
        for statement in statements:
            conn.execute(statement)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(friends_conversations)")}
        for name, spec in {"job_kind": "TEXT NOT NULL DEFAULT 'reply'", "active_plan_id": "TEXT", "generation_json": "TEXT"}.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE friends_conversations ADD COLUMN {name} {spec}")
    except BaseException:
        conn.execute("ROLLBACK TO friends_batch_v2")
        conn.execute("RELEASE friends_batch_v2")
        raise
    conn.execute("RELEASE friends_batch_v2")


def _envelope_valid(envelope: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(envelope, dict):
        return None, "grant_preparation_invalid"
    required = {
        "schema",
        "campaign_id",
        "contacts",
        "offer",
        "playbook",
        "issuer",
        "actions",
        "starts_at",
        "expires_at",
        "caps",
        "revocation_version",
    }
    if (
        set(envelope) != required
        or envelope.get("schema") != "friends_autonomous_v1"
        or envelope.get("revocation_version") != 0
    ):
        return None, "grant_preparation_invalid"
    if not _valid_string(envelope.get("campaign_id")) or not _valid_string(
        envelope.get("issuer")
    ):
        return None, "grant_identity_invalid"
    contacts = envelope.get("contacts")
    if not isinstance(contacts, list) or len(contacts) != 3:
        return None, "grant_contacts_exactly_three_required"
    if any(
        not isinstance(row, dict)
        or set(row) != {"contact_id", "channel_id"}
        or not _valid_string(row.get("contact_id"))
        or not _valid_string(row.get("channel_id"))
        for row in contacts
    ):
        return None, "grant_contact_invalid"
    if (
        len({row["contact_id"] for row in contacts}) != 3
        or len({(row["contact_id"], row["channel_id"]) for row in contacts}) != 3
    ):
        return None, "grant_contacts_not_unique"
    if not isinstance(envelope.get("offer"), dict) or not isinstance(
        envelope.get("playbook"), dict
    ):
        return None, "grant_envelope_invalid"
    actions = envelope.get("actions")
    if (
        not isinstance(actions, list)
        or not actions
        or any(not _valid_string(action, 80) for action in actions)
    ):
        return None, "grant_actions_invalid"
    starts, expires = _iso(envelope.get("starts_at")), _iso(envelope.get("expires_at"))
    if (
        starts is None
        or expires is None
        or expires <= starts
        or expires - starts > _MAX_WINDOW
    ):
        return None, "grant_window_invalid"
    if envelope.get("caps") != {
        "max_turns": _MAX_TURNS,
        "max_messages": _MAX_MESSAGES,
        "max_blocks": _MAX_BLOCKS,
        "global_cap": _GLOBAL_CAP,
    }:
        return None, "grant_caps_invalid"
    return envelope, None


def prepare_friends_envelope(
    *,
    campaign_id: Any,
    contacts: Any,
    offer: Any,
    playbook: Any,
    issuer: Any,
    starts_at: Any,
    expires_at: Any,
    actions: Any = ("offer",),
) -> dict[str, Any]:
    if not isinstance(contacts, list):
        return {"ok": False, "error": "grant_contacts_exactly_three_required"}
    if any(
        not isinstance(row, dict)
        or not _valid_string(row.get("contact_id"))
        or not _valid_string(row.get("channel_id"))
        for row in contacts
    ):
        return {"ok": False, "error": "grant_contact_invalid"}
    normalized = [
        {
            "contact_id": row["contact_id"].strip(),
            "channel_id": row["channel_id"].strip(),
        }
        for row in contacts
    ]
    starts, expires = _iso(starts_at), _iso(expires_at)
    envelope = {
        "schema": "friends_autonomous_v1",
        "campaign_id": campaign_id.strip()
        if isinstance(campaign_id, str)
        else campaign_id,
        "contacts": normalized,
        "offer": offer,
        "playbook": playbook,
        "issuer": issuer.strip() if isinstance(issuer, str) else issuer,
        "actions": list(actions) if isinstance(actions, (list, tuple)) else actions,
        "starts_at": starts.isoformat() if starts else starts_at,
        "expires_at": expires.isoformat() if expires else expires_at,
        "caps": {
            "max_turns": _MAX_TURNS,
            "max_messages": _MAX_MESSAGES,
            "max_blocks": _MAX_BLOCKS,
            "global_cap": _GLOBAL_CAP,
        },
        "revocation_version": 0,
    }
    _, error = _envelope_valid(envelope)
    return (
        {"ok": False, "error": error}
        if error
        else {
            "ok": True,
            "mutated": False,
            "envelope": envelope,
            "envelope_digest": _digest(envelope),
            "offer_digest": _digest({"offer": offer, "playbook": playbook}),
        }
    )


def persist_friends_pending(
    prepared: Any, *, profile_id: Any, chat_id: Any, thread_id: Any, operator_id: Any
) -> dict[str, Any]:
    if not isinstance(prepared, dict) or prepared.get("ok") is not True:
        return {"ok": False, "error": "grant_preparation_invalid"}
    envelope, error = _envelope_valid(prepared.get("envelope"))
    if error or prepared.get("envelope_digest") != _digest(envelope):
        return {"ok": False, "error": error or "grant_digest_invalid"}
    if not all(_valid_string(x) for x in (profile_id, chat_id, operator_id)):
        return {"ok": False, "error": "approval_binding_invalid"}
    pending_id = "friends_pending_" + uuid.uuid4().hex[:16]
    now = _now().isoformat()
    card = _digest({
        "pending_id": pending_id,
        "envelope_digest": prepared["envelope_digest"],
    })
    _init()
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO friends_pending_envelopes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pending_id,
                    prepared["envelope_digest"],
                    _canonical(envelope),
                    card,
                    str(profile_id),
                    str(chat_id),
                    str(thread_id or ""),
                    str(operator_id),
                    "pending",
                    envelope["expires_at"],
                    now,
                    None,
                ),
            )
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "pending_already_exists"}
    return {
        "ok": True,
        "pending_id": pending_id,
        "envelope_digest": prepared["envelope_digest"],
        "card_digest": card,
        "expires_at": envelope["expires_at"],
    }


def _contact_snapshots(
    conn: sqlite3.Connection, contacts: list[dict[str, str]]
) -> tuple[list[dict[str, str]] | None, str | None]:
    snapshots = []
    for item in contacts:
        row = conn.execute(
            "SELECT c.id,c.whitelisted,ch.id AS channel_id,ch.address_hash,ch.context_key_hash,ch.is_active,ch.validation_status,ch.allow_send,ch.authorized_at,ch.revoked_at FROM contacts c JOIN contact_channels ch ON ch.contact_id=c.id WHERE c.id=? AND ch.id=?",
            (item["contact_id"], item["channel_id"]),
        ).fetchone()
        if (
            row is None
            or row["whitelisted"] != 1
            or row["is_active"] != 1
            or row["validation_status"] != "validated"
            or row["allow_send"] != 1
            or not row["authorized_at"]
            or row["revoked_at"] is not None
        ):
            return None, "contact_binding_invalid"
        snapshots.append({
            "contact_id": row["id"],
            "channel_id": row["channel_id"],
            "binding_digest": _digest({
                "address_hash": row["address_hash"],
                "context_key_hash": row["context_key_hash"],
            }),
        })
    return snapshots, None


def activate_friends_pending(
    pending_id: Any, *, decision: Any, authority: Any
) -> dict[str, Any]:
    from gateway.whatsapp_ops_batch_approval import authority_binding

    binding = authority_binding(authority)
    if str(decision or "").lower() not in {"approved", "denied"}:
        return {"ok": False, "error": "trusted_decision_required"}
    if binding is None or not _valid_string(pending_id):
        return {"ok": False, "error": "trusted_callback_required"}
    _init()
    now = _now()
    now_s = now.isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM friends_pending_envelopes WHERE pending_id=?",
                (pending_id,),
            ).fetchone()
            if row is None:
                result = {"ok": False, "error": "pending_not_found"}
            elif row["status"] != "pending":
                result = {"ok": False, "error": "pending_already_consumed"}
            elif _iso(row["expires_at"]) is None or _iso(row["expires_at"]) <= now:
                result = {"ok": False, "error": "pending_expired"}
            elif any(
                binding[k] != row[k]
                for k in (
                    "profile_id",
                    "chat_id",
                    "thread_id",
                    "operator_id",
                    "pending_id",
                    "envelope_digest",
                )
            ):
                result = {"ok": False, "error": "callback_binding_mismatch"}
            elif decision != "approved":
                conn.execute(
                    "UPDATE friends_pending_envelopes SET status='denied',resolved_at=? WHERE pending_id=? AND status='pending'",
                    (now_s, pending_id),
                )
                result = {"ok": True, "status": "denied", "mutated": False}
            else:
                envelope = json.loads(row["envelope_json"])
                envelope, error = _envelope_valid(envelope)
                snapshots, error2 = (
                    _contact_snapshots(conn, envelope["contacts"])
                    if not error
                    else (None, error)
                )
                if error or error2:
                    result = {"ok": False, "error": error or error2}
                else:
                    grant_id = "friends_grant_" + uuid.uuid4().hex[:16]
                    offer_digest = _digest({
                        "offer": envelope["offer"],
                        "playbook": envelope["playbook"],
                    })
                    conn.execute(
                        "INSERT INTO friends_grants (grant_id,campaign_id,envelope_digest,issuer_hash,contacts_json,actions_json,starts_at,expires_at,revocation_version,status,max_turns,max_messages,max_blocks,global_cap,messages_reserved,payload_json,offer_digest,created_at,activated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            grant_id,
                            envelope["campaign_id"],
                            row["envelope_digest"],
                            _digest(envelope["issuer"]),
                            _canonical(snapshots),
                            _canonical(envelope["actions"]),
                            envelope["starts_at"],
                            envelope["expires_at"],
                            0,
                            "active",
                            _MAX_TURNS,
                            _MAX_MESSAGES,
                            _MAX_BLOCKS,
                            _GLOBAL_CAP,
                            0,
                            _canonical(envelope),
                            offer_digest,
                            now_s,
                            now_s,
                        ),
                    )
                    conn.execute(
                        "UPDATE friends_pending_envelopes SET status='consumed',resolved_at=? WHERE pending_id=? AND status='pending'",
                        (now_s, pending_id),
                    )
                    result = {
                        "ok": True,
                        "grant_id": grant_id,
                        "status": "active",
                        "expires_at": envelope["expires_at"],
                        "offer_digest": offer_digest,
                    }
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def friends_pending_binding(pending_id: str) -> dict[str, str] | None:
    """Safe binding data for the Telegram callback; no envelope text is exposed."""
    _init()
    with _conn() as conn:
        row = conn.execute(
            "SELECT pending_id,envelope_digest,profile_id,chat_id,thread_id,operator_id,status FROM friends_pending_envelopes WHERE pending_id=?",
            (pending_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def trusted_activate_friends_grant(
    prepared: Any, *, decision: Any, operator_identity: Any
) -> dict[str, Any]:
    """Removed public shortcut: strings are never grant authority."""
    return {"ok": False, "error": "trusted_callback_required"}


def friends_grant_status(grant_id: str) -> dict[str, Any]:
    _init()
    with _conn() as conn:
        row = conn.execute(
            "SELECT grant_id,campaign_id,envelope_digest,status,starts_at,expires_at,revocation_version,messages_reserved,global_cap,offer_digest FROM friends_grants WHERE grant_id=?",
            (grant_id,),
        ).fetchone()
    return (
        {"ok": False, "error": "grant_not_found"}
        if row is None
        else {"ok": True, **dict(row)}
    )


def _grant_active(
    conn: sqlite3.Connection, grant_id: str
) -> tuple[sqlite3.Row | None, str | None]:
    row = conn.execute(
        "SELECT * FROM friends_grants WHERE grant_id=?", (grant_id,)
    ).fetchone()
    if row is None:
        return None, "grant_not_found"
    if row["status"] != "active":
        return None, "grant_not_active"
    now = _now()
    if (
        _iso(row["starts_at"]) is None
        or _iso(row["starts_at"]) > now
        or _iso(row["expires_at"]) is None
        or _iso(row["expires_at"]) <= now
    ):
        return None, "grant_expired"
    return row, None


def _bound_contact_current(
    conn: sqlite3.Connection, grant: sqlite3.Row, contact_id: str, channel_id: str
) -> bool:
    snapshots = json.loads(grant["contacts_json"])
    bound = next(
        (
            x
            for x in snapshots
            if x["contact_id"] == contact_id and x["channel_id"] == channel_id
        ),
        None,
    )
    if bound is None:
        return False
    current, error = _contact_snapshots(
        conn, [{"contact_id": contact_id, "channel_id": channel_id}]
    )
    return (
        error is None
        and current is not None
        and current[0]["binding_digest"] == bound["binding_digest"]
    )


def freeze_friends_message_plan(
    grant_id: str,
    *,
    contact_id: Any,
    channel_id: Any,
    blocks: Any,
    action: Any,
    context_highwatermark: Any,
    offer_digest: Any,
) -> dict[str, Any]:
    if not all((
        _valid_string(contact_id),
        _valid_string(channel_id),
        _valid_string(action, 80),
        _valid_string(context_highwatermark),
        isinstance(offer_digest, str) and len(offer_digest) == 64,
    )):
        return {"ok": False, "error": "plan_input_invalid"}
    if (
        not isinstance(blocks, list)
        or not 1 <= len(blocks) <= _MAX_BLOCKS
        or any(not _valid_string(v, 4096) for v in blocks)
    ):
        return {"ok": False, "error": "plan_blocks_invalid"}
    # Exact immutable bytes: no strip/normalization after the digest is made.
    if any(not isinstance(v, str) or v != v.strip() for v in blocks):
        return {"ok": False, "error": "plan_blocks_noncanonical"}
    _init()
    now = _now().isoformat()
    digest = _digest(blocks)
    with _conn() as conn:
        grant, reason = _grant_active(conn, grant_id)
        if reason:
            return {"ok": False, "error": reason}
        if offer_digest != grant["offer_digest"]:
            return {"ok": False, "error": "offer_digest_mismatch"}
        if action not in json.loads(grant["actions_json"]):
            return {"ok": False, "error": "action_not_granted"}
        if not _bound_contact_current(conn, grant, contact_id, channel_id):
            return {"ok": False, "error": "contact_binding_invalid"}
        if conn.execute(
            "SELECT 1 FROM friends_suppressions WHERE contact_id=? AND channel_id=?",
            (contact_id, channel_id),
        ).fetchone():
            return {"ok": False, "error": "contact_suppressed"}
        replay = _digest({
            "contact_id": contact_id,
            "channel_id": channel_id,
            "action": action,
            "context_highwatermark": context_highwatermark,
            "grant_revision": grant["revocation_version"],
        })
        plan_id = "friends_plan_" + uuid.uuid4().hex[:16]
        try:
            conn.execute(
                "INSERT INTO friends_plans (plan_id,grant_id,contact_id,channel_id,action,offer_digest,context_highwatermark,blocks_digest,blocks_json,replay_key,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    grant_id,
                    contact_id,
                    channel_id,
                    action,
                    offer_digest,
                    context_highwatermark,
                    digest,
                    _canonical(blocks),
                    replay,
                    "ready",
                    now,
                    now,
                ),
            )
            for index, text in enumerate(blocks, 1):
                conn.execute(
                    "INSERT INTO friends_blocks (block_id,plan_id,block_index,text,block_digest,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        "friends_block_" + uuid.uuid4().hex[:16],
                        plan_id,
                        index,
                        text,
                        _digest(text),
                        "pending",
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "plan_duplicate"}
    return {
        "ok": True,
        "plan_id": plan_id,
        "blocks_digest": digest,
        "block_count": len(blocks),
    }


def acquire_friends_conversation_lease(
    plan_id: str, *, lease_seconds: Any = 60
) -> dict[str, Any]:
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or not 1 <= lease_seconds <= 3600
    ):
        return {"ok": False, "error": "lease_seconds_invalid"}
    _init()
    now = _now()
    fence = secrets.token_urlsafe(24)
    expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            plan = conn.execute(
                "SELECT * FROM friends_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            grant, reason = (
                _grant_active(conn, plan["grant_id"])
                if plan
                else (None, "plan_not_found")
            )
            if plan is None:
                result = {"ok": False, "error": "plan_not_found"}
            elif plan["status"] in {
                "cancelled",
                "completed",
                "failed_unknown",
                "paused",
            }:
                result = {"ok": False, "error": "plan_" + plan["status"]}
            elif reason:
                result = {"ok": False, "error": reason}
            elif not _bound_contact_current(
                conn, grant, plan["contact_id"], plan["channel_id"]
            ):
                result = {"ok": False, "error": "contact_binding_invalid"}
            else:
                old = conn.execute(
                    "SELECT * FROM friends_conversation_leases WHERE grant_id=? AND contact_id=? AND channel_id=?",
                    (plan["grant_id"], plan["contact_id"], plan["channel_id"]),
                ).fetchone()
                if (
                    old
                    and _iso(old["expires_at"])
                    and _iso(old["expires_at"]) > now
                    and old["plan_id"] != plan_id
                ):
                    result = {"ok": False, "error": "lease_held"}
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO friends_conversation_leases VALUES (?,?,?,?,?,?)",
                        (
                            plan["grant_id"],
                            plan["contact_id"],
                            plan["channel_id"],
                            _digest(fence),
                            expiry,
                            plan_id,
                        ),
                    )
                    conn.execute(
                        "UPDATE friends_plans SET lease_fence_hash=?,lease_expires_at=?,updated_at=? WHERE plan_id=?",
                        (_digest(fence), expiry, now.isoformat(), plan_id),
                    )
                    result = {
                        "ok": True,
                        "plan_id": plan_id,
                        "fence": fence,
                        "expires_at": expiry,
                    }
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def _plan_fence(
    conn: sqlite3.Connection, plan: sqlite3.Row, fence: str, now: datetime
) -> str | None:
    lease = conn.execute(
        "SELECT * FROM friends_conversation_leases WHERE grant_id=? AND contact_id=? AND channel_id=?",
        (plan["grant_id"], plan["contact_id"], plan["channel_id"]),
    ).fetchone()
    if (
        plan["lease_fence_hash"] != _digest(str(fence))
        or not lease
        or lease["plan_id"] != plan["plan_id"]
        or lease["fence_hash"] != _digest(str(fence))
        or not _iso(lease["expires_at"])
        or _iso(lease["expires_at"]) <= now
    ):
        return "lease_fence_invalid"
    return None


def friends_plan_transport_target(plan_id: str, fence: str) -> dict[str, Any]:
    _init()
    with _conn() as conn:
        plan = conn.execute(
            "SELECT * FROM friends_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            return {"ok": False, "error": "plan_not_found"}
        if plan["status"] in {"cancelled", "completed", "failed_unknown", "paused"}:
            return {"ok": False, "error": "plan_" + plan["status"]}
        bad = _plan_fence(conn, plan, fence, _now())
        grant, reason = _grant_active(conn, plan["grant_id"])
        if bad:
            return {"ok": False, "error": bad}
        if reason:
            return {"ok": False, "error": reason}
        if not _bound_contact_current(
            conn, grant, plan["contact_id"], plan["channel_id"]
        ):
            return {"ok": False, "error": "contact_binding_invalid"}
        return {
            "ok": True,
            "contact_id": plan["contact_id"],
            "channel_id": plan["channel_id"],
        }


def reserve_friends_block(plan_id: str, fence: str) -> dict[str, Any]:
    _init()
    now = _now()
    now_s = now.isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            plan = conn.execute(
                "SELECT * FROM friends_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan is None:
                result = {"ok": False, "error": "plan_not_found"}
            elif plan["status"] in {
                "cancelled",
                "completed",
                "failed_unknown",
                "paused",
            }:
                result = {"ok": False, "error": "plan_" + plan["status"]}
            else:
                bad = _plan_fence(conn, plan, fence, now)
                grant, reason = _grant_active(conn, plan["grant_id"])
                if bad:
                    result = {"ok": False, "error": bad}
                elif reason:
                    result = {"ok": False, "error": reason}
                elif not _bound_contact_current(
                    conn, grant, plan["contact_id"], plan["channel_id"]
                ):
                    result = {"ok": False, "error": "contact_binding_invalid"}
                elif conn.execute(
                    "SELECT 1 FROM friends_suppressions WHERE contact_id=? AND channel_id=?",
                    (plan["contact_id"], plan["channel_id"]),
                ).fetchone():
                    result = {"ok": False, "error": "contact_suppressed"}
                elif conn.execute(
                    "SELECT 1 FROM friends_blocks b JOIN friends_plans p ON p.plan_id=b.plan_id WHERE p.grant_id=? AND b.status='reserved' LIMIT 1",
                    (plan["grant_id"],),
                ).fetchone():
                    result = {"ok": False, "error": "grant_block_in_flight"}
                else:
                    usage = conn.execute(
                        "SELECT COALESCE(SUM(messages_reserved),0) AS messages,COALESCE(SUM(turns_reserved),0) AS turns FROM friends_plans WHERE grant_id=? AND contact_id=? AND channel_id=?",
                        (plan["grant_id"], plan["contact_id"], plan["channel_id"]),
                    ).fetchone()
                    block = conn.execute(
                        "SELECT * FROM friends_blocks WHERE plan_id=? AND status='pending' ORDER BY block_index LIMIT 1",
                        (plan_id,),
                    ).fetchone()
                    if block is None:
                        result = {"ok": False, "error": "plan_completed"}
                    elif (
                        plan["offer_digest"] != grant["offer_digest"]
                        or _digest(json.loads(plan["blocks_json"]))
                        != plan["blocks_digest"]
                        or block["block_digest"] != _digest(block["text"])
                        or json.loads(plan["blocks_json"])[block["block_index"] - 1]
                        != block["text"]
                    ):
                        result = {"ok": False, "error": "plan_integrity_invalid"}
                    elif (
                        grant["messages_reserved"] >= grant["global_cap"]
                        or usage["messages"] >= grant["max_messages"]
                    ):
                        result = {"ok": False, "error": "message_cap_reached"}
                    elif (
                        plan["turns_reserved"] == 0
                        and usage["turns"] >= grant["max_turns"]
                    ):
                        result = {"ok": False, "error": "turn_cap_reached"}
                    else:
                        reservation = "reserve_" + uuid.uuid4().hex[:16]
                        idem = "friends:" + block["block_id"]
                        changed = conn.execute(
                            "UPDATE friends_blocks SET status='reserved',reservation_id=?,attempts=attempts+1,reserved_at=?,updated_at=? WHERE block_id=? AND status='pending'",
                            (reservation, now_s, now_s, block["block_id"]),
                        ).rowcount
                        if changed != 1:
                            result = {"ok": False, "error": "reservation_conflict"}
                        else:
                            conn.execute(
                                "INSERT INTO friends_outbox VALUES (?,?,?,?,?,?,?,?,?)",
                                (
                                    "friends_outbox_" + uuid.uuid4().hex[:16],
                                    block["block_id"],
                                    plan_id,
                                    "reserved",
                                    idem,
                                    None,
                                    None,
                                    now_s,
                                    now_s,
                                ),
                            )
                            conn.execute(
                                "UPDATE friends_plans SET status='sending',messages_reserved=messages_reserved+1,turns_reserved=CASE WHEN turns_reserved=0 THEN 1 ELSE turns_reserved END,updated_at=? WHERE plan_id=?",
                                (now_s, plan_id),
                            )
                            conn.execute(
                                "UPDATE friends_grants SET messages_reserved=messages_reserved+1 WHERE grant_id=? AND messages_reserved < global_cap",
                                (plan["grant_id"],),
                            )
                            result = {
                                "ok": True,
                                "reservation_id": reservation,
                                "block": {
                                    "block_id": block["block_id"],
                                    "index": block["block_index"],
                                    "text": block["text"],
                                    "digest": block["block_digest"],
                                    "contact_id": plan["contact_id"],
                                    "channel_id": plan["channel_id"],
                                    "idempotency_key": idem,
                                },
                            }
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def finish_friends_block(
    plan_id: str, reservation_id: str, *, outcome: str, receipt: Any = None
) -> dict[str, Any]:
    _init()
    now = _now().isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            block = conn.execute(
                "SELECT b.*,p.grant_id,p.status AS plan_status FROM friends_blocks b JOIN friends_plans p ON p.plan_id=b.plan_id WHERE b.plan_id=? AND b.reservation_id=? AND b.status='reserved'",
                (plan_id, reservation_id),
            ).fetchone()
            if block is None:
                result = {"ok": False, "error": "reservation_not_found"}
            elif outcome == "sent" and not _valid_string(receipt, 500):
                result = {"ok": False, "error": "provider_receipt_required"}
            else:
                status = (
                    "sent"
                    if outcome == "sent"
                    else ("failed" if outcome == "failed" else "uncertain")
                )
                receipt_hash = _digest(receipt) if status == "sent" else None
                conn.execute(
                    "UPDATE friends_blocks SET status=?,receipt_hash=?,error_code=?,finished_at=?,updated_at=? WHERE block_id=?",
                    (
                        status,
                        receipt_hash,
                        None if status == "sent" else "provider_" + str(outcome),
                        now,
                        now,
                        block["block_id"],
                    ),
                )
                conn.execute(
                    "UPDATE friends_outbox SET status=?,receipt_hash=?,error_code=?,updated_at=? WHERE block_id=?",
                    (
                        status,
                        receipt_hash,
                        None if status == "sent" else "provider_" + str(outcome),
                        now,
                        block["block_id"],
                    ),
                )
                if status == "uncertain":
                    conn.execute(
                        "UPDATE friends_grants SET status='paused' WHERE grant_id=? AND status='active'",
                        (block["grant_id"],),
                    )
                    conn.execute(
                        "UPDATE friends_plans SET status='paused',updated_at=? WHERE grant_id=? AND status NOT IN ('cancelled','completed','failed_unknown')",
                        (now, block["grant_id"]),
                    )
                    plan_state = "failed_unknown"
                elif status == "failed":
                    conn.execute(
                        "UPDATE friends_plans SET status='paused',updated_at=? WHERE plan_id=? AND status NOT IN ('cancelled','completed')",
                        (now, plan_id),
                    )
                    plan_state = "failed"
                else:
                    pending = conn.execute(
                        "SELECT 1 FROM friends_blocks WHERE plan_id=? AND status='pending'",
                        (plan_id,),
                    ).fetchone()
                    desired = "sending" if pending else "completed"
                    conn.execute(
                        "UPDATE friends_plans SET status=?,updated_at=? WHERE plan_id=? AND status='sending'",
                        (desired, now, plan_id),
                    )
                    plan_state = "sent"
                if status != "sent" or not pending:
                    conn.execute(
                        "DELETE FROM friends_conversation_leases WHERE plan_id=?",
                        (plan_id,),
                    )
                result = {
                    "ok": True,
                    "status": status,
                    "plan_id": plan_id,
                    "plan_state": plan_state,
                }
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def recover_friends_orphaned_reservations() -> dict[str, Any]:
    _init()
    now = _now().isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT DISTINCT p.grant_id FROM friends_blocks b JOIN friends_plans p ON p.plan_id=b.plan_id WHERE b.status='reserved'"
            ).fetchall()
            conn.execute(
                "UPDATE friends_blocks SET status='uncertain',error_code='recovered_orphan',updated_at=? WHERE status='reserved'",
                (now,),
            )
            for row in rows:
                conn.execute(
                    "UPDATE friends_grants SET status='paused' WHERE grant_id=? AND status='active'",
                    (row["grant_id"],),
                )
                conn.execute(
                    "UPDATE friends_plans SET status='paused',updated_at=? WHERE grant_id=? AND status NOT IN ('cancelled','completed')",
                    (now, row["grant_id"]),
                )
            conn.commit()
            return {"ok": True, "grants_paused": len(rows)}
        except BaseException:
            conn.rollback()
            raise


def enqueue_friends_inbound(conn: sqlite3.Connection, *, event_id: str, contact_id: str,
                            text: str, received_at: str, profile_id: str = "default") -> int:
    """Keep enqueue atomic with the canonical inbound insertion."""
    from tools.whatsapp_ops_friends_queue import enqueue
    return enqueue(conn, event_id=event_id, contact_id=contact_id, text=text, received_at=received_at)



def revoke_friends_grant(
    grant_id: str, *, expected_revocation_version: int | None = None
) -> dict[str, Any]:
    _init()
    now = _now().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT revocation_version,status FROM friends_grants WHERE grant_id=?",
            (grant_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "grant_not_found"}
        if expected_revocation_version is not None and (
            isinstance(expected_revocation_version, bool)
            or not isinstance(expected_revocation_version, int)
            or row["revocation_version"] != expected_revocation_version
        ):
            return {"ok": False, "error": "revocation_version_conflict"}
        if row["status"] == "revoked":
            return {
                "ok": True,
                "grant_id": grant_id,
                "status": "revoked",
                "idempotent": True,
            }
        conn.execute(
            "UPDATE friends_grants SET status='revoked',revocation_version=revocation_version+1,revoked_at=? WHERE grant_id=?",
            (now, grant_id),
        )
        conn.execute(
            "UPDATE friends_plans SET status='cancelled',updated_at=? WHERE grant_id=? AND status NOT IN ('completed','cancelled')",
            (now, grant_id),
        )
        conn.execute(
            "UPDATE friends_blocks SET status='cancelled',error_code='grant_revoked',updated_at=? WHERE plan_id IN (SELECT plan_id FROM friends_plans WHERE grant_id=?) AND status='pending'",
            (now, grant_id),
        )
    return {"ok": True, "grant_id": grant_id, "status": "revoked"}


def _cancel_contact(
    contact_id: str, channel_id: str | None, reason: str, suppress: bool
) -> dict[str, Any]:
    _init()
    now = _now().isoformat()
    with _conn() as conn:
        channels = (
            [channel_id]
            if channel_id
            else [
                r["channel_id"]
                for r in conn.execute(
                    "SELECT DISTINCT channel_id FROM friends_plans WHERE contact_id=?",
                    (contact_id,),
                ).fetchall()
            ]
        )
        if suppress:
            for channel in channels:
                conn.execute(
                    "INSERT OR IGNORE INTO friends_suppressions VALUES (?,?,?,?)",
                    (contact_id, channel, reason, now),
                )
        if not channels:
            return {"ok": True, "plans_cancelled": 0}
        marks = ",".join("?" for _ in channels)
        rows = conn.execute(
            f"SELECT plan_id FROM friends_plans WHERE contact_id=? AND channel_id IN ({marks}) AND status IN ('ready','sending','paused')",
            (contact_id, *channels),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE friends_plans SET status='cancelled',updated_at=? WHERE plan_id=?",
                (now, row["plan_id"]),
            )
            conn.execute(
                "UPDATE friends_blocks SET status='cancelled',error_code=?,updated_at=? WHERE plan_id=? AND status='pending'",
                (reason, now, row["plan_id"]),
            )
    return {"ok": True, "plans_cancelled": len(rows)}


def note_friends_reply(
    contact_id: str, channel_id: str | None = None
) -> dict[str, Any]:
    return _cancel_contact(contact_id, channel_id, "reply_received", False)


def note_friends_optout(
    contact_id: str, channel_id: str | None = None
) -> dict[str, Any]:
    return _cancel_contact(contact_id, channel_id, "opt_out", True)
