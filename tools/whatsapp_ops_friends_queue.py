"""Transactional conversation jobs; no model, transport, or public tool surface."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta

from tools.whatsapp_ops_batch import _bound_contact_current, _iso


def optout(text: str) -> str:
    value = "".join(
        c
        for c in unicodedata.normalize("NFD", text.casefold())
        if not unicodedata.combining(c)
    )
    value = re.sub(r"[^\w\s]", " ", value)
    value = " ".join(value.split())
    if value in {"pare", "parar", "stop", "sair", "remover", "cancelar", "nao quero"}:
        return "opt_out"
    if re.search(
        r"\b(nao (me )?(envie|mande|chame)|pare de (enviar|mandar)|remova (meu|o meu)|sem mais mensagens|nao quero (mais|receber))\b",
        value,
    ):
        return "opt_out"
    if re.search(r"\b(pare|parar|remover|cancelar|sair)\b", value):
        return "ambiguous_opt_out"
    return ""


def cancel_plans(conn, grant_id: str, contact_id: str | None, stamp: str) -> None:
    clause, args = (
        ("", []) if contact_id is None else (" AND contact_id=?", [contact_id])
    )
    conn.execute(
        "UPDATE friends_blocks SET status='cancelled',error_code='conversation_changed',updated_at=? WHERE status='pending' AND plan_id IN (SELECT plan_id FROM friends_plans WHERE grant_id=?"
        + clause
        + ")",
        [stamp, grant_id, *args],
    )
    conn.execute(
        "UPDATE friends_plans SET status='cancelled',updated_at=? WHERE grant_id=?"
        + clause
        + " AND status IN ('ready','sending','paused')",
        [stamp, grant_id, *args],
    )
    conn.execute(
        "DELETE FROM friends_conversation_leases WHERE grant_id=?" + clause,
        [grant_id, *args],
    )


def enqueue(
    conn, *, event_id: str, contact_id: str, text: str, received_at: str
) -> int:
    when = _iso(received_at)
    if not contact_id or not text.strip() or when is None:
        return 0
    created = 0
    grants = conn.execute(
        "SELECT g.*,p.profile_id FROM friends_grants g JOIN friends_pending_envelopes p ON p.envelope_digest=g.envelope_digest WHERE g.status='active'"
    ).fetchall()
    for grant in grants:
        if not (_iso(grant["starts_at"]) <= when < _iso(grant["expires_at"])):
            continue
        bound = next(
            (
                x
                for x in json.loads(grant["contacts_json"])
                if x["contact_id"] == contact_id
            ),
            None,
        )
        if not bound or not _bound_contact_current(
            conn, grant, contact_id, bound["channel_id"]
        ):
            continue
        key = (grant["profile_id"], grant["grant_id"], contact_id, bound["channel_id"])
        current = conn.execute(
            "SELECT * FROM friends_conversations WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=?",
            key,
        ).fetchone()
        stopped = conn.execute(
            "SELECT 1 FROM friends_suppressions WHERE contact_id=? AND channel_id=?",
            key[2:],
        ).fetchone()
        if stopped or (current and current["status"] in ("paused", "closed")):
            continue
        if conn.execute(
            "SELECT 1 FROM friends_inbound_queue WHERE event_id=?", (event_id,)
        ).fetchone():
            continue
        reason = optout(text)
        conn.execute(
            "INSERT INTO friends_inbound_queue VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                *key,
                text.strip()[:4096],
                received_at,
                "processed" if reason else "pending",
                received_at,
            ),
        )
        first = when
        if (
            current
            and current["status"] == "pending"
            and current["job_kind"] == "reply"
        ):
            first = _iso(current["first_event_at"]) or when
        due = min(
            when + timedelta(seconds=5), first + timedelta(seconds=20)
        ).isoformat()
        conn.execute(
            "INSERT INTO friends_conversations(profile_id,grant_id,contact_id,channel_id,highwatermark,first_event_at,last_event_at,due_at,status,job_kind) VALUES (?,?,?,?,?,?,?,?,?,'reply') ON CONFLICT(profile_id,grant_id,contact_id,channel_id) DO UPDATE SET highwatermark=excluded.highwatermark,first_event_at=excluded.first_event_at,last_event_at=excluded.last_event_at,due_at=excluded.due_at,status=excluded.status,job_kind='reply',generation_version=friends_conversations.generation_version+1,followup_due_at=NULL,lease_fence_hash=NULL,lease_expires_at=NULL,active_plan_id=NULL,generation_json=NULL",
            (
                *key,
                event_id,
                first.isoformat(),
                received_at,
                due,
                "paused" if reason else "pending",
            ),
        )
        cancel_plans(conn, grant["grant_id"], contact_id, received_at)
        if reason:
            # Ambiguous stop requests also suppress until an operator reconciles.
            conn.execute(
                "INSERT OR IGNORE INTO friends_suppressions VALUES (?,?,?,?)",
                (*key[2:], reason, received_at),
            )
            conn.execute(
                "INSERT OR REPLACE INTO friends_qualifications VALUES (?,?,?,?,?,?,?)",
                (*key[1:], "stop", 1, json.dumps({"reason": reason}), received_at),
            )
        created += 1
    return created


def maintain(conn, profile_id: str, now: datetime) -> None:
    """Expire/recover/seed with the caller holding BEGIN IMMEDIATE."""
    stamp = now.isoformat()
    grants = conn.execute(
        "SELECT g.* FROM friends_grants g JOIN friends_pending_envelopes p ON p.envelope_digest=g.envelope_digest WHERE p.profile_id=?",
        (profile_id,),
    ).fetchall()
    for grant in grants:
        gid = grant["grant_id"]
        if grant["status"] == "active" and _iso(grant["expires_at"]) <= now:
            conn.execute(
                "UPDATE friends_grants SET status='expired' WHERE grant_id=?", (gid,)
            )
            grant = dict(grant)
            grant["status"] = "expired"
        if grant["status"] != "active":
            cancel_plans(conn, gid, None, stamp)
            conn.execute(
                "UPDATE friends_conversations SET status='paused',followup_due_at=NULL,lease_fence_hash=NULL,lease_expires_at=NULL WHERE grant_id=?",
                (gid,),
            )
            conn.execute(
                "UPDATE friends_inbound_queue SET status='processed' WHERE grant_id=? AND status='pending'",
                (gid,),
            )
            continue
        # Only an expired provider reservation is uncertain, never a valid one.
        orphans = conn.execute(
            "SELECT b.block_id,b.plan_id FROM friends_blocks b JOIN friends_plans p ON p.plan_id=b.plan_id WHERE p.grant_id=? AND b.status='reserved' AND p.lease_expires_at<=?",
            (gid, stamp),
        ).fetchall()
        if orphans:
            for block in orphans:
                conn.execute(
                    "UPDATE friends_blocks SET status='uncertain',error_code='expired_provider_lease',updated_at=? WHERE block_id=?",
                    (stamp, block["block_id"]),
                )
                conn.execute(
                    "UPDATE friends_outbox SET status='uncertain',error_code='expired_provider_lease',updated_at=? WHERE block_id=?",
                    (stamp, block["block_id"]),
                )
            conn.execute(
                "UPDATE friends_grants SET status='paused' WHERE grant_id=?", (gid,)
            )
            cancel_plans(conn, gid, None, stamp)
            conn.execute(
                "UPDATE friends_conversations SET status='paused',followup_due_at=NULL WHERE grant_id=?",
                (gid,),
            )
            continue
        if _iso(grant["starts_at"]) > now:
            continue
        for contact in json.loads(grant["contacts_json"]):
            cid, channel = contact["contact_id"], contact["channel_id"]
            if not _bound_contact_current(conn, grant, cid, channel):
                continue
            if conn.execute(
                "SELECT 1 FROM friends_suppressions WHERE contact_id=? AND channel_id=?",
                (cid, channel),
            ).fetchone():
                continue
            conn.execute(
                "INSERT OR IGNORE INTO friends_conversations(profile_id,grant_id,contact_id,channel_id,highwatermark,due_at,status,job_kind) VALUES (?,?,?,?,?,?,'pending','open')",
                (profile_id, gid, cid, channel, "open:" + gid, stamp),
            )
        conn.execute(
            "UPDATE friends_conversations SET status='pending',lease_fence_hash=NULL,lease_expires_at=NULL WHERE grant_id=? AND status='generating' AND lease_expires_at<=?",
            (gid, stamp),
        )
        conn.execute(
            "UPDATE friends_conversations SET status='pending',job_kind='followup',highwatermark='followup:'||processed_cursor,due_at=?,active_plan_id=NULL,generation_json=NULL,followup_due_at=NULL WHERE grant_id=? AND status='idle' AND followup_sent=0 AND followup_due_at<=?",
            (stamp, gid, stamp),
        )


def history(conn, row) -> list[dict[str, str]]:
    key = (row["grant_id"], row["contact_id"], row["channel_id"])
    inbound = conn.execute(
        "SELECT text,received_at AS stamp,rowid AS seq FROM friends_inbound_queue WHERE grant_id=? AND contact_id=? AND channel_id=? ORDER BY received_at DESC,rowid DESC LIMIT 24",
        key,
    ).fetchall()
    sent = conn.execute(
        "SELECT b.text,b.finished_at AS stamp,b.block_index AS seq FROM friends_blocks b JOIN friends_plans p ON p.plan_id=b.plan_id WHERE p.grant_id=? AND p.contact_id=? AND p.channel_id=? AND b.status='sent' ORDER BY b.finished_at DESC,b.block_index DESC LIMIT 24",
        key,
    ).fetchall()
    events = [(x["stamp"], 0, x["seq"], "user", x["text"]) for x in inbound]
    events += [(x["stamp"], 1, x["seq"], "assistant", x["text"]) for x in sent]
    return [{"role": x[3], "text": x[4]} for x in sorted(events)[-24:]]
