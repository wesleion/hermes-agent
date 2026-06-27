"""SQLite persistence for the WhatsApp Ops toolset.

All paths are profile-safe via ``get_hermes_home()``.  This module stores only
operational state; callers are responsible for avoiding raw PII in logs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML is normally available in Hermes
    yaml = None  # type: ignore[assignment]

DB_FILENAME = "wpp_ops.sqlite"

# Raw refs staged for registration are purged after this age.
_STAGING_TTL = timedelta(minutes=5)
_STAGING_TTL_MIN_SECONDS = 60
_STAGING_TTL_MAX_SECONDS = 7 * 24 * 60 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registration_staging_ttl() -> timedelta:
    """Return the profile-configured registration staging TTL.

    The framework default stays conservative (5 minutes), but operator-facing
    gateway profiles can opt into a longer window with:

      whatsapp_ops.registration_staging_ttl_seconds: <seconds>

    This is behavioral config, not a secret. Values are clamped to avoid raw
    refs being kept indefinitely by accident.
    """
    if yaml is None:
        return _STAGING_TTL
    try:
        config_path = get_hermes_home() / "config.yaml"
        data = yaml.safe_load(config_path.read_text()) or {}
        wpp = data.get("whatsapp_ops") if isinstance(data, dict) else {}
        if not isinstance(wpp, dict):
            return _STAGING_TTL
        raw = wpp.get("registration_staging_ttl_seconds")
        if raw is None:
            raw = wpp.get("registration_staging_ttl")
        seconds = int(raw)
        seconds = max(_STAGING_TTL_MIN_SECONDS, min(seconds, _STAGING_TTL_MAX_SECONDS))
        return timedelta(seconds=seconds)
    except Exception:
        return _STAGING_TTL


def registration_staging_ttl_seconds() -> int:
    """Public, sanitized TTL value for status/UX output."""
    return int(_registration_staging_ttl().total_seconds())


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
            CREATE TABLE IF NOT EXISTS registration_staging (
                id TEXT PRIMARY KEY,
                contact_ref_raw TEXT,
                thread_ref_raw TEXT,
                display_name TEXT,
                kind TEXT NOT NULL DEFAULT 'inbound',
                expires_at TEXT NOT NULL,
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
        _ensure_registration_staging_columns(conn)
        _backfill_registration_staging_metadata(conn)
    return db_path



def _ensure_registration_staging_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(registration_staging)")}
    additions = {
        "ref_hash": "ref_hash TEXT",
        "last_seen_at": "last_seen_at TEXT",
        "message_count": "message_count INTEGER NOT NULL DEFAULT 1",
        "safe_hint_json": "safe_hint_json TEXT",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE registration_staging ADD COLUMN {ddl}")



def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None

def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _phone_digits(value: str) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def mask_phone(value: str | None) -> str:
    digits = _phone_digits(value or "")
    if not digits:
        return ""
    if len(digits) <= 4:
        return "****" + digits[-2:]
    if digits.startswith("55"):
        prefix = "+55"
    else:
        prefix = "+" + digits[:2] if len(digits) >= 12 else "+" + digits[:1]
    return f"{prefix}***{digits[-4:]}"


def _safe_contact_id(contact_id: str) -> str:
    raw = str(contact_id or "")
    if "@" in raw or _phone_digits(raw):
        return "contact_" + hash_text(raw)[:16]
    return raw


def _safe_contact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact_id": _safe_contact_id(row["id"]),
        "display_name": row["display_name"],
        "phone_masked": mask_phone(row.get("phone_e164_enc")),
        "whitelisted": bool(row.get("whitelisted")),
        "policy_group": row.get("policy_group"),
    }



def _safe_registration_id(kind: str, raw_ref: str) -> str:
    prefix = "grp" if kind == "group" else "ctt"
    return f"{prefix}_{hash_text(raw_ref)[:8]}"



def _clean_registration_text(value: Any, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    if re.search(r"(?i)https?://", text) or "@" in text:
        return ""
    text = re.sub(r"\+?\d[\d\s().-]{6,}\d", "", text).strip()
    return text[:limit]



def _safe_message_type(value: Any) -> str:
    text = re.sub(r"[^a-z0-9_.-]+", "", str(value or "").strip().lower())
    return text[:40]



def _coerce_safe_hint(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    hint: dict[str, Any] = {}
    for key in ("display_name", "group_name", "participant_name", "source_group_name"):
        cleaned = _clean_registration_text(value.get(key))
        if cleaned:
            hint[key] = cleaned
    for key in ("safe_id", "source_group_safe_id"):
        cleaned = _safe_message_type(value.get(key))
        if cleaned:
            hint[key] = cleaned
    msg_type = _safe_message_type(value.get("last_message_type"))
    if msg_type:
        hint["last_message_type"] = msg_type
    if "has_media" in value:
        hint["has_media"] = bool(value.get("has_media"))
    return hint



def _raw_ref_for_kind(kind: str, contact_ref: str, thread_ref: str) -> str:
    if kind == "group":
        return str(thread_ref or contact_ref or "")
    return str(contact_ref or thread_ref or "")



def _build_staging_hint(
    *,
    kind: str,
    raw_ref: str,
    contact_ref: str = "",
    thread_ref: str = "",
    display_name: str = "",
    safe_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hint = _coerce_safe_hint(safe_hint)
    hint["safe_id"] = _safe_registration_id(kind, raw_ref)
    digits = _phone_digits(raw_ref)
    if kind == "contact" and digits:
        masked = mask_phone(raw_ref)
        if masked:
            hint["phone_masked"] = masked
        hint["last4"] = digits[-4:]
    if kind == "contact" and thread_ref and thread_ref != contact_ref:
        hint["source_group_safe_id"] = _safe_registration_id("group", thread_ref)
        source_name = _clean_registration_text(hint.get("source_group_name") or "")
        if source_name:
            hint["source_group_name"] = source_name
    display = _clean_registration_text(display_name)
    if kind == "group":
        display = hint.get("group_name") or display
    else:
        display = hint.get("participant_name") or display
    if display:
        hint["display_name"] = display
    return hint



def _load_staging_hint(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}



def _merge_staging_hints(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(old)
    for key, value in new.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged



def _backfill_registration_staging_metadata(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            """
            SELECT id, contact_ref_raw, thread_ref_raw, display_name, kind,
                   created_at, ref_hash, last_seen_at, safe_hint_json
            FROM registration_staging
            WHERE ref_hash IS NULL OR last_seen_at IS NULL OR safe_hint_json IS NULL
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        kind = str(row["kind"] or "contact").strip().lower()
        if kind not in {"contact", "group"}:
            kind = "group" if row["thread_ref_raw"] and row["thread_ref_raw"] != row["contact_ref_raw"] else "contact"
        contact_ref = str(row["contact_ref_raw"] or "")
        thread_ref = str(row["thread_ref_raw"] or "")
        raw_ref = _raw_ref_for_kind(kind, contact_ref, thread_ref)
        if not raw_ref:
            continue
        existing_hint = _load_staging_hint(row["safe_hint_json"])
        hint = _merge_staging_hints(existing_hint, _build_staging_hint(
            kind=kind,
            raw_ref=raw_ref,
            contact_ref=contact_ref,
            thread_ref=thread_ref,
            display_name=row["display_name"] or "",
            safe_hint=existing_hint,
        ))
        conn.execute(
            """
            UPDATE registration_staging
            SET ref_hash=?, last_seen_at=COALESCE(last_seen_at, created_at),
                message_count=COALESCE(message_count, 1), safe_hint_json=?
            WHERE id=?
            """,
            (hash_text(raw_ref), json.dumps(hint, ensure_ascii=False, sort_keys=True), row["id"]),
        )



