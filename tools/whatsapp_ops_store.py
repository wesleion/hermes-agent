"""SQLite persistence for the WhatsApp Ops toolset.

All paths are profile-safe via ``get_hermes_home()``.  This module stores only
operational state; callers are responsible for avoiding raw PII in logs.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

DB_FILENAME = "wpp_ops.sqlite"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db_path(hermes_home: str | Path | None = None) -> Path:
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return home / DB_FILENAME


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> Path:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                phone_e164_hash TEXT,
                phone_e164_enc TEXT,
                whitelisted INTEGER NOT NULL DEFAULT 0,
                policy_group TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS contact_aliases (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL REFERENCES contacts(id),
                alias_norm TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(alias_norm, contact_id)
            );
            CREATE TABLE IF NOT EXISTS lists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_norm TEXT NOT NULL UNIQUE,
                allowed INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS list_members (
                list_id TEXT NOT NULL REFERENCES lists(id),
                contact_id TEXT NOT NULL REFERENCES contacts(id),
                created_at TEXT NOT NULL,
                PRIMARY KEY(list_id, contact_id)
            );
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                targets_json TEXT NOT NULL,
                message TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                send_at TEXT,
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                draft_id TEXT NOT NULL REFERENCES drafts(id),
                approval_token_hash TEXT NOT NULL UNIQUE,
                approver_ref_hash TEXT,
                message_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY,
                draft_id TEXT NOT NULL REFERENCES drafts(id),
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                scheduled_for TEXT,
                sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbound_events (
                id TEXT PRIMARY KEY,
                source_event_id_hash TEXT NOT NULL UNIQUE,
                contact_ref_hash TEXT,
                thread_ref_hash TEXT,
                payload_redacted_json TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                entity_type TEXT,
                entity_id TEXT,
                actor_ref_hash TEXT,
                safe_summary TEXT NOT NULL,
                metadata_redacted_json TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
    return db_path


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def create_draft(
    targets: list[dict[str, Any]],
    message: str,
    send_at: str | None = None,
    created_by: str | None = None,
) -> dict[str, str]:
    init_db()
    if not isinstance(targets, list) or not targets:
        raise ValueError("targets must be a non-empty list")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message is required")

    now = utc_now()
    draft_id = "draft_" + uuid.uuid4().hex[:12]
    message_hash = hash_text(message)
    idempotency_key = hash_text(
        json.dumps(targets, sort_keys=True, ensure_ascii=False) + "\n" + message + "\n" + str(send_at or "")
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO drafts (
                id, targets_json, message, message_hash, send_at, status,
                idempotency_key, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id,
                json.dumps(targets, ensure_ascii=False, sort_keys=True),
                message,
                message_hash,
                send_at,
                "draft",
                idempotency_key,
                created_by,
                now,
                now,
            ),
        )
    return {
        "draft_id": draft_id,
        "status": "draft",
        "message_hash": message_hash,
        "idempotency_key": idempotency_key,
    }


def get_draft(draft_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        return _row_to_dict(conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def create_approval(draft_id: str, timeout_minutes: int = 60) -> dict[str, str]:
    init_db()
    draft = get_draft(draft_id)
    if draft is None:
        raise ValueError("draft not found")
    token = secrets.token_urlsafe(24)
    approval_id = "approval_" + uuid.uuid4().hex[:12]
    now_dt = datetime.now(timezone.utc)
    expires_at = (now_dt + timedelta(minutes=timeout_minutes)).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO approvals (
                id, draft_id, approval_token_hash, approver_ref_hash,
                message_hash, status, expires_at, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                draft_id,
                hash_text(token),
                None,
                draft["message_hash"],
                "approved",
                expires_at,
                now_dt.isoformat(),
                now_dt.isoformat(),
            ),
        )
    return {"approval_id": approval_id, "approval_token": token, "expires_at": expires_at}


def get_valid_approval(draft_id: str, approval_token: str) -> dict[str, Any] | None:
    if not approval_token:
        return None
    init_db()
    token_hash = hash_text(approval_token)
    with _connect() as conn:
        return _row_to_dict(
            conn.execute(
                """
                SELECT * FROM approvals
                WHERE draft_id=? AND approval_token_hash=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (draft_id, token_hash),
            ).fetchone()
        )


def idempotency_used(idempotency_key: str) -> bool:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM outbox WHERE idempotency_key=? LIMIT 1",
            (idempotency_key,),
        ).fetchone()
    return row is not None


def mark_outbox_blocked(draft_id: str, idempotency_key: str, reason: str) -> None:
    init_db()
    now = utc_now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO outbox (
                id, draft_id, idempotency_key, status, attempt_count,
                last_error, scheduled_for, sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "outbox_" + uuid.uuid4().hex[:12],
                draft_id,
                idempotency_key,
                "blocked",
                0,
                reason[:500],
                None,
                None,
                now,
                now,
            ),
        )


def update_draft_status(draft_id: str, status: str, send_at: str | None = None) -> None:
    init_db()
    with _connect() as conn:
        if send_at is None:
            conn.execute(
                "UPDATE drafts SET status=?, updated_at=? WHERE id=?",
                (status, utc_now(), draft_id),
            )
        else:
            conn.execute(
                "UPDATE drafts SET status=?, send_at=?, updated_at=? WHERE id=?",
                (status, send_at, utc_now(), draft_id),
            )
