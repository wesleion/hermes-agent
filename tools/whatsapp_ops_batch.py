"""Persistent, human-granted three-contact WhatsApp friends pilot ledger.

This is deliberately not a model tool.  An authenticated callback must call the
trusted activation entry point; dispatchers only consume an already-active grant.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

_MAX_TURNS = 10
_MAX_MESSAGES = 30
_MAX_BLOCKS = 3
_GLOBAL_CAP = 90
_MAX_WINDOW = timedelta(hours=2)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _valid_string(value: Any, maximum: int = 160) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _conn() -> sqlite3.Connection:
    from tools.whatsapp_ops_store import _connect
    return _connect()


def migrate_friends_batch_ledger(conn: sqlite3.Connection) -> None:
    """Add-only migration, safe inside the store's owner transaction."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS friends_grants (
      grant_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, envelope_digest TEXT NOT NULL UNIQUE,
      issuer_hash TEXT NOT NULL, contacts_json TEXT NOT NULL, actions_json TEXT NOT NULL,
      starts_at TEXT NOT NULL, expires_at TEXT NOT NULL, revocation_version INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL CHECK(status IN ('active','revoked','expired','paused')),
      max_turns INTEGER NOT NULL, max_messages INTEGER NOT NULL, max_blocks INTEGER NOT NULL,
      global_cap INTEGER NOT NULL, messages_reserved INTEGER NOT NULL DEFAULT 0,
      payload_json TEXT NOT NULL, created_at TEXT NOT NULL, activated_at TEXT NOT NULL, revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS friends_plans (
      plan_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES friends_grants(grant_id),
      contact_id TEXT NOT NULL, channel_id TEXT NOT NULL, action TEXT NOT NULL, offer_digest TEXT NOT NULL,
      context_highwatermark TEXT NOT NULL, blocks_digest TEXT NOT NULL, blocks_json TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('ready','sending','completed','cancelled','paused','failed_unknown')),
      lease_fence_hash TEXT, lease_expires_at TEXT, turns_reserved INTEGER NOT NULL DEFAULT 0,
      messages_reserved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(grant_id, contact_id, channel_id, blocks_digest)
    );
    CREATE TABLE IF NOT EXISTS friends_blocks (
      block_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES friends_plans(plan_id), block_index INTEGER NOT NULL,
      text TEXT NOT NULL, block_digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','reserved','sent','failed','uncertain','cancelled')),
      reservation_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, receipt_hash TEXT, error_code TEXT,
      reserved_at TEXT, finished_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(plan_id, block_index)
    );
    CREATE TABLE IF NOT EXISTS friends_outbox (
      outbox_id TEXT PRIMARY KEY, block_id TEXT NOT NULL UNIQUE REFERENCES friends_blocks(block_id),
      plan_id TEXT NOT NULL, status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
      receipt_hash TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_friends_blocks_plan_state ON friends_blocks(plan_id, status, block_index);
    CREATE INDEX IF NOT EXISTS ix_friends_plans_contact_state ON friends_plans(contact_id, status);
    """)


def _init() -> None:
    from tools.whatsapp_ops_store import init_db
    init_db()


def prepare_friends_envelope(*, campaign_id: Any, contacts: Any, offer: Any, playbook: Any,
                             issuer: Any, starts_at: Any, expires_at: Any,
                             actions: Any = ("offer",)) -> dict[str, Any]:
    """Validate and canonically preview an immutable grant; this never writes."""
    if not _valid_string(campaign_id) or not _valid_string(issuer):
        return {"ok": False, "error": "grant_identity_invalid"}
    if not isinstance(contacts, list) or len(contacts) != 3:
        return {"ok": False, "error": "grant_contacts_exactly_three_required"}
    normalized: list[dict[str, str]] = []
    for row in contacts:
        if not isinstance(row, dict) or not _valid_string(row.get("contact_id")) or not _valid_string(row.get("channel_id")):
            return {"ok": False, "error": "grant_contact_invalid"}
        normalized.append({"contact_id": row["contact_id"].strip(), "channel_id": row["channel_id"].strip()})
    if len({(row["contact_id"], row["channel_id"]) for row in normalized}) != 3:
        return {"ok": False, "error": "grant_contacts_not_unique"}
    if not isinstance(offer, dict) or not isinstance(playbook, dict):
        return {"ok": False, "error": "grant_envelope_invalid"}
    if not isinstance(actions, (list, tuple)) or not actions or any(not _valid_string(a, 80) for a in actions):
        return {"ok": False, "error": "grant_actions_invalid"}
    starts, expires = _iso(starts_at), _iso(expires_at)
    if starts is None or expires is None or expires <= starts or expires - starts > _MAX_WINDOW:
        return {"ok": False, "error": "grant_window_invalid"}
    envelope = {"schema": "friends_autonomous_v1", "campaign_id": campaign_id.strip(), "contacts": normalized,
                "offer": offer, "playbook": playbook, "issuer": issuer.strip(), "actions": list(actions),
                "starts_at": starts.isoformat(), "expires_at": expires.isoformat(),
                "caps": {"max_turns": _MAX_TURNS, "max_messages": _MAX_MESSAGES, "max_blocks": _MAX_BLOCKS, "global_cap": _GLOBAL_CAP},
                "revocation_version": 0}
    return {"ok": True, "mutated": False, "envelope": envelope, "envelope_digest": _digest(envelope)}


def trusted_activate_friends_grant(prepared: Any, *, decision: Any, operator_identity: Any) -> dict[str, Any]:
    """Persist one exact callback-approved envelope; not registered as a tool.

    Only the authenticated callback layer may call this internal API. A bare
    boolean trust flag, model tool, or caller-supplied expected identity is not accepted.
    """
    if not isinstance(prepared, dict) or prepared.get("ok") is not True or not isinstance(prepared.get("envelope"), dict):
        return {"ok": False, "error": "grant_preparation_invalid"}
    envelope = prepared["envelope"]
    if str(decision or "").strip().lower() != "approved":
        return {"ok": False, "error": "trusted_decision_required"}
    if not _valid_string(operator_identity) or operator_identity != envelope.get("issuer"):
        return {"ok": False, "error": "operator_mismatch"}
    if prepared.get("envelope_digest") != _digest(envelope):
        return {"ok": False, "error": "grant_digest_invalid"}
    starts, expires = _iso(envelope.get("starts_at")), _iso(envelope.get("expires_at"))
    if starts is None or expires is None or expires <= starts or expires - starts > _MAX_WINDOW:
        return {"ok": False, "error": "grant_window_invalid"}
    _init(); now = _now().isoformat(); grant_id = "friends_grant_" + uuid.uuid4().hex[:16]
    try:
        with _conn() as conn:
            conn.execute("""INSERT INTO friends_grants (grant_id,campaign_id,envelope_digest,issuer_hash,contacts_json,actions_json,starts_at,expires_at,revocation_version,status,max_turns,max_messages,max_blocks,global_cap,payload_json,created_at,activated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (grant_id, envelope["campaign_id"], prepared["envelope_digest"], _digest(operator_identity), _canonical(envelope["contacts"]), _canonical(envelope["actions"]), envelope["starts_at"], envelope["expires_at"], 0, "active", _MAX_TURNS, _MAX_MESSAGES, _MAX_BLOCKS, _GLOBAL_CAP, _canonical(envelope), now, now))
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "grant_already_activated"}
    return {"ok": True, "grant_id": grant_id, "status": "active", "expires_at": envelope["expires_at"]}