# ---------------------------------------------------------------------------
# Staging raw inbound refs for registration
# ---------------------------------------------------------------------------


def _staging_cleanup() -> None:
    """Remove expired staging rows."""
    now = utc_now()
    with _connect() as conn:
        conn.execute("DELETE FROM registration_staging WHERE expires_at <= ?", (now,))


def stage_raw_ref(
    *,
    contact_ref: str = "",
    thread_ref: str = "",
    display_name: str = "",
    kind: str | None = None,
    safe_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store raw inbound refs temporarily for registration use.

    Auto-purges old staging first.  Expires after the profile TTL.
    The caller is responsible for calling this only from trusted ingest
    code paths, never from model-driven tool calls.
    """
    init_db()
    _staging_cleanup()
    contact_ref = str(contact_ref or "")
    thread_ref = str(thread_ref or "")
    kind_norm = str(kind or "").strip().lower()
    if kind_norm not in {"contact", "group"}:
        kind_norm = "group" if thread_ref and thread_ref != contact_ref else "contact"
    raw_ref = _raw_ref_for_kind(kind_norm, contact_ref, thread_ref)
    if not raw_ref:
        return {"ok": False, "error": "no_ref"}

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    expires_at = (now_dt + _registration_staging_ttl()).isoformat()
    ref_hash = hash_text(raw_ref)
    hint = _build_staging_hint(
        kind=kind_norm,
        raw_ref=raw_ref,
        contact_ref=contact_ref,
        thread_ref=thread_ref,
        display_name=display_name,
        safe_hint=safe_hint,
    )
    display = hint.get("display_name", "")[:80]

    with _connect() as conn:
        existing = conn.execute(
            """
            SELECT * FROM registration_staging
            WHERE kind=? AND ref_hash=? AND expires_at > ?
            ORDER BY COALESCE(last_seen_at, created_at) DESC, created_at DESC
            LIMIT 1
            """,
            (kind_norm, ref_hash, utc_now()),
        ).fetchone()
        if existing:
            row_id = existing["id"]
            merged_hint = _merge_staging_hints(_load_staging_hint(existing["safe_hint_json"]), hint)
            message_count = int(existing["message_count"] or 1) + 1
            conn.execute(
                """
                UPDATE registration_staging
                SET contact_ref_raw=?, thread_ref_raw=?, display_name=?, expires_at=?,
                    last_seen_at=?, message_count=?, safe_hint_json=?
                WHERE id=?
                """,
                (
                    contact_ref or None,
                    thread_ref or None,
                    display,
                    expires_at,
                    now,
                    message_count,
                    json.dumps(merged_hint, ensure_ascii=False, sort_keys=True),
                    row_id,
                ),
            )
        else:
            row_id = "staging_" + uuid.uuid4().hex[:12]
            conn.execute(
                """
                INSERT INTO registration_staging (
                    id, contact_ref_raw, thread_ref_raw, display_name,
                    kind, expires_at, created_at, ref_hash, last_seen_at,
                    message_count, safe_hint_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    contact_ref or None,
                    thread_ref or None,
                    display,
                    kind_norm,
                    expires_at,
                    now,
                    ref_hash,
                    now,
                    1,
                    json.dumps(hint, ensure_ascii=False, sort_keys=True),
                ),
            )
            message_count = 1
    return {
        "ok": True,
        "staging_id": row_id,
        "kind": kind_norm,
        "expires_at": expires_at,
        "safe_id": hint.get("safe_id", ""),
        "message_count": message_count,
    }



