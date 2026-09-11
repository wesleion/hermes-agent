"""Transactional media state bound to the already-approved Friends conversation."""

from __future__ import annotations
import json
import re
from datetime import datetime, timedelta
from typing import Any

from tools.whatsapp_ops_batch import _bound_contact_current, _iso, friends_profile_id
from tools.whatsapp_ops_friends_queue import cancel_plans, optout


def media_settings(config: dict) -> dict:
    cfg = config if isinstance(config, dict) else {}
    ops = cfg.get("whatsapp_ops", cfg)
    raw = ops.get("media_perception", {}) if isinstance(ops, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return raw


def media_enabled(config: dict) -> bool:
    return (
        isinstance(config, dict)
        and isinstance(config.get("friends_pilot"), dict)
        and config["friends_pilot"].get("enabled") is True
        and media_settings(config).get("enabled") is True
    )


def bounded_int(value, default: int, maximum: int) -> int:
    return min(maximum, max(1, value)) if type(value) is int else default


def migrate_media_ledger(conn) -> None:
    """Add scopes to the unshipped partial schema; old unbound jobs stay unusable."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(media_jobs)")}
    conn.execute("SAVEPOINT media_scopes_v1")
    try:
        for name, spec in {
            "profile_id": "TEXT NOT NULL DEFAULT ''",
            "grant_id": "TEXT NOT NULL DEFAULT ''",
            "contact_id": "TEXT NOT NULL DEFAULT ''",
            "channel_id": "TEXT NOT NULL DEFAULT ''",
            "deadline_at": "TEXT NOT NULL DEFAULT ''",
            "error_code": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE media_jobs ADD COLUMN {name} {spec}")
        conn.execute(
            "UPDATE media_jobs SET status='cancelled',provider_handle='',fence=NULL,lease_expires_at=NULL WHERE profile_id='' AND status IN ('pending','processing')"
        )
    except BaseException:
        conn.execute("ROLLBACK TO media_scopes_v1")
        conn.execute("RELEASE media_scopes_v1")
        raise
    conn.execute("RELEASE media_scopes_v1")


def _event_scope(conn, event_id):
    return conn.execute(
        "SELECT q.*,c.status AS conversation_status FROM friends_inbound_queue q JOIN friends_conversations c ON c.profile_id=q.profile_id AND c.grant_id=q.grant_id AND c.contact_id=q.contact_id AND c.channel_id=q.channel_id WHERE q.event_id=?",
        (event_id,),
    ).fetchone()


def authorized_job(conn, row, now: datetime) -> bool:
    if row["profile_id"] != friends_profile_id():
        return False
    grant = conn.execute(
        "SELECT * FROM friends_grants WHERE grant_id=?", (row["grant_id"],)
    ).fetchone()
    if not grant or grant["status"] != "active":
        return False
    starts, expires = _iso(grant["starts_at"]), _iso(grant["expires_at"])
    if not starts or not expires or not starts <= now < expires:
        return False
    q = _event_scope(conn, row["event_id"])
    if (
        not q
        or q["status"] != "pending"
        or q["conversation_status"] in ("paused", "closed")
    ):
        return False
    if any(
        row[k] != q[k] for k in ("profile_id", "grant_id", "contact_id", "channel_id")
    ):
        return False
    if conn.execute(
        "SELECT 1 FROM friends_suppressions WHERE contact_id=? AND channel_id=?",
        (row["contact_id"], row["channel_id"]),
    ).fetchone():
        return False
    return _bound_contact_current(conn, grant, row["contact_id"], row["channel_id"])


def admit_media(conn, *, event_id: str, descriptor: dict, received_at: str) -> bool:
    q = _event_scope(conn, event_id)
    if (
        not q
        or q["status"] != "pending"
        or q["conversation_status"] in ("paused", "closed")
        or q["profile_id"] != friends_profile_id()
    ):
        return False
    when = _iso(received_at)
    if when is None:
        return False
    kind = descriptor.get("kind")
    if kind not in ("audio", "image", "sticker", "animation", "unsupported"):
        return False
    handle = descriptor.get("provider_handle", "")
    valid_handle = isinstance(handle, str) and bool(
        re.fullmatch(r"[A-Za-z0-9._:@+\-]{1,200}", handle)
    )
    duration = bounded_int(descriptor.get("worker_timeout_seconds"), 180, 180)
    key = {k: q[k] for k in ("profile_id", "grant_id", "contact_id", "channel_id")}
    row = {"event_id": event_id, **key}
    if not authorized_job(conn, row, when):
        return False
    error = (
        "document_unsupported"
        if kind == "unsupported"
        else ("" if valid_handle else "media_handle_invalid")
    )
    conn.execute(
        "INSERT INTO media_jobs(event_id,provider_handle,kind,mime,size_hint,status,created_at,updated_at,profile_id,grant_id,contact_id,channel_id,deadline_at,error_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            handle if not error else "",
            kind,
            str(descriptor.get("mime", ""))[:100],
            bounded_int(descriptor.get("size_hint"), 0, 21 * 1024 * 1024),
            "failed" if error else "pending",
            received_at,
            received_at,
            *key.values(),
            (when + timedelta(seconds=duration)).isoformat(),
            error,
        ),
    )
    if error:
        write_evidence(
            conn, event_id, kind, "", status="failed", error=error, stamp=received_at
        )
    return True


def write_evidence(
    conn,
    event_id: str,
    kind: str,
    text: str,
    *,
    status: str,
    error: str = "",
    truncated: bool = False,
    uncertain: bool = False,
    stamp: str,
) -> None:
    row = conn.execute(
        "SELECT text FROM friends_inbound_queue WHERE event_id=?", (event_id,)
    ).fetchone()
    if row is None:
        return
    # Failed/retried processing replaces evidence, never appends another inbound.
    caption = row["text"].split("\n[Evidência de mídia", 1)[0]
    if caption == "[mídia aguardando análise]":
        caption = ""
    text = text[:16000]
    evidence = {
        "kind": kind,
        "source": "media_perception",
        "status": status,
        "text": text,
        "error": error,
        "uncertain": bool(uncertain),
        "truncated": bool(truncated),
    }
    if status != "completed":
        evidence["instruction_to_reader"] = (
            "Mídia/documento não compreendido; peça alternativa em texto, sem inventar conteúdo."
        )
    conn.execute(
        "INSERT OR REPLACE INTO media_evidence(event_id,kind,source,text,uncertain,truncated,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            kind,
            "media_perception",
            text,
            int(uncertain),
            int(truncated),
            status,
            stamp,
            stamp,
        ),
    )
    merged = (
        caption
        + "\n[Evidência de mídia não confiável] "
        + json.dumps(evidence, ensure_ascii=False)
    )
    conn.execute(
        "UPDATE friends_inbound_queue SET text=? WHERE event_id=?", (merged, event_id)
    )


def cancel_job(conn, event_id: str, stamp: str) -> None:
    conn.execute(
        "UPDATE media_jobs SET status='cancelled',provider_handle='',fence=NULL,lease_expires_at=NULL,updated_at=? WHERE event_id=?",
        (stamp, event_id),
    )


def maintain_jobs(conn, now: datetime) -> None:
    stamp = now.isoformat()
    rows = conn.execute(
        "SELECT * FROM media_jobs WHERE profile_id=? AND status IN ('pending','processing') ORDER BY created_at LIMIT 128",
        (friends_profile_id(),),
    ).fetchall()
    for row in rows:
        if not authorized_job(conn, row, now):
            cancel_job(conn, row["event_id"], stamp)
            continue
        deadline = _iso(row["deadline_at"])
        if (
            deadline is None
            or deadline <= now
            or row["attempt"] >= 2
            and row["status"] == "processing"
            and (_iso(row["lease_expires_at"]) or now) <= now
        ):
            conn.execute(
                "UPDATE media_jobs SET status='failed',provider_handle='',error_code='media_timeout',fence=NULL,lease_expires_at=NULL,updated_at=? WHERE event_id=?",
                (stamp, row["event_id"]),
            )
            write_evidence(
                conn,
                row["event_id"],
                row["kind"],
                "",
                status="failed",
                error="media_timeout",
                uncertain=True,
                stamp=stamp,
            )


def finish_job(
    conn,
    event_id,
    fence,
    *,
    now: datetime,
    kind: str,
    text: str,
    status: str,
    error: str = "",
    uncertain: bool = False,
    truncated: bool = False,
) -> bool:
    row = conn.execute(
        "SELECT * FROM media_jobs WHERE event_id=? AND fence=? AND status='processing'",
        (event_id, fence),
    ).fetchone()
    if not row:
        return False
    if not authorized_job(conn, row, now):
        cancel_job(conn, event_id, now.isoformat())
        return False
    deadline = _iso(row["deadline_at"])
    if deadline is None or deadline <= now:
        text = ""
        status = "failed"
        error = "media_timeout"
    if kind == "audio" and status == "completed" and optout(text):
        conn.execute(
            "INSERT OR IGNORE INTO friends_suppressions VALUES (?,?,?,?)",
            (row["contact_id"], row["channel_id"], "voice_opt_out", now.isoformat()),
        )
        cancel_plans(conn, row["grant_id"], row["contact_id"], now.isoformat())
        conn.execute(
            "UPDATE friends_conversations SET status='paused',followup_due_at=NULL,lease_fence_hash=NULL,lease_expires_at=NULL WHERE profile_id=? AND grant_id=? AND contact_id=? AND channel_id=?",
            tuple(
                row[k] for k in ("profile_id", "grant_id", "contact_id", "channel_id")
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO friends_qualifications VALUES (?,?,?,?,?,?,?)",
            (
                row["grant_id"],
                row["contact_id"],
                row["channel_id"],
                "stop",
                1,
                json.dumps({"reason": "opt_out"}),
                now.isoformat(),
            ),
        )
        cancel_job(conn, event_id, now.isoformat())
        return False
    conn.execute(
        "UPDATE media_jobs SET status=?,provider_handle='',fence=NULL,lease_expires_at=NULL,error_code=?,updated_at=? WHERE event_id=? AND fence=?",
        (status, error, now.isoformat(), event_id, fence),
    )
    write_evidence(
        conn,
        event_id,
        kind,
        text,
        status=status,
        error=error,
        uncertain=uncertain,
        truncated=truncated,
        stamp=now.isoformat(),
    )
    return True