def friends_grant_status(grant_id: str) -> dict[str, Any]:
    _init()
    with _conn() as conn:
        row = conn.execute("SELECT grant_id,campaign_id,envelope_digest,status,starts_at,expires_at,revocation_version,messages_reserved,global_cap FROM friends_grants WHERE grant_id=?", (grant_id,)).fetchone()
    return {"ok": False, "error": "grant_not_found"} if row is None else {"ok": True, **dict(row)}


def revoke_friends_grant(grant_id: str, *, expected_revocation_version: int | None = None) -> dict[str, Any]:
    _init(); now = _now().isoformat()
    with _conn() as conn:
        row = conn.execute("SELECT revocation_version,status FROM friends_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None: return {"ok": False, "error": "grant_not_found"}
        if expected_revocation_version is not None and row["revocation_version"] != expected_revocation_version: return {"ok": False, "error": "revocation_version_conflict"}
        conn.execute("UPDATE friends_grants SET status='revoked', revocation_version=revocation_version+1, revoked_at=? WHERE grant_id=?", (now, grant_id))
        conn.execute("UPDATE friends_plans SET status='cancelled',updated_at=? WHERE grant_id=? AND status IN ('ready','sending','paused')", (now, grant_id))
    return {"ok": True, "grant_id": grant_id, "status": "revoked"}


def _grant_active(conn: sqlite3.Connection, grant_id: str) -> tuple[sqlite3.Row | None, str | None]:
    row = conn.execute("SELECT * FROM friends_grants WHERE grant_id=?", (grant_id,)).fetchone()
    if row is None: return None, "grant_not_found"
    now = _now()
    if row["status"] != "active": return None, "grant_not_active"
    if _iso(row["starts_at"]) > now or _iso(row["expires_at"]) <= now: return None, "grant_expired"
    return row, None


def freeze_friends_message_plan(grant_id: str, *, contact_id: Any, channel_id: Any, blocks: Any,
                                action: Any, context_highwatermark: Any, offer_digest: Any) -> dict[str, Any]:
    if not _valid_string(contact_id) or not _valid_string(channel_id) or not _valid_string(action, 80) or not _valid_string(context_highwatermark) or not isinstance(offer_digest, str) or len(offer_digest) != 64:
        return {"ok": False, "error": "plan_input_invalid"}
    if not isinstance(blocks, list) or not (1 <= len(blocks) <= _MAX_BLOCKS) or any(not _valid_string(v, 4096) for v in blocks):
        return {"ok": False, "error": "plan_blocks_invalid"}
    _init(); frozen = [v.strip() for v in blocks]; digest = _digest(frozen); now = _now().isoformat()
    with _conn() as conn:
        grant, reason = _grant_active(conn, grant_id)
        if reason: return {"ok": False, "error": reason}
        contacts = json.loads(grant["contacts_json"])
        if {"contact_id": contact_id.strip(), "channel_id": channel_id.strip()} not in contacts: return {"ok": False, "error": "contact_outside_grant"}
        if action not in json.loads(grant["actions_json"]): return {"ok": False, "error": "action_not_granted"}
        plan_id = "friends_plan_" + uuid.uuid4().hex[:16]
        try:
            conn.execute("""INSERT INTO friends_plans (plan_id,grant_id,contact_id,channel_id,action,offer_digest,context_highwatermark,blocks_digest,blocks_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (plan_id,grant_id,contact_id.strip(),channel_id.strip(),action.strip(),offer_digest,context_highwatermark.strip(),digest,_canonical(frozen),"ready",now,now))
            for index, text in enumerate(frozen, 1):
                conn.execute("INSERT INTO friends_blocks (block_id,plan_id,block_index,text,block_digest,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", ("friends_block_"+uuid.uuid4().hex[:16],plan_id,index,text,_digest(text),"pending",now,now))
        except sqlite3.IntegrityError: return {"ok": False, "error": "plan_duplicate"}
    return {"ok": True, "plan_id": plan_id, "blocks_digest": digest, "block_count": len(frozen)}


def acquire_friends_conversation_lease(plan_id: str, *, lease_seconds: Any = 60) -> dict[str, Any]:
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600: return {"ok": False, "error": "lease_seconds_invalid"}
    _init(); fence = secrets.token_urlsafe(24); expires = (_now()+timedelta(seconds=lease_seconds)).isoformat(); now=_now().isoformat()
    with _conn() as conn:
        plan=conn.execute("SELECT * FROM friends_plans WHERE plan_id=?",(plan_id,)).fetchone()
        if plan is None:return {"ok":False,"error":"plan_not_found"}
        if plan["status"] in {"cancelled","completed","failed_unknown"}: return {"ok":False,"error":"plan_"+plan["status"]}
        if _iso(plan["lease_expires_at"]) and _iso(plan["lease_expires_at"]) > _now(): return {"ok":False,"error":"lease_held"}
        conn.execute("UPDATE friends_plans SET lease_fence_hash=?,lease_expires_at=?,updated_at=? WHERE plan_id=?",(_digest(fence),expires,now,plan_id))
    return {"ok":True,"plan_id":plan_id,"fence":fence,"expires_at":expires}


def friends_plan_transport_target(plan_id: str, fence: str) -> dict[str, Any]:
    """Read the bound 1:1 target for a fence without consuming a block."""
    _init()
    with _conn() as conn:
        plan = conn.execute("SELECT * FROM friends_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None: return {"ok": False, "error": "plan_not_found"}
        if plan["status"] in {"cancelled", "completed", "failed_unknown", "paused"}: return {"ok": False, "error": "plan_" + plan["status"]}
        if plan["lease_fence_hash"] != _digest(str(fence)) or not _iso(plan["lease_expires_at"]) or _iso(plan["lease_expires_at"]) <= _now(): return {"ok": False, "error": "lease_fence_invalid"}
        _, reason = _grant_active(conn, plan["grant_id"])
        if reason: return {"ok": False, "error": reason}
        return {"ok": True, "contact_id": plan["contact_id"], "channel_id": plan["channel_id"]}


def reserve_friends_block(plan_id: str, fence: str) -> dict[str, Any]:
    _init(); now=_now(); now_s=now.isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            plan=conn.execute("SELECT * FROM friends_plans WHERE plan_id=?",(plan_id,)).fetchone()
            if plan is None: result={"ok":False,"error":"plan_not_found"}
            elif plan["status"] == "cancelled": result={"ok":False,"error":"plan_cancelled"}
            elif plan["status"] in {"failed_unknown","paused"}: result={"ok":False,"error":"plan_"+plan["status"]}
            elif plan["lease_fence_hash"] != _digest(str(fence)) or not _iso(plan["lease_expires_at"]) or _iso(plan["lease_expires_at"]) <= now: result={"ok":False,"error":"lease_fence_invalid"}
            else:
                grant, reason=_grant_active(conn,plan["grant_id"])
                if reason: result={"ok":False,"error":reason}
                elif plan["messages_reserved"] >= grant["max_messages"] or grant["messages_reserved"] >= grant["global_cap"]: result={"ok":False,"error":"message_cap_reached"}
                else:
                    inflight=conn.execute("SELECT 1 FROM friends_blocks WHERE plan_id=? AND status='reserved' LIMIT 1",(plan_id,)).fetchone()
                    block=None if inflight else conn.execute("SELECT * FROM friends_blocks WHERE plan_id=? AND status='pending' ORDER BY block_index LIMIT 1",(plan_id,)).fetchone()
                    if inflight: result={"ok":False,"error":"block_in_flight"}
                    elif block is None: result={"ok":False,"error":"plan_completed"}
                    else:
                        reservation="reserve_"+uuid.uuid4().hex[:16]; idem="friends:"+block["block_id"]
                        conn.execute("UPDATE friends_blocks SET status='reserved',reservation_id=?,attempts=attempts+1,reserved_at=?,updated_at=? WHERE block_id=? AND status='pending'",(reservation,now_s,now_s,block["block_id"]))
                        conn.execute("INSERT INTO friends_outbox (outbox_id,block_id,plan_id,status,idempotency_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",("friends_outbox_"+uuid.uuid4().hex[:16],block["block_id"],plan_id,"reserved",idem,now_s,now_s))
                        conn.execute("UPDATE friends_plans SET status='sending',messages_reserved=messages_reserved+1,turns_reserved=turns_reserved+1,updated_at=? WHERE plan_id=?",(now_s,plan_id))
                        conn.execute("UPDATE friends_grants SET messages_reserved=messages_reserved+1 WHERE grant_id=?",(plan["grant_id"],))
                        result={"ok":True,"reservation_id":reservation,"block":{"block_id":block["block_id"],"index":block["block_index"],"text":block["text"],"digest":block["block_digest"],"contact_id":plan["contact_id"],"channel_id":plan["channel_id"],"idempotency_key":idem}}
            conn.commit(); return result
        except BaseException: conn.rollback(); raise


def finish_friends_block(plan_id: str, reservation_id: str, *, outcome: str, receipt: Any = None) -> dict[str, Any]:
    _init(); now=_now().isoformat(); outcome=str(outcome or "")
    with _conn() as conn:
        block=conn.execute("SELECT * FROM friends_blocks WHERE plan_id=? AND reservation_id=? AND status='reserved'",(plan_id,reservation_id)).fetchone()
        if block is None:return {"ok":False,"error":"reservation_not_found"}
        if outcome == "sent" and not _valid_string(receipt, 500): return {"ok":False,"error":"provider_receipt_required"}
        status = "sent" if outcome == "sent" else ("failed" if outcome == "failed" else "uncertain")
        plan_status = "sending" if status == "sent" else ("paused" if status == "failed" else "failed_unknown")
        rh=_digest(receipt) if status == "sent" else None
        conn.execute("UPDATE friends_blocks SET status=?,receipt_hash=?,error_code=?,finished_at=?,updated_at=? WHERE block_id=?",(status,rh,None if status=="sent" else "provider_"+outcome,now,now,block["block_id"]))
        conn.execute("UPDATE friends_outbox SET status=?,receipt_hash=?,error_code=?,updated_at=? WHERE block_id=?",(status,rh,None if status=="sent" else "provider_"+outcome,now,block["block_id"]))
        if status == "sent":
            pending=conn.execute("SELECT 1 FROM friends_blocks WHERE plan_id=? AND status='pending' LIMIT 1",(plan_id,)).fetchone(); plan_status="sending" if pending else "completed"
        conn.execute("UPDATE friends_plans SET status=?,updated_at=? WHERE plan_id=?",(plan_status,now,plan_id))
    return {"ok":True,"status":status,"plan_id":plan_id}


def _cancel_contact(contact_id: str, reason: str) -> dict[str, Any]:
    _init(); now=_now().isoformat()
    with _conn() as conn:
        rows=conn.execute("SELECT plan_id FROM friends_plans WHERE contact_id=? AND status IN ('ready','sending','paused')",(contact_id,)).fetchall()
        for row in rows:
            conn.execute("UPDATE friends_plans SET status='cancelled',updated_at=? WHERE plan_id=?",(now,row["plan_id"]))
            conn.execute("UPDATE friends_blocks SET status='cancelled',error_code=?,updated_at=? WHERE plan_id=? AND status='pending'",(reason,now,row["plan_id"]))
    return {"ok":True,"plans_cancelled":len(rows)}


def note_friends_reply(contact_id: str) -> dict[str, Any]: return _cancel_contact(contact_id,"reply_received")
def note_friends_optout(contact_id: str) -> dict[str, Any]: return _cancel_contact(contact_id,"opt_out")