def _delete_staging_row(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    ref_hash = row.get("ref_hash")
    if ref_hash:
        conn.execute(
            "DELETE FROM registration_staging WHERE kind=? AND ref_hash=?",
            (row.get("kind"), ref_hash),
        )
    else:
        conn.execute("DELETE FROM registration_staging WHERE id=?", (row["id"],))



def _latest_staging(kind: str = "contact", staging_id: str = "") -> dict[str, Any] | None:
    """Return and consume a non-expired staging row for *kind*."""
    init_db()
    _staging_cleanup()
    kind_norm = str(kind or "contact").strip().lower()
    if kind_norm not in {"contact", "group"}:
        kind_norm = "contact"
    if staging_id:
        where = "id=? AND kind=? AND expires_at > ?"
        params: tuple[Any, ...] = (staging_id, kind_norm, utc_now())
    else:
        where = "kind=? AND expires_at > ?"
        params = (kind_norm, utc_now())
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM registration_staging
            WHERE {where}
            ORDER BY COALESCE(last_seen_at, created_at) DESC, created_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        _delete_staging_row(conn, result)
    return result



def consume_latest_raw_ref(kind: str = "contact", staging_id: str = "") -> str | None:
    """Return the raw ref from staging, optionally selecting a staging id.

    ``kind="contact"`` returns ``contact_ref_raw``.
    ``kind="group"`` returns ``thread_ref_raw``.
    Matching staging rows are consumed after read.
    """
    row = _latest_staging(kind, staging_id=staging_id)
    if row is None:
        return None
    kind_norm = str(kind or "contact").strip().lower()
    key = "thread_ref_raw" if kind_norm == "group" else "contact_ref_raw"
    return str(row.get(key) or "") or None



def _public_staging_item(row: dict[str, Any]) -> dict[str, Any]:
    kind = str(row.get("kind") or "contact")
    hint = _load_staging_hint(row.get("safe_hint_json"))
    raw_ref = _raw_ref_for_kind(kind, str(row.get("contact_ref_raw") or ""), str(row.get("thread_ref_raw") or ""))
    if raw_ref and not hint.get("safe_id"):
        hint["safe_id"] = _safe_registration_id(kind, raw_ref)
    display_name = row.get("display_name") or hint.get("display_name") or ""
    if not display_name:
        display_name = hint.get("group_name") or hint.get("participant_name") or ""
    item = {
        "staging_id": row["id"],
        "display_name": display_name,
        "kind": kind,
        "created_at": row.get("created_at"),
        "last_seen_at": row.get("last_seen_at") or row.get("created_at"),
        "available_until": row.get("expires_at"),
        "message_count": int(row.get("message_count") or 1),
        "safe_id": hint.get("safe_id") or "",
    }
    for key in (
        "phone_masked",
        "last4",
        "source_group_safe_id",
        "source_group_name",
        "last_message_type",
        "has_media",
    ):
        if key in hint and hint[key] not in (None, ""):
            item[key] = hint[key]
    return item



def peek_staging() -> list[dict[str, Any]]:
    """Return sanitized info about what's staged for registration (no raw refs)."""
    init_db()
    _staging_cleanup()
    with _connect() as conn:
        rows = [dict(row) for row in conn.execute(
            """
            SELECT *
            FROM registration_staging
            WHERE expires_at > ?
            ORDER BY COALESCE(last_seen_at, created_at) DESC, created_at DESC
            """,
            (utc_now(),),
        ).fetchall()]
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("kind") or "contact"), str(row.get("ref_hash") or row.get("id")))
        if key not in grouped:
            grouped[key] = dict(row)
            grouped[key]["message_count"] = int(row.get("message_count") or 1)
            continue
        grouped[key]["message_count"] = int(grouped[key].get("message_count") or 1) + int(row.get("message_count") or 1)
    return [_public_staging_item(row) for row in grouped.values()]



def registration_staging_diagnostics() -> dict[str, Any]:
    """Return sanitized registration-staging diagnostics for operator UX."""
    init_db()
    _staging_cleanup()
    with _connect() as conn:
        staging_count = conn.execute(
            "SELECT COUNT(*) FROM registration_staging WHERE expires_at > ?",
            (utc_now(),),
        ).fetchone()[0]
        inbound_count = conn.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0]
        latest = conn.execute(
            "SELECT created_at FROM inbound_events ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    latest_created_at = latest["created_at"] if latest else None
    empty_reason = "none"
    if not staging_count:
        empty_reason = "no_inbound_yet" if not inbound_count else "no_active_staging_or_expired"
    return {
        "staging_ttl_seconds": registration_staging_ttl_seconds(),
        "staged_count": int(staging_count),
        "inbound_count": int(inbound_count),
        "latest_inbound_created_at": latest_created_at,
        "empty_reason": empty_reason,
    }


# ---------------------------------------------------------------------------
# Contacts & aliases
# ---------------------------------------------------------------------------


def upsert_contact(
    *,
    contact_id: str,
    display_name: str,
    phone_e164: str = "",
    aliases: list[str] | None = None,
    whitelisted: bool = False,
    policy_group: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    init_db()
    if not contact_id or not display_name:
        raise ValueError("contact_id and display_name are required")
    now = utc_now()
    phone_digits = _phone_digits(phone_e164)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO contacts (
                id, display_name, phone_e164_hash, phone_e164_enc, whitelisted,
                policy_group, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                phone_e164_hash=excluded.phone_e164_hash,
                phone_e164_enc=excluded.phone_e164_enc,
                whitelisted=excluded.whitelisted,
                policy_group=excluded.policy_group,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                contact_id,
                display_name,
                hash_text(phone_digits) if phone_digits else None,
                phone_e164,
                1 if whitelisted else 0,
                policy_group,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        alias_values = {_normalize(display_name), *( _normalize(alias) for alias in (aliases or []) )}
        if phone_digits:
            alias_values.add(phone_digits)
        for alias_norm in sorted(a for a in alias_values if a):
            conn.execute(
                """
                INSERT OR IGNORE INTO contact_aliases (id, contact_id, alias_norm, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ("alias_" + uuid.uuid4().hex[:12], contact_id, alias_norm, now),
            )
    return {
        "contact_id": contact_id,
        "display_name": display_name,
        "phone_masked": mask_phone(phone_e164),
        "whitelisted": bool(whitelisted),
        "policy_group": policy_group,
    }


def resolve_contact(query: str) -> dict[str, Any]:
    init_db()
    query_norm = _normalize(query)
    query_digits = _phone_digits(query)
    if not query_norm and not query_digits:
        return {"ok": True, "ambiguous": True, "matches": [], "query": ""}
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT c.*
            FROM contacts c
            LEFT JOIN contact_aliases a ON a.contact_id = c.id
            WHERE c.id = ?
               OR a.alias_norm = ?
               OR (? != '' AND c.phone_e164_hash = ?)
               OR lower(c.display_name) LIKE ?
            ORDER BY c.display_name ASC
            LIMIT 6
            """,
            (query, query_norm, query_digits, hash_text(query_digits) if query_digits else "", f"%{query_norm}%"),
        ).fetchall()
    matches = [_safe_contact(dict(row)) for row in rows]
    if len(matches) == 1:
        return {"ok": True, "ambiguous": False, "match": matches[0], "matches": matches}
    return {"ok": True, "ambiguous": True, "matches": matches, "query": str(query)[:80]}


def list_contacts(filter_text: str = "", limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    filter_norm = _normalize(filter_text)
    limit = max(1, min(int(limit or 50), 100))
    with _connect() as conn:
        if filter_norm:
            rows = conn.execute(
                """
                SELECT DISTINCT c.*
                FROM contacts c
                LEFT JOIN contact_aliases a ON a.contact_id = c.id
                WHERE a.alias_norm LIKE ?
                   OR lower(c.display_name) LIKE ?
                   OR lower(COALESCE(c.metadata_json, '')) LIKE ?
                ORDER BY c.display_name ASC
                LIMIT ?
                """,
                (f"%{filter_norm}%", f"%{filter_norm}%", f"%{filter_norm}%", limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM contacts ORDER BY display_name ASC LIMIT ?", (limit,)).fetchall()
    return [_safe_contact(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Local registration (writes to SQLite directly, not via env/Infisical)
# ---------------------------------------------------------------------------


def register_contact_local(
    *,
    alias: str,
    raw_ref: str,
    display_name: str | None = None,
    policy_group: str = "manual",
    allow_send: bool = False,
) -> dict[str, Any]:
    """Register a contact in the local SQLite allowlist cache.

    This writes directly to the operational DB, bypassing Infisical/env.
    The registration survives runtime sync_allowlist_from_env() calls
    because upsert only adds/updates — it never deletes existing rows.

    Returns sanitized contact info (no raw ref in output).
    """
    init_db()
    if not alias.strip():
        return {"ok": False, "error": "alias_required"}
    if not raw_ref.strip():
        return {"ok": False, "error": "raw_ref_required"}
    contact_id = _contact_id_from_target(raw_ref)
    display = (display_name or alias).strip() or alias
    metadata = {
        "source": "local_registration",
        "target_ref_hash": hash_text(raw_ref),
    }
    try:
        result = upsert_contact(
            contact_id=contact_id,
            display_name=display,
            aliases=[alias],
            whitelisted=allow_send,
            policy_group=policy_group,
            metadata=metadata,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True, **result}


def register_group_local(
    *,
    alias: str,
    raw_ref: str,
    display_name: str | None = None,
    policy_group: str = "manual",
    allow_send: bool = False,
) -> dict[str, Any]:
    """Register a group in the local SQLite allowlist cache.

    Same semantics as ``register_contact_local`` but for groups/lists.
    """
    init_db()
    if not alias.strip():
        return {"ok": False, "error": "alias_required"}
    if not raw_ref.strip():
        return {"ok": False, "error": "raw_ref_required"}
    list_id = "list_" + hash_text(raw_ref)[:16]
    name = (display_name or alias).strip() or alias
    now = utc_now()
    metadata = {
        "source": "local_registration",
        "target_ref_hash": hash_text(raw_ref),
    }
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO lists (id, name, name_norm, allowed, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name_norm) DO UPDATE SET
                name=excluded.name,
                allowed=excluded.allowed,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                list_id,
                name,
                _normalize(name),
                1 if allow_send else 0,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
    return {
        "ok": True,
        "group_id": list_id,
        "name": name,
        "allow_send": bool(allow_send),
        "policy_group": policy_group,
    }


# ---------------------------------------------------------------------------
# Env-based allowlist sync
# ---------------------------------------------------------------------------


def _load_allowlist_json(env_name: str, default: Any, *fallback_env_names: str) -> Any:
    raw = ""
    for candidate in (env_name, *fallback_env_names):
        raw = os.environ.get(candidate, "").strip()
        if raw:
            break
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("allowlist_json_invalid") from exc


def _iter_alias_map_entries(alias_map: Any) -> list[dict[str, Any]]:
    if not isinstance(alias_map, dict):
        return []
    entries: list[dict[str, Any]] = []
    for alias, value in alias_map.items():
        if isinstance(value, dict):
            item = dict(value)
            item.setdefault("alias", alias)
            entries.append(item)
        elif isinstance(value, str):
            entries.append({"alias": alias, "target_ref": value, "kind": "contact"})
    return entries


def _contact_id_from_target(target_ref: str) -> str:
    return "contact_" + hash_text(target_ref)[:16]


def _sync_contact_item(item: dict[str, Any]) -> bool:
    alias = str(item.get("alias") or "").strip()
    target_ref = str(item.get("target_ref") or "").strip()
    kind = str(item.get("kind") or "contact").strip().lower()
    if kind not in {"contact", "dm"}:
        return False
    if not alias or not target_ref:
        raise ValueError("allowlist_entry_invalid")
    display_name = str(item.get("display_name") or alias).strip() or alias
    metadata = {
        "source": "env",
        "target_ref_hash": hash_text(target_ref),
        "allow_receive": bool(item.get("allow_receive", True)),
        "allow_send": bool(item.get("allow_send", False)),
    }
    upsert_contact(
        contact_id=_contact_id_from_target(target_ref),
        display_name=display_name,
        aliases=[alias],
        whitelisted=bool(item.get("allow_send", False)),
        policy_group=item.get("policy_group"),
        metadata=metadata,
    )
    return True


def _sync_group_item(item: dict[str, Any]) -> bool:
    alias = str(item.get("alias") or item.get("name") or "").strip()
    target_ref = str(item.get("target_ref") or "").strip()
    kind = str(item.get("kind") or "group").strip().lower()
    if kind not in {"group", "list"}:
        return False
    if not alias or not target_ref:
        raise ValueError("allowlist_entry_invalid")
    now = utc_now()
    list_id = "list_" + hash_text(target_ref)[:16]
    metadata = {
        "source": "env",
        "target_ref_hash": hash_text(target_ref),
        "allow_receive": bool(item.get("allow_receive", True)),
        "allow_send": bool(item.get("allow_send", False)),
    }
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO lists (id, name, name_norm, allowed, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name_norm) DO UPDATE SET
                name=excluded.name,
                allowed=excluded.allowed,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                list_id,
                alias,
                _normalize(alias),
                1 if bool(item.get("allow_send", False)) else 0,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
    return True


def sync_allowlist_from_env() -> dict[str, Any]:
    """Sync Infisical-rendered allowlist env into the local runtime cache.

    Infisical/env is the source of truth for raw WhatsApp refs. SQLite keeps a
    derived, sanitized operational cache: target refs are converted to stable
    hashes/ids and never returned by this function.
    """
    init_db()
    try:
        contacts = _load_allowlist_json("CONTACTS_JSON", [], "WHATSAPP_OPS_ALLOWLIST_CONTACTS_JSON")
        groups = _load_allowlist_json("GROUPS_JSON", [], "WHATSAPP_OPS_ALLOWLIST_GROUPS_JSON")
        alias_map = _load_allowlist_json("ALIAS_MAP_JSON", {}, "WHATSAPP_OPS_ALIAS_MAP_JSON")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if not isinstance(contacts, list) or not isinstance(groups, list):
        return {"ok": False, "error": "allowlist_json_invalid"}

    contact_items = [*contacts, *_iter_alias_map_entries(alias_map)]
    contacts_synced = 0
    groups_synced = 0
    try:
        for item in contact_items:
            if not isinstance(item, dict):
                raise ValueError("allowlist_entry_invalid")
            if _sync_contact_item(item):
                contacts_synced += 1
        for item in groups:
            if not isinstance(item, dict):
                raise ValueError("allowlist_entry_invalid")
            if _sync_group_item(item):
                groups_synced += 1
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "source": "env",
        "contacts_synced": contacts_synced,
        "groups_synced": groups_synced,
    }


# ---------------------------------------------------------------------------
# Drafts & approvals
# ---------------------------------------------------------------------------


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
    approval_id = "approval_" + uuid.uuid4().hex[:12]
    token_hash = hash_text(secrets.token_urlsafe(32))
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
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
                token_hash,
                None,
                draft["message_hash"],
                "pending",
                expires_at,
                now,
                None,
            ),
        )
        conn.execute(
            "UPDATE drafts SET status=?, updated_at=? WHERE id=?",
            ("pending_approval", now, draft_id),
        )
    return {"approval_id": approval_id, "status": "pending", "expires_at": expires_at}


def get_valid_approval(draft_id: str, approval_token: str | None = None) -> dict[str, Any] | None:
    """Return the latest approved approval for a draft.

    ``approval_token`` is retained for backward-compatible direct calls. The
    public tool no longer exposes or requires it; production approval is state-
    based after a trusted human resolve action.
    """
    init_db()
    params: tuple[Any, ...]
    token_clause = ""
    if approval_token:
        token_clause = " AND approval_token_hash=?"
        params = (draft_id, hash_text(approval_token))
    else:
        params = (draft_id,)
    with _connect() as conn:
        return _row_to_dict(
            conn.execute(
                f"""
                SELECT * FROM approvals
                WHERE draft_id=? AND status='approved'{token_clause}
                ORDER BY resolved_at DESC, created_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
        )


def get_latest_approval(draft_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        return _row_to_dict(
            conn.execute(
                """
                SELECT * FROM approvals
                WHERE draft_id=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (draft_id,),
            ).fetchone()
        )


def resolve_approval(approval_id: str, decision: str, approver_ref: str | None = None) -> dict[str, Any]:
    init_db()
    decision_norm = str(decision or "").strip().lower()
    if decision_norm not in {"approved", "denied"}:
        return {"ok": False, "approval_id": approval_id, "error": "decision_invalid"}
    now = utc_now()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            return {"ok": False, "approval_id": approval_id, "error": "approval_not_found"}
        approval = dict(row)
        if approval.get("status") != "pending":
            return {"ok": False, "approval_id": approval_id, "error": "approval_not_pending", "status": approval.get("status")}
        try:
            expires = datetime.fromisoformat(str(approval.get("expires_at", "")).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except ValueError:
            expires = datetime.now(timezone.utc) - timedelta(seconds=1)
        if expires <= datetime.now(timezone.utc):
            conn.execute(
                "UPDATE approvals SET status=?, resolved_at=? WHERE id=?",
                ("expired", now, approval_id),
            )
            return {"ok": False, "approval_id": approval_id, "error": "approval_expired", "status": "expired"}
        approver_hash = hash_text(str(approver_ref)) if approver_ref else None
        conn.execute(
            "UPDATE approvals SET status=?, approver_ref_hash=?, resolved_at=? WHERE id=?",
            (decision_norm, approver_hash, now, approval_id),
        )
        conn.execute(
            "UPDATE drafts SET status=?, updated_at=? WHERE id=?",
            (decision_norm, now, approval["draft_id"]),
        )
    return {"ok": True, "approval_id": approval_id, "draft_id": approval["draft_id"], "status": decision_norm}


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


def mark_outbox_result(draft_id: str, idempotency_key: str, status: str, last_error: str | None = None) -> None:
    init_db()
    now = utc_now()
    sent_at = now if status == "sent" else None
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO outbox (
                id, draft_id, idempotency_key, status, attempt_count,
                last_error, scheduled_for, sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO UPDATE SET
                status=excluded.status,
                attempt_count=outbox.attempt_count + 1,
                last_error=excluded.last_error,
                sent_at=excluded.sent_at,
                updated_at=excluded.updated_at
            """,
            (
                "outbox_" + uuid.uuid4().hex[:12],
                draft_id,
                idempotency_key,
                status,
                1,
                (last_error or "")[:500] if last_error else None,
                None,
                sent_at,
                now,
                now,
            ),
        )


def reserve_outbox_send(draft_id: str, idempotency_key: str) -> bool:
    init_db()
    now = utc_now()
    with _connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO outbox (
                    id, draft_id, idempotency_key, status, attempt_count,
                    last_error, scheduled_for, sent_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "outbox_" + uuid.uuid4().hex[:12],
                    draft_id,
                    idempotency_key,
                    "sending",
                    0,
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def _sanitize_payload(value: Any) -> Any:
    secret_keys = {"token", "api_key", "apikey", "authorization", "password", "secret", "key"}
    pii_keys = {"id", "wid", "lid", "phone", "phone_e164", "number", "contact", "contact_ref", "chatid", "chat_id", "thread_ref"}
    url_keys = {"url", "mediaurl", "media_url", "fileurl", "file_url", "downloadurl", "download_url", "thumbnailurl", "thumbnail_url"}
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            key_norm = key_str.strip().lower()
            if key_norm in secret_keys or any(part in key_norm for part in ("token", "secret", "password", "api_key")):
                safe[key_str] = "<redacted>"
            elif key_norm in pii_keys:
                safe[key_str] = "<redacted>"
            elif key_norm in url_keys or key_norm.endswith("url") or key_norm.endswith("uri"):
                safe[key_str] = "<redacted-url>"
            else:
                safe[key_str] = _sanitize_payload(item)
        return safe
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value[:50]]
    if isinstance(value, str):
        cleaned = value[:500]
        if re.match(r"(?i)^https?://", cleaned):
            return "<redacted-url>"
        cleaned = re.sub(r"(?i)\b[\w.-]+@(?:lid|g\.us|s\.whatsapp\.net)\b", "<redacted-wa-ref>", cleaned)
        cleaned = re.sub(r"\+?\d[\d\s().-]{6,}\d", "<redacted-phone>", cleaned)
        return cleaned
    return value


def record_inbound_event(
    *,
    source_event_id: str,
    contact_ref: str = "",
    thread_ref: str = "",
    payload: dict[str, Any] | None = None,
    status: str = "received",
) -> dict[str, Any]:
    init_db()
    if not source_event_id:
        return {"ok": False, "error": "source_event_id_required"}
    event_id = "inbound_" + uuid.uuid4().hex[:12]
    now = utc_now()
    safe_payload = _sanitize_payload(payload or {})
    with _connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO inbound_events (
                    id, source_event_id_hash, contact_ref_hash, thread_ref_hash,
                    payload_redacted_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    hash_text(source_event_id),
                    hash_text(contact_ref) if contact_ref else None,
                    hash_text(thread_ref) if thread_ref else None,
                    json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
                    str(status or "received")[:40],
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM inbound_events WHERE source_event_id_hash=?",
                (hash_text(source_event_id),),
            ).fetchone()
            return {"ok": True, "event_id": row["id"] if row else "", "deduped": True}
    return {"ok": True, "event_id": event_id, "deduped": False, "status": str(status or "received")[:40]}


def lookup_inbound_events(thread: str = "", contact: str = "", limit: int = 20) -> list[dict[str, Any]]:
    init_db()
    limit = max(1, min(int(limit or 20), 100))
    clauses: list[str] = []
    params: list[Any] = []
    if thread:
        clauses.append("thread_ref_hash=?")
        params.append(hash_text(thread))
    if contact:
        clauses.append("contact_ref_hash=?")
        params.append(hash_text(contact))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, payload_redacted_json, status, created_at
            FROM inbound_events
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_redacted_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        events.append(
            {
                "event_id": row["id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "payload": payload,
            }
        )
    return events


_COUNT_QUERIES = {
    "contacts": "SELECT count(*) FROM contacts",
    "lists": "SELECT count(*) FROM lists",
    "inbound_events": "SELECT count(*) FROM inbound_events",
}

_STATUS_COUNT_QUERIES = {
    "drafts": "SELECT status, count(*) AS count FROM drafts GROUP BY status ORDER BY status",
    "approvals": "SELECT status, count(*) AS count FROM approvals GROUP BY status ORDER BY status",
    "outbox": "SELECT status, count(*) AS count FROM outbox GROUP BY status ORDER BY status",
}


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    query = _COUNT_QUERIES[table]
    return int(conn.execute(query).fetchone()[0])


def _count_by_status(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    query = _STATUS_COUNT_QUERIES[table]
    rows = conn.execute(query).fetchall()
    return {str(row["status"] or "unknown"): int(row["count"]) for row in rows}


def get_cockpit_overview(limit: int = 10) -> dict[str, Any]:
    """Return a sanitized admin cockpit snapshot.

    This is read-only and intentionally contains no raw WhatsApp refs, phone
    numbers, message IDs, approval tokens, endpoint URLs, or API keys.
    """
    init_db()
    limit = max(1, min(int(limit or 10), 50))
    with _connect() as conn:
        counts = {
            "contacts": _count_rows(conn, "contacts"),
            "lists": _count_rows(conn, "lists"),
            "inbound_events": _count_rows(conn, "inbound_events"),
            "drafts_by_status": _count_by_status(conn, "drafts"),
            "approvals_by_status": _count_by_status(conn, "approvals"),
            "outbox_by_status": _count_by_status(conn, "outbox"),
        }
        approval_rows = conn.execute(
            """
            SELECT id, draft_id, status, expires_at, created_at, resolved_at
            FROM approvals
            WHERE status='pending'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        draft_rows = conn.execute(
            """
            SELECT id, status, message_hash, send_at, created_at, updated_at
            FROM drafts
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    pending_approvals = [
        {
            "approval_id": row["id"],
            "draft_id": row["draft_id"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
        }
        for row in approval_rows
    ]
    recent_drafts = [
        {
            "draft_id": row["id"],
            "status": row["status"],
            "message_hash": row["message_hash"],
            "send_at": row["send_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in draft_rows
    ]
    return {
        "ok": True,
        "counts": counts,
        "pending_approvals": pending_approvals,
        "recent_drafts": recent_drafts,
        "recent_inbound": lookup_inbound_events(limit=limit),
        "operator_actions": [
            "wpp_sync_allowlist",
            "wpp_inbound_lookup",
            "wpp_resolve_contact",
            "wpp_list_contacts",
            "wpp_request_approval",
            "wpp_status",
            "wpp_cancel",
            "wpp_register_alias",
        ],
    }


def get_send_allowlist_ids() -> dict[str, list[str]]:
    """Return sanitized IDs currently allowed for send from derived runtime cache."""
    init_db()
    with _connect() as conn:
        contact_rows = conn.execute(
            "SELECT id FROM contacts WHERE whitelisted=1 ORDER BY id"
        ).fetchall()
        group_rows = conn.execute(
            "SELECT id FROM lists WHERE allowed=1 ORDER BY id"
        ).fetchall()
    return {
        "contacts": [_safe_contact_id(str(row["id"])) for row in contact_rows],
        "groups": [str(row["id"]) for row in group_rows],
    }


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
