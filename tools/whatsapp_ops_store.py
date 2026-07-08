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


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_recent_iso(value: Any, *, max_age: timedelta) -> bool:
    parsed = _parse_iso_datetime(value)
    if parsed is None:
        return False
    return datetime.now(timezone.utc) - parsed <= max_age


def _truthy_provider_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _payload_from_self(payload: dict[str, Any] | Any) -> bool:
    """Return True when a provider payload is an outbound echo from our own number.

    QuePasa/Baileys-style webhooks can echo messages sent by the connected
    account with fields such as ``fromme`` or ``key.fromMe``. Those are useful
    as conversation context, but they must not become actionable contact
    registration items: the participant is the operator's own connected number.
    """
    if not isinstance(payload, dict):
        return False
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else payload.get("data")
    if not isinstance(data, dict):
        data = {}
    key = data.get("key") if isinstance(data.get("key"), dict) else {}
    candidates = (
        payload.get("fromme"),
        payload.get("fromMe"),
        payload.get("from_me"),
        payload.get("isFromMe"),
        data.get("fromme"),
        data.get("fromMe"),
        data.get("from_me"),
        key.get("fromme"),
        key.get("fromMe"),
        key.get("from_me"),
    )
    return any(_truthy_provider_bool(value) for value in candidates)


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
                target_ref_enc TEXT,
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
                media_json TEXT,
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
            CREATE TABLE IF NOT EXISTS media_transcriptions (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE REFERENCES inbound_events(id),
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                mode TEXT NOT NULL,
                language TEXT,
                provider TEXT NOT NULL,
                media_type TEXT NOT NULL,
                media_metadata_json TEXT,
                safety_flags_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS registration_ignored (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                ref_hash TEXT NOT NULL,
                safe_hint_json TEXT,
                created_at TEXT NOT NULL,
                source_staging_id TEXT,
                UNIQUE(kind, ref_hash)
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
        _ensure_draft_columns(conn)
        _ensure_list_columns(conn)
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


def _ensure_draft_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(drafts)")}
    if "media_json" not in columns:
        conn.execute("ALTER TABLE drafts ADD COLUMN media_json TEXT")


def _ensure_list_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(lists)")}
    if "target_ref_enc" not in columns:
        conn.execute("ALTER TABLE lists ADD COLUMN target_ref_enc TEXT")



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


_PHONE_REF_DOMAINS = ("@s.whatsapp.net", "@c.us", "@whatsapp.net")
_NON_PHONE_REF_DOMAINS = ("@lid", "@g.us")


def _trusted_phone_digits(value: Any) -> str:
    """Return digits only when *value* is a real phone-bearing value.

    WhatsApp provider identifiers such as ``@lid`` and group JIDs contain long
    digit strings that are not phone numbers. They must never become
    ``phone_masked``/``last4`` hints in the operator cockpit.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if any(domain in lowered for domain in _NON_PHONE_REF_DOMAINS):
        return ""
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
        return ""
    digits = _phone_digits(text)
    if not (8 <= len(digits) <= 15):
        return ""
    if any(domain in lowered for domain in _PHONE_REF_DOMAINS):
        return digits
    if "@" in lowered:
        return ""
    return digits


def _mask_trusted_phone(value: Any) -> str:
    digits = _trusted_phone_digits(value)
    return mask_phone(digits) if digits else ""


def _safe_existing_phone_mask(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if any(domain in lowered for domain in _NON_PHONE_REF_DOMAINS):
        return ""
    if "*" in text and re.fullmatch(r"\+?\d{1,3}\*+\d{2,4}", text):
        return text
    return _mask_trusted_phone(text)


def _safe_person_name(value: Any, limit: int = 60) -> str:
    return _clean_registration_text(value, limit=limit)


def _mask_phone_fragment(value: str) -> str:
    text = str(value or "")
    # Do not treat ISO/date fragments as phones (e.g. 2026-06-06 timestamps).
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
        return text
    return mask_phone(text) or "<redacted-phone>"


def _replace_phone_mentions_with_names(text: str) -> str:
    """Prefer a provider/contact name over an opaque phone placeholder.

    Examples:
    - @5511999987655 (Maria) -> @Maria
    - @<redacted-phone> (Maria) -> @Maria
    If there is no adjacent name, the caller still masks the phone digits.
    """
    def repl(match: re.Match[str]) -> str:
        name = _safe_person_name(match.group(1), limit=50)
        if not name:
            return match.group(0)
        return f"@{name}"

    text = re.sub(r"@?\+?\d[\d\s().-]{6,}\d\s*\(([^)]+)\)", repl, text)
    text = re.sub(r"@?<redacted-phone>\s*\(([^)]+)\)", repl, text, flags=re.IGNORECASE)
    return text


def _safe_contact_id(contact_id: str) -> str:
    raw = str(contact_id or "")
    if raw.startswith(("contact_", "synthetic_")):
        return raw
    if "@" in raw or _phone_digits(raw):
        return "contact_" + hash_text(raw)[:16]
    return raw


def _safe_contact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact_id": _safe_contact_id(row["id"]),
        "display_name": row["display_name"],
        "phone_masked": _safe_existing_phone_mask(row.get("phone_e164_enc")),
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

    explicit_phone = ""
    for key in (
        "phone",
        "phone_e164",
        "phone_number",
        "phoneNumber",
        "senderPhone",
        "participantPhone",
    ):
        explicit_phone = _safe_existing_phone_mask(value.get(key))
        if explicit_phone:
            hint["phone_masked"] = explicit_phone
            digits = _trusted_phone_digits(value.get(key))
            if digits:
                hint["last4"] = digits[-4:]
            hint["phone_source"] = "explicit"
            break
    if not explicit_phone and str(value.get("phone_source") or "").strip() in {"explicit", "raw_ref", "resolved"}:
        existing_mask = _safe_existing_phone_mask(value.get("phone_masked"))
        if existing_mask:
            hint["phone_masked"] = existing_mask
            last4 = re.sub(r"\D+", "", str(value.get("last4") or ""))[-4:]
            if last4:
                hint["last4"] = last4
            hint["phone_source"] = str(value.get("phone_source"))
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
    digits = _trusted_phone_digits(raw_ref)
    if kind == "contact" and digits:
        masked = mask_phone(raw_ref)
        if masked:
            hint["phone_masked"] = masked
            hint["phone_source"] = "raw_ref"
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
    if "phone_masked" not in new:
        merged.pop("phone_masked", None)
        merged.pop("last4", None)
        merged.pop("phone_source", None)
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


def _registration_ref_is_ignored(conn: sqlite3.Connection, kind: str, ref_hash: str) -> bool:
    if not ref_hash:
        return False
    row = conn.execute(
        "SELECT 1 FROM registration_ignored WHERE kind=? AND ref_hash=? LIMIT 1",
        (kind, ref_hash),
    ).fetchone()
    return row is not None


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
        if _registration_ref_is_ignored(conn, kind_norm, ref_hash):
            return {
                "ok": True,
                "ignored": True,
                "kind": kind_norm,
                "safe_id": hint.get("safe_id", ""),
            }
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



def _recent_messages_for_staging_row(row: dict[str, Any], *, limit: int = 5, max_text_chars: int = 140) -> list[dict[str, Any]]:
    """Return bounded safe local context for a staging row.

    This is intentionally local-store only. It can include self-echo messages as
    context ("Eu"), but it never uses those messages as contact-registration
    targets and it never fetches provider history.
    """
    thread_ref = str(row.get("thread_ref_raw") or "").strip()
    contact_ref = str(row.get("contact_ref_raw") or "").strip()
    lookup_ref = thread_ref or contact_ref
    if not lookup_ref:
        return []
    events = lookup_inbound_events(thread=lookup_ref, limit=max(limit * 4, limit))
    recent: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if _message_type_from_payload(payload) == "system":
            continue
        summary = _summary_event(event, max_text_chars)
        from_self = _payload_from_self(payload)
        summary["from_self"] = from_self
        summary["direction"] = "eu" if from_self else "contato"
        if from_self:
            summary["sender_label"] = "Eu"
            summary.pop("sender_display_name", None)
            summary.pop("sender_phone_masked", None)
        else:
            summary["sender_label"] = str(summary.get("sender_label") or "Contato")
        recent.append(summary)
        if len(recent) >= max(1, min(limit, 5)):
            break
    return list(reversed(recent))


def _registration_context_summary(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    parts: list[str] = []
    for msg in messages[-3:]:
        who = str(msg.get("sender_label") or ("Eu" if msg.get("from_self") else "Contato"))
        preview = str(msg.get("text_preview") or "").strip()
        if not preview:
            preview = "mídia" if msg.get("has_media") else str(msg.get("message_type") or "mensagem")
        parts.append(f"{who}: {preview}")
    return _truncate_text(" | ".join(parts), 260)


def _recent_contact_phone_mask(messages: list[dict[str, Any]], display_name: str) -> str:
    """Return a sanitized sender phone mask from local context for this contact.

    This is a display-only fallback for pre-patch staging rows. It only accepts
    already-masked sender phones from sanitized local inbound payloads and only
    when the sender display name matches the staging contact name.
    """
    display_norm = _normalize(display_name)
    if not display_norm:
        return ""
    for msg in reversed(messages):
        if msg.get("from_self"):
            continue
        sender_name = _normalize(str(msg.get("sender_display_name") or ""))
        if sender_name != display_norm:
            continue
        masked = _safe_existing_phone_mask(msg.get("sender_phone_masked"))
        if masked:
            return masked
    return ""


def _is_system_staging_row(row: dict[str, Any]) -> bool:
    hint = _load_staging_hint(row.get("safe_hint_json"))
    return str(hint.get("last_message_type") or "").strip().lower() == "system"


def _metadata_target_ref_hash(value: Any) -> str:
    try:
        data = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("target_ref_hash") or "").strip()


def _staging_ref_is_registered(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    """Return True when a staging row already has a local contact/group record.

    Registration staging is a short-lived operator queue. If the same raw-ref
    hash is already present in the local allowlist cache, showing it again as
    "Cadastrar" is redundant and can make the operator think the register step
    failed. Name-only collisions are intentionally not enough: same display
    name can point to a different provider ref.
    """
    kind = str(row.get("kind") or "contact").strip().lower()
    ref_hash = str(row.get("ref_hash") or "").strip()
    if not ref_hash:
        return False
    if kind == "group":
        expected_id = "list_" + ref_hash[:16]
        direct = conn.execute("SELECT 1 FROM lists WHERE id=? LIMIT 1", (expected_id,)).fetchone()
        if direct is not None:
            return True
        rows = conn.execute("SELECT metadata_json FROM lists WHERE metadata_json LIKE ?", (f"%{ref_hash[:16]}%",)).fetchall()
    else:
        expected_id = "contact_" + ref_hash[:16]
        direct = conn.execute("SELECT 1 FROM contacts WHERE id=? LIMIT 1", (expected_id,)).fetchone()
        if direct is not None:
            return True
        rows = conn.execute("SELECT metadata_json FROM contacts WHERE metadata_json LIKE ?", (f"%{ref_hash[:16]}%",)).fetchall()
    return any(_metadata_target_ref_hash(row_meta["metadata_json"]) == ref_hash for row_meta in rows)


def _payload_message_type(payload: dict[str, Any]) -> str:
    return str(_message_type_from_payload(payload) or "").strip().lower()


def _public_staging_item(row: dict[str, Any]) -> dict[str, Any]:
    kind = str(row.get("kind") or "contact")
    hint = _load_staging_hint(row.get("safe_hint_json"))
    raw_ref = _raw_ref_for_kind(kind, str(row.get("contact_ref_raw") or ""), str(row.get("thread_ref_raw") or ""))
    if raw_ref and not hint.get("safe_id"):
        hint["safe_id"] = _safe_registration_id(kind, raw_ref)
    if kind == "contact" and hint.get("phone_masked") and not hint.get("phone_source") and not _trusted_phone_digits(raw_ref):
        hint.pop("phone_masked", None)
        hint.pop("last4", None)
    display_name = row.get("display_name") or hint.get("display_name") or ""
    if not display_name:
        display_name = hint.get("group_name") or hint.get("participant_name") or ""
    recent_messages = _recent_messages_for_staging_row(row, limit=5, max_text_chars=140)
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
    if recent_messages:
        item["recent_messages"] = recent_messages
        item["context_summary"] = _registration_context_summary(recent_messages)
        latest_text = next((str(msg.get("text_preview") or "").strip() for msg in reversed(recent_messages) if msg.get("text_preview")), "")
        if latest_text:
            item["last_text_preview"] = latest_text
        if kind == "contact" and not hint.get("phone_masked"):
            context_mask = _recent_contact_phone_mask(recent_messages, str(display_name or ""))
            if context_mask:
                hint["phone_masked"] = context_mask
                digits = _phone_digits(context_mask)
                if digits:
                    hint["last4"] = digits[-4:]
                hint["phone_source"] = "local_context"
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
    if kind == "contact" and not item.get("phone_masked"):
        item["phone_status"] = "unresolved"
        item["identity_note"] = "número não resolvido"
    return item



def peek_staging() -> list[dict[str, Any]]:
    """Return sanitized info about what's staged for registration (no raw refs)."""
    init_db()
    _staging_cleanup()
    with _connect() as conn:
        rows = []
        for row in conn.execute(
            """
            SELECT *
            FROM registration_staging
            WHERE expires_at > ?
            ORDER BY COALESCE(last_seen_at, created_at) DESC, created_at DESC
            """,
            (utc_now(),),
        ).fetchall():
            row_dict = dict(row)
            if _is_system_staging_row(row_dict):
                continue
            if _staging_ref_is_registered(conn, row_dict):
                continue
            rows.append(row_dict)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("kind") or "contact"), str(row.get("ref_hash") or row.get("id")))
        if key not in grouped:
            grouped[key] = dict(row)
            grouped[key]["message_count"] = int(row.get("message_count") or 1)
            continue
        grouped[key]["message_count"] = int(grouped[key].get("message_count") or 1) + int(row.get("message_count") or 1)
    return [_public_staging_item(row) for row in grouped.values()]


def ignore_staging_item(staging_id: str = "", item_index: Any = 0) -> dict[str, Any]:
    """Ignore a current registration-staging target without exposing raw refs.

    The ignore is keyed by kind + raw-ref hash, so the same group/contact will
    not be re-staged by the next provider echo. This is still a local store
    decision; no WhatsApp send, provider call, or secret-manager write occurs.
    """
    init_db()
    _staging_cleanup()
    selected_id = str(staging_id or "").strip()
    if not selected_id and item_index not in (None, "", 0, "0"):
        try:
            idx = int(str(item_index).strip())
        except Exception:
            return {"ok": False, "error": "item_invalid", "hint": "Use /ignorar N com o número mostrado em /fila."}
        staged = peek_staging()
        if idx < 1 or idx > len(staged):
            return {"ok": False, "error": "item_not_found", "hint": "Use /fila para ver os itens atuais."}
        selected_id = str(staged[idx - 1].get("staging_id") or "")
    if not selected_id:
        return {"ok": False, "error": "staging_id_required", "hint": "Use /ignorar N com o número mostrado em /fila."}

    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM registration_staging WHERE id=? AND expires_at > ? LIMIT 1",
            (selected_id, utc_now()),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "item_not_found_or_expired", "hint": "Use /fila para ver os itens atuais."}
        row_dict = dict(row)
        kind = str(row_dict.get("kind") or "contact").strip().lower()
        raw_ref = _raw_ref_for_kind(kind, str(row_dict.get("contact_ref_raw") or ""), str(row_dict.get("thread_ref_raw") or ""))
        ref_hash = str(row_dict.get("ref_hash") or (hash_text(raw_ref) if raw_ref else ""))
        public_item = _public_staging_item(row_dict)
        if not ref_hash:
            return {"ok": False, "error": "ref_hash_missing"}
        ignored_id = "ignored_" + uuid.uuid4().hex[:12]
        now = utc_now()
        conn.execute(
            """
            INSERT INTO registration_ignored (id, kind, ref_hash, safe_hint_json, created_at, source_staging_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, ref_hash) DO UPDATE SET
                safe_hint_json=excluded.safe_hint_json,
                created_at=excluded.created_at,
                source_staging_id=excluded.source_staging_id
            """,
            (
                ignored_id,
                kind,
                ref_hash,
                json.dumps({
                    "safe_id": public_item.get("safe_id", ""),
                    "display_name": public_item.get("display_name", ""),
                    "kind": kind,
                    "source_group_name": public_item.get("source_group_name", ""),
                }, ensure_ascii=False, sort_keys=True),
                now,
                selected_id,
            ),
        )
        _delete_staging_row(conn, row_dict)
    return {
        "ok": True,
        "ignored": True,
        "kind": kind,
        "staging_id": selected_id,
        "safe_id": public_item.get("safe_id", ""),
        "display_name": public_item.get("display_name", ""),
        "message": "Item removido da fila e marcado para não reaparecer automaticamente.",
        "send_performed": False,
        "provider_history_used": False,
    }



def registration_staging_diagnostics() -> dict[str, Any]:
    """Return sanitized registration-staging diagnostics for operator UX."""
    init_db()
    _staging_cleanup()
    staging_count = len(peek_staging())
    latest_created_at = None
    with _connect() as conn:
        inbound_count = conn.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0]
        latest_rows = conn.execute(
            "SELECT created_at, payload_redacted_json FROM inbound_events ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    for row in latest_rows:
        try:
            payload = json.loads(row["payload_redacted_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict) or _payload_message_type(payload) == "system":
            continue
        latest_created_at = row["created_at"]
        break
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
    phone_digits = _trusted_phone_digits(phone_e164)
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
        "phone_masked": _safe_existing_phone_mask(phone_e164),
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


def get_transport_contact_ref(query: str) -> str:
    """Return raw provider ref for an allowlisted contact, for transport only."""
    init_db()
    needle = str(query or "").strip()
    if not needle:
        return ""
    needle_norm = _normalize(needle)
    needle_digits = _phone_digits(needle)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT c.*
            FROM contacts c
            LEFT JOIN contact_aliases a ON a.contact_id = c.id
            WHERE c.whitelisted = 1
              AND (
                c.id = ?
                OR a.alias_norm = ?
                OR lower(c.display_name) = ?
                OR (? != '' AND c.phone_e164_hash = ?)
              )
            LIMIT 2
            """,
            (needle, needle_norm, needle_norm, needle_digits, hash_text(needle_digits) if needle_digits else ""),
        ).fetchall()
    if len(rows) != 1:
        return ""
    return str(dict(rows[0]).get("phone_e164_enc") or "").strip()


def get_transport_group_ref(query: str) -> str:
    """Return raw provider ref for an allowlisted group/list, for transport only."""
    init_db()
    needle = str(query or "").strip()
    if not needle:
        return ""
    needle_norm = _normalize(needle)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM lists
            WHERE allowed = 1
              AND (id = ? OR name_norm = ? OR lower(name) = ?)
            LIMIT 2
            """,
            (needle, needle_norm, needle_norm),
        ).fetchall()
    if len(rows) != 1:
        return ""
    return str(dict(rows[0]).get("target_ref_enc") or "").strip()


def _segment_id_from_name(name: str) -> str:
    return "segment_" + hash_text(_normalize(name))[:16]


def _normalize_contact_ref_for_import(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw
    digits = _phone_digits(raw)
    if len(digits) >= 8:
        return f"{digits}@s.whatsapp.net"
    return raw


def import_contact_list_local(
    *,
    list_name: str,
    contacts: list[dict[str, Any]],
    allow_send: bool = False,
    policy_group: str = "lead",
    source: str = "manual_import",
) -> dict[str, Any]:
    """Import a commercial contact segment into local SQLite.

    Raw refs are retained only for operational transport; the returned summary is
    sanitized. ``allow_send`` defaults false, so imported prospect lists cannot
    be sent to until the operator explicitly opts into send allowlisting.
    """
    init_db()
    name = str(list_name or "").strip()
    if not name:
        return {"ok": False, "error": "list_name_required"}
    if not isinstance(contacts, list) or not contacts:
        return {"ok": False, "error": "contacts_required"}
    now = utc_now()
    list_id = _segment_id_from_name(name)
    imported: list[dict[str, Any]] = []
    skipped = 0
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO lists (id, name, name_norm, allowed, target_ref_enc, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name_norm) DO UPDATE SET
                name=excluded.name,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                list_id,
                name,
                _normalize(name),
                0,
                None,
                json.dumps({"source": source, "kind": "contact_segment"}, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
    for item in contacts[:500]:
        if not isinstance(item, dict):
            skipped += 1
            continue
        alias = str(item.get("alias") or item.get("name") or item.get("display_name") or "").strip()
        raw_ref = _normalize_contact_ref_for_import(str(item.get("target_ref") or item.get("phone") or item.get("phone_e164") or ""))
        if not alias or not raw_ref:
            skipped += 1
            continue
        contact_id = _contact_id_from_target(raw_ref)
        display = str(item.get("display_name") or item.get("name") or alias).strip() or alias
        metadata = {
            "source": source,
            "target_ref_hash": hash_text(raw_ref),
            "segment_id": list_id,
        }
        upsert_contact(
            contact_id=contact_id,
            display_name=display,
            phone_e164=raw_ref,
            aliases=[alias],
            whitelisted=bool(allow_send),
            policy_group=policy_group,
            metadata=metadata,
        )
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO list_members (list_id, contact_id, created_at) VALUES (?, ?, ?)",
                (list_id, contact_id, now),
            )
        imported.append({"contact_id": contact_id, "display_name": display, "whitelisted": bool(allow_send)})
    return {
        "ok": True,
        "list_id": list_id,
        "name": name,
        "imported_count": len(imported),
        "skipped_count": skipped,
        "allow_send": bool(allow_send),
        "contacts": [_safe_contact({"id": c["contact_id"], "display_name": c["display_name"], "phone_e164_enc": "", "whitelisted": c["whitelisted"], "policy_group": policy_group}) for c in imported[:50]],
    }


def list_contact_segments(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    limit = max(1, min(int(limit or 50), 100))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.name, l.allowed, l.metadata_json,
                   count(m.contact_id) AS member_count,
                   sum(CASE WHEN c.whitelisted = 1 THEN 1 ELSE 0 END) AS whitelisted_count
            FROM lists l
            LEFT JOIN list_members m ON m.list_id = l.id
            LEFT JOIN contacts c ON c.id = m.contact_id
            WHERE l.id LIKE 'segment_%' OR json_extract(COALESCE(l.metadata_json, '{}'), '$.kind') = 'contact_segment'
            GROUP BY l.id
            ORDER BY l.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "list_id": row["id"],
            "name": row["name"],
            "member_count": int(row["member_count"] or 0),
            "whitelisted_count": int(row["whitelisted_count"] or 0),
            "bulk_send_allowed": bool(row["allowed"]),
        }
        for row in rows
    ]


def list_contact_segment_members(list_ref: str, limit: int = 50) -> dict[str, Any]:
    init_db()
    ref = str(list_ref or "").strip()
    if not ref:
        return {"ok": False, "error": "list_ref_required"}
    limit = max(1, min(int(limit or 50), 100))
    ref_norm = _normalize(ref)
    with _connect() as conn:
        segment = conn.execute(
            "SELECT * FROM lists WHERE id=? OR name_norm=? LIMIT 1",
            (ref, ref_norm),
        ).fetchone()
        if segment is None:
            return {"ok": False, "error": "list_not_found"}
        rows = conn.execute(
            """
            SELECT c.*
            FROM list_members m
            JOIN contacts c ON c.id = m.contact_id
            WHERE m.list_id=?
            ORDER BY c.display_name ASC
            LIMIT ?
            """,
            (segment["id"], limit),
        ).fetchall()
    return {"ok": True, "list_id": segment["id"], "name": segment["name"], "contacts": [_safe_contact(dict(row)) for row in rows]}


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
            phone_e164=raw_ref,
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
            INSERT INTO lists (id, name, name_norm, allowed, target_ref_enc, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name_norm) DO UPDATE SET
                name=excluded.name,
                allowed=excluded.allowed,
                target_ref_enc=excluded.target_ref_enc,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                list_id,
                name,
                _normalize(name),
                1 if allow_send else 0,
                raw_ref,
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
# Conversation target resolution (read-only, no raw refs in public output)
# ---------------------------------------------------------------------------


def _public_conversation_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    result = {
        "target_kind": str(candidate.get("target_kind") or ""),
        "target_label": _clean_registration_text(candidate.get("target_label") or candidate.get("target_safe_id") or "alvo", limit=80) or "alvo",
        "target_safe_id": str(candidate.get("target_safe_id") or "")[:80],
        "source": str(candidate.get("source") or "local")[:80],
        "thread_filter_set": bool(candidate.get("thread_ref")),
        "contact_filter_set": bool(candidate.get("contact_ref")),
    }
    if candidate.get("staging_id"):
        result["staging_id"] = str(candidate.get("staging_id"))[:80]
    if candidate.get("message_count") not in (None, ""):
        try:
            result["message_count"] = int(candidate.get("message_count") or 0)
        except Exception:
            pass
    if candidate.get("last_seen_at"):
        result["last_seen_at"] = str(candidate.get("last_seen_at"))[:80]
    return result


def _conversation_target_response(candidate: dict[str, Any], *, include_transport: bool = False) -> dict[str, Any]:
    public = _public_conversation_candidate(candidate)
    public.update({"ok": True, "ambiguous": False})
    if include_transport:
        public["_thread_ref"] = str(candidate.get("thread_ref") or "")
        public["_contact_ref"] = str(candidate.get("contact_ref") or "")
    return public


def _candidate_from_staging_row(row: dict[str, Any], *, source: str = "staging") -> dict[str, Any] | None:
    kind = str(row.get("kind") or "contact").strip().lower()
    contact_ref = str(row.get("contact_ref_raw") or "")
    thread_ref = str(row.get("thread_ref_raw") or "")
    raw_ref = _raw_ref_for_kind(kind, contact_ref, thread_ref)
    if not raw_ref:
        return None
    public = _public_staging_item(row)
    label = public.get("display_name") or public.get("phone_masked") or public.get("safe_id") or "alvo"
    return {
        "target_kind": "group" if kind == "group" else "contact",
        "target_label": label,
        "target_safe_id": public.get("safe_id") or _safe_registration_id(kind, raw_ref),
        "source": source,
        "thread_ref": raw_ref if kind == "group" else "",
        "contact_ref": raw_ref if kind != "group" else "",
        "staging_id": public.get("staging_id") or row.get("id") or "",
        "message_count": public.get("message_count"),
        "last_seen_at": public.get("last_seen_at") or public.get("created_at"),
    }


def _candidate_from_registered_group(row: dict[str, Any]) -> dict[str, Any] | None:
    raw_ref = str(row.get("target_ref_enc") or "").strip()
    if not raw_ref:
        return None
    return {
        "target_kind": "group",
        "target_label": row.get("name") or "grupo",
        "target_safe_id": str(row.get("id") or ("list_" + hash_text(raw_ref)[:16])),
        "source": "registered_group",
        "thread_ref": raw_ref,
        "contact_ref": "",
    }


def _candidate_from_registered_contact(row: dict[str, Any]) -> dict[str, Any] | None:
    raw_ref = str(row.get("phone_e164_enc") or "").strip()
    if not raw_ref:
        return None
    return {
        "target_kind": "contact",
        "target_label": row.get("display_name") or "contato",
        "target_safe_id": _safe_contact_id(str(row.get("id") or "")),
        "source": "registered_contact",
        "thread_ref": "",
        "contact_ref": raw_ref,
    }


def _conversation_target_for_queue_item(item_index: int) -> dict[str, Any] | None:
    queue = get_actionable_queue(limit=max(10, min(int(item_index or 0), 50)))
    items = queue.get("items") if isinstance(queue, dict) else []
    if not isinstance(items, list) or item_index < 1 or item_index > len(items):
        return {"ok": False, "error": "item_not_found", "hint": "Use /fila para ver os itens atuais."}
    item = items[item_index - 1]
    if not isinstance(item, dict):
        return {"ok": False, "error": "item_invalid"}
    if item.get("kind") == "context":
        return {
            "target_kind": "context",
            "target_label": item.get("operator_title") or item.get("title") or "Contexto recente",
            "target_safe_id": str(item.get("safe_event_id") or "context_recent")[:80],
            "source": "queue_context",
            "thread_ref": "",
            "contact_ref": "",
            "message_count": 1,
            "last_seen_at": item.get("created_at") or "",
        }
    if item.get("kind") != "registration" or not item.get("staging_id"):
        return {
            "ok": False,
            "error": "item_not_conversation_target",
            "hint": "Use um item de cadastro mostrado em /fila ou informe o nome do grupo/contato.",
        }
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM registration_staging WHERE id=? AND expires_at > ? LIMIT 1",
            (str(item.get("staging_id") or ""), utc_now()),
        ).fetchone()
    if row is None:
        return {"ok": False, "error": "item_not_found_or_expired", "hint": "Use /fila novamente."}
    return _candidate_from_staging_row(dict(row), source="queue")


def resolve_conversation_target(
    query: str = "",
    *,
    item_index: Any = 0,
    prefer: str = "",
    include_transport: bool = False,
) -> dict[str, Any]:
    """Resolve an operator-safe WhatsApp conversation target.

    Public output never includes raw WhatsApp refs, phones, URLs, or payloads.
    ``include_transport=True`` is only for internal callers that need the raw
    local ref to filter already-sanitized ``inbound_events``.
    """
    init_db()
    try:
        idx = int(str(item_index or 0).strip() or "0")
    except Exception:
        return {"ok": False, "error": "item_invalid", "hint": "Use /ctxwpp item N ou /ctxwpp <nome>."}
    if idx > 0:
        candidate = _conversation_target_for_queue_item(idx)
        if not candidate or candidate.get("ok") is False:
            return candidate or {"ok": False, "error": "item_not_found"}
        return _conversation_target_response(candidate, include_transport=include_transport)

    needle = str(query or "").strip()
    if not needle:
        return {"ok": False, "error": "target_required", "hint": "Use /ctxwpp <nome>, /ctxwpp item N ou /ctxwpp ajuda."}
    needle_norm = _normalize(needle)
    needle_digits = _phone_digits(needle)
    prefer_norm = str(prefer or "").strip().lower()
    candidates: list[dict[str, Any]] = []

    with _connect() as conn:
        list_rows = conn.execute(
            """
            SELECT *
            FROM lists
            WHERE id = ? OR name_norm = ? OR lower(name) LIKE ?
            ORDER BY CASE WHEN name_norm = ? THEN 0 ELSE 1 END, name ASC
            LIMIT 8
            """,
            (needle, needle_norm, f"%{needle_norm}%", needle_norm),
        ).fetchall()
        contact_rows = conn.execute(
            """
            SELECT DISTINCT c.*
            FROM contacts c
            LEFT JOIN contact_aliases a ON a.contact_id = c.id
            WHERE c.id = ?
               OR a.alias_norm = ?
               OR (? != '' AND c.phone_e164_hash = ?)
               OR lower(c.display_name) LIKE ?
            ORDER BY CASE WHEN lower(c.display_name) = ? THEN 0 ELSE 1 END, c.display_name ASC
            LIMIT 8
            """,
            (needle, needle_norm, needle_digits, hash_text(needle_digits) if needle_digits else "", f"%{needle_norm}%", needle_norm),
        ).fetchall()
        staging_rows = conn.execute(
            """
            SELECT *
            FROM registration_staging
            WHERE expires_at > ?
            ORDER BY COALESCE(last_seen_at, created_at) DESC, created_at DESC
            LIMIT 50
            """,
            (utc_now(),),
        ).fetchall()

    for row in list_rows:
        candidate = _candidate_from_registered_group(dict(row))
        if candidate:
            candidates.append(candidate)
    for row in contact_rows:
        candidate = _candidate_from_registered_contact(dict(row))
        if candidate:
            candidates.append(candidate)
    for row in staging_rows:
        row_dict = dict(row)
        if _is_system_staging_row(row_dict):
            continue
        public = _public_staging_item(row_dict)
        safe_id = str(public.get("safe_id") or "")
        label = str(public.get("display_name") or public.get("phone_masked") or "")
        if needle in {str(public.get("staging_id") or ""), safe_id} or _normalize(label) == needle_norm or needle_norm in _normalize(label):
            candidate = _candidate_from_staging_row(row_dict, source="staging")
            if candidate:
                candidates.append(candidate)

    if prefer_norm in {"group", "grupo"}:
        candidates = [c for c in candidates if c.get("target_kind") == "group"]
    elif prefer_norm in {"contact", "contato", "dm"}:
        candidates = [c for c in candidates if c.get("target_kind") == "contact"]

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        key = (
            str(candidate.get("target_kind") or ""),
            hash_text(str(candidate.get("thread_ref") or candidate.get("contact_ref") or candidate.get("target_safe_id") or "")),
            str(candidate.get("source") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    candidates = deduped

    if not candidates:
        return {
            "ok": False,
            "error": "target_not_found",
            "query": _clean_registration_text(needle, limit=80),
            "hint": "Use /fila para ver itens recentes ou cadastre o grupo/contato primeiro.",
        }
    if len(candidates) == 1:
        return _conversation_target_response(candidates[0], include_transport=include_transport)
    return {
        "ok": True,
        "ambiguous": True,
        "query": _clean_registration_text(needle, limit=80),
        "matches": [_public_conversation_candidate(candidate) for candidate in candidates[:6]],
        "hint": "Alvo ambíguo. Use /ctxwpp item N pela fila ou refine o nome.",
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
        phone_e164=target_ref,
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
            INSERT INTO lists (id, name, name_norm, allowed, target_ref_enc, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name_norm) DO UPDATE SET
                name=excluded.name,
                allowed=excluded.allowed,
                target_ref_enc=excluded.target_ref_enc,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                list_id,
                alias,
                _normalize(alias),
                1 if bool(item.get("allow_send", False)) else 0,
                target_ref,
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
    media: dict[str, Any] | None = None,
) -> dict[str, str]:
    init_db()
    if not isinstance(targets, list) or not targets:
        raise ValueError("targets must be a non-empty list")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message is required")

    now = utc_now()
    draft_id = "draft_" + uuid.uuid4().hex[:12]
    media_json = json.dumps(media or {}, ensure_ascii=False, sort_keys=True) if media else None
    message_hash = hash_text(message)
    idempotency_key = hash_text(
        json.dumps(targets, sort_keys=True, ensure_ascii=False)
        + "\n"
        + message
        + "\n"
        + str(send_at or "")
        + "\n"
        + str(media_json or "")
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO drafts (
                id, targets_json, message, message_hash, media_json, send_at, status,
                idempotency_key, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id,
                json.dumps(targets, ensure_ascii=False, sort_keys=True),
                message,
                message_hash,
                media_json,
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
    if decision_norm not in {"approved", "denied", "edit_requested"}:
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
        draft_status = "needs_edit" if decision_norm == "edit_requested" else decision_norm
        conn.execute(
            "UPDATE drafts SET status=?, updated_at=? WHERE id=?",
            (draft_status, now, approval["draft_id"]),
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
    media_meta_keys = {"directpath", "filesha256", "fileencsha256", "mediakey", "mediakeytimestamp"}
    thumbnail_keys = {"jpegthumbnail", "thumbnail"}
    binary_keys = {"base64", "blob"}
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            key_norm = key_str.strip().lower()
            if key_norm in secret_keys or any(part in key_norm for part in ("token", "secret", "password", "api_key")):
                safe[key_str] = "<redacted>"
            elif key_norm in {"phone", "phone_e164", "number"}:
                safe[key_str] = _safe_existing_phone_mask(item) or "<redacted>"
            elif key_norm in pii_keys:
                safe[key_str] = "<redacted>"
            elif key_norm in url_keys or key_norm.endswith("url") or key_norm.endswith("uri"):
                safe[key_str] = "<redacted-url>"
            elif key_norm in media_meta_keys:
                safe[key_str] = "<redacted>"
            elif key_norm in thumbnail_keys:
                safe[key_str] = "<redacted>"
            elif key_norm in binary_keys:
                safe[key_str] = "<redacted>"
            else:
                safe[key_str] = _sanitize_payload(item)
        return safe
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value[:50]]
    if isinstance(value, str):
        cleaned = value[:500]
        # data: and blob: URLs (including data: URIs with base64 payloads)
        if re.match(r"(?i)^(?:data|blob):", cleaned):
            return "<redacted-url>"
        if re.match(r"(?i)^https?://", cleaned):
            return "<redacted-url>"
        # Long bare base64 strings (>=32 chars of base64 alphabet with optional padding)
        if re.match(r"^[A-Za-z0-9+/=]{32,}$", cleaned):
            return "<redacted-base64>"
        # Text bodies may contain pasted links/data URIs even when the key is not URL-like.
        # Keep the surrounding text for operator context, but remove fetchable media/link targets.
        cleaned = re.sub(r"(?i)\bhttps?://[^\s<>\]\)\"']+", "<redacted-url>", cleaned)
        cleaned = re.sub(r"(?i)\b(?:data|blob):[^\s<>\]\)\"']+", "<redacted-url>", cleaned)
        cleaned = re.sub(r"(?i)\b[\w.+:-]+@(?:lid|g\.us|s\.whatsapp\.net)\b", "<redacted-wa-ref>", cleaned)
        cleaned = _replace_phone_mentions_with_names(cleaned)
        cleaned = re.sub(r"\+?\d[\d\s().-]{6,}\d", lambda match: _mask_phone_fragment(match.group(0)), cleaned)
        cleaned = re.sub(r"(?i)<redacted-phone>[:\w.+-]*@(?:lid|g\.us|s\.whatsapp\.net)\b", "<redacted-wa-ref>", cleaned)
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


_CONTEXT_MODES = {"summary", "operator", "debug"}
_TEXT_PREVIEW_KEYS = {"text", "body", "conversation", "caption", "content"}
_MEDIA_MESSAGE_KEYS = {
    "imageMessage": "image",
    "audioMessage": "audio",
    "videoMessage": "video",
    "documentMessage": "document",
}
_MEDIA_META_KEYS = {
    "mimetype",
    "mime",
    "filelength",
    "file_length",
    "filesize",
    "fileName",
    "filename",
    "seconds",
    "duration",
    "width",
    "height",
}


def _truncate_text(value: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def _first_text_preview(value: Any, max_chars: int) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            key_norm = str(key or "").strip()
            if key_norm in _TEXT_PREVIEW_KEYS and isinstance(item, str):
                return _truncate_text(item, max_chars)
            nested = _first_text_preview(item, max_chars)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value[:20]:
            nested = _first_text_preview(item, max_chars)
            if nested:
                return nested
    return ""


def _message_type_from_payload(payload: dict[str, Any]) -> str:
    for key in ("messageType", "type"):
        raw = str(payload.get(key) or "").strip().lower()
        if raw:
            return raw[:40]
    stack: list[Any] = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in _MEDIA_MESSAGE_KEYS:
                    return _MEDIA_MESSAGE_KEYS[key]
                if isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(value, list):
            stack.extend(value[:20])
    return "text" if _first_text_preview(payload, 1) else "unknown"


def _collect_media_metadata(value: Any, media: dict[str, Any]) -> None:
    if len(media) >= 12:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_norm = str(key or "")
            if key_norm in _MEDIA_MESSAGE_KEYS:
                media["type"] = _MEDIA_MESSAGE_KEYS[key_norm]
            key_lower = key_norm.casefold()
            if key_lower in {name.casefold() for name in _MEDIA_META_KEYS} and not isinstance(item, (dict, list)):
                if "url" not in key_lower and "token" not in key_lower:
                    media[key_norm] = item
            if isinstance(item, (dict, list)):
                _collect_media_metadata(item, media)
    elif isinstance(value, list):
        for item in value[:20]:
            _collect_media_metadata(item, media)


def _media_summary_from_payload(payload: dict[str, Any], message_type: str) -> dict[str, Any]:
    media: dict[str, Any] = {}
    _collect_media_metadata(payload, media)
    if message_type in {"image", "audio", "video", "document", "media"}:
        media.setdefault("type", message_type)
    if not media:
        return {}
    media["has_media"] = True
    return media


def _suggested_actions(message_type: str, has_media: bool) -> list[str]:
    actions = ["wpp_inbound_lookup", "wpp_register_staging_status"]
    if has_media and message_type in {"audio", "video"}:
        actions.append("wpp_transcribe_media")
    if has_media and message_type in {"image", "document"}:
        actions.append("review_media_metadata")
    return actions


def _event_context(event: dict[str, Any], mode: str, max_text_chars: int) -> dict[str, Any]:
    payload_obj = event.get("payload")
    payload: dict[str, Any] = payload_obj if isinstance(payload_obj, dict) else {}
    message_type = _message_type_from_payload(payload)
    media = _media_summary_from_payload(payload, message_type)
    item: dict[str, Any] = {
        "safe_event_id": event.get("event_id", ""),
        "status": event.get("status", ""),
        "created_at": event.get("created_at", ""),
        "message_type": message_type,
        "has_media": bool(media),
        "suggested_actions": _suggested_actions(message_type, bool(media)),
    }
    if mode in {"operator", "debug"}:
        preview = _first_text_preview(payload, max_text_chars)
        if preview:
            item["text_preview"] = preview
        if media:
            item["media"] = media
    if mode == "debug":
        item["payload_redacted"] = payload
    return item


def get_thread_context(
    thread: str = "",
    contact: str = "",
    limit: int = 20,
    mode: str = "summary",
    max_text_chars: int = 160,
) -> dict[str, Any]:
    """Return a bounded, sanitized local-store context view for a WPP thread.

    Uses only already-ingested local inbound events.  It never fetches provider
    history, never sends, and never exposes raw WhatsApp refs or media URLs.
    """
    mode_norm = str(mode or "summary").strip().lower()
    if mode_norm not in _CONTEXT_MODES:
        mode_norm = "summary"
    max_text = max(0, min(int(max_text_chars or 160), 500))
    events_raw = lookup_inbound_events(thread=thread, contact=contact, limit=limit)
    events = [_event_context(event, mode_norm, max_text) for event in events_raw]
    type_counts: dict[str, int] = {}
    media_counts: dict[str, int] = {}
    for event in events:
        msg_type = str(event.get("message_type") or "unknown")
        type_counts[msg_type] = type_counts.get(msg_type, 0) + 1
        if event.get("has_media"):
            media_counts[msg_type] = media_counts.get(msg_type, 0) + 1
    return {
        "ok": True,
        "source": "local_inbound_store",
        "mode": mode_norm,
        "thread_filter_set": bool(thread),
        "contact_filter_set": bool(contact),
        "message_count": len(events),
        "type_counts": type_counts,
        "media_counts": media_counts,
        "events": events,
    }


_TRANSCRIBABLE_MEDIA_TYPES = {"audio", "voice", "video", "ptt"}
_TRANSCRIPTION_SAFETY_FLAGS = {
    "transcription_performed": False,
    "download_performed": False,
    "stt_provider_called": False,
    "provider_history_used": False,
    "send_performed": False,
    "llm_used": False,
    "transcript_persisted": False,
    "raw_media_exposed": False,
}


def _transcription_safety_flags() -> dict[str, bool]:
    return dict(_TRANSCRIPTION_SAFETY_FLAGS)


def _safe_inbound_event_id(value: Any) -> str:
    event_id = str(value or "").strip()
    if re.fullmatch(r"inbound_[A-Za-z0-9_-]{1,80}", event_id):
        return event_id
    return ""


def _safe_short_token(value: Any, default: str, max_len: int = 40) -> str:
    token = str(value or "").strip().lower()[:max_len]
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", token):
        return token
    return default


def _safe_language(value: Any) -> str:
    language = str(value or "").strip().lower()[:32]
    if not language:
        return ""
    if re.fullmatch(r"[a-z]{2,8}(?:[-_][a-z0-9]{2,8})?", language):
        return language
    return ""


def _payload_has_voice_hint(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_norm = str(key or "").strip().lower()
            if key_norm in {"ptt", "voice", "isvoice", "is_voice"} and bool(item):
                return True
            if key_norm in {"messagetype", "type"} and str(item or "").strip().lower() in {"ptt", "voice"}:
                return True
            if isinstance(item, (dict, list)) and _payload_has_voice_hint(item):
                return True
    elif isinstance(value, list):
        return any(_payload_has_voice_hint(item) for item in value[:20])
    return False


def _transcribable_media_from_payload(payload: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    message_type = _message_type_from_payload(payload)
    media = _media_summary_from_payload(payload, message_type)
    media_type = str(media.get("type") or message_type or "unknown").strip().lower()[:40]
    if media_type == "audio" and _payload_has_voice_hint(payload):
        media_type = "voice"
    is_transcribable = media_type in _TRANSCRIBABLE_MEDIA_TYPES
    safe_media = _safe_media_for_summary(media) if media else {}
    if is_transcribable:
        safe_media.setdefault("type", "voice" if media_type == "ptt" else media_type)
    return is_transcribable, ("voice" if media_type == "ptt" else media_type), safe_media


def _media_transcription_row_to_status(row: sqlite3.Row) -> dict[str, Any]:
    try:
        media = json.loads(row["media_metadata_json"] or "{}")
    except json.JSONDecodeError:
        media = {}
    try:
        flags = json.loads(row["safety_flags_json"] or "{}")
    except json.JSONDecodeError:
        flags = {}
    safe_flags = _transcription_safety_flags()
    if isinstance(flags, dict):
        for key in safe_flags:
            safe_flags[key] = bool(flags.get(key, safe_flags[key]))
    return {
        "status_id": row["id"],
        "event_id": row["event_id"],
        "status": row["status"],
        "reason": row["reason"],
        "mode": row["mode"],
        "language": row["language"] or "",
        "provider": row["provider"],
        "media_type": row["media_type"],
        "media": media if isinstance(media, dict) else {},
        **safe_flags,
        "status_persisted": True,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def request_media_transcription(
    event_id: str,
    mode: str = "on_request",
    language: str = "",
    provider: str = "disabled",
    force: bool = False,
    persist_status: bool = True,
) -> dict[str, Any]:
    """Request fail-closed local transcription status for a sanitized inbound media event.

    Phase 1 intentionally does not download media, call STT/cloud/LLM, fetch
    provider history, send messages, expose raw media, or persist transcripts.
    It only records a local status row for safe ``inbound_*`` event IDs whose
    already-sanitized payload indicates audio/voice/video media.
    """
    init_db()
    safe_event_id = _safe_inbound_event_id(event_id)
    flags = _transcription_safety_flags()
    if not safe_event_id:
        return {
            "ok": False,
            "status": "invalid_event_id",
            "error": "event_id_must_be_internal_inbound_id",
            "event_id_accepted": False,
            "status_persisted": False,
            **flags,
        }

    mode_norm = _safe_short_token(mode, "on_request")
    if mode_norm != "on_request":
        mode_norm = "on_request"
    language_norm = _safe_language(language)
    provider_norm = _safe_short_token(provider, "disabled")
    now = utc_now()

    with _connect() as conn:
        event = conn.execute(
            """
            SELECT id, payload_redacted_json, status, created_at
            FROM inbound_events
            WHERE id=?
            """,
            (safe_event_id,),
        ).fetchone()
        if event is None:
            return {
                "ok": False,
                "event_id": safe_event_id,
                "status": "not_found",
                "error": "inbound_event_not_found",
                "status_persisted": False,
                **flags,
            }
        try:
            payload = json.loads(event["payload_redacted_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        is_transcribable, media_type, media = _transcribable_media_from_payload(payload)
        if not is_transcribable:
            return {
                "ok": False,
                "event_id": safe_event_id,
                "status": "no_transcribable_media",
                "reason": "event_has_no_audio_voice_or_video_media",
                "message_type": _message_type_from_payload(payload),
                "status_persisted": False,
                **flags,
            }

        status_id = "transcription_" + uuid.uuid4().hex[:12]
        existing = conn.execute(
            "SELECT * FROM media_transcriptions WHERE event_id=?",
            (safe_event_id,),
        ).fetchone()
        if persist_status and existing is not None and not force:
            status = _media_transcription_row_to_status(existing)
            return {
                "ok": True,
                "source": "local_inbound_store",
                "local_status_only": True,
                "existing_status": True,
                **status,
            }

        result: dict[str, Any] = {
            "ok": True,
            "source": "local_inbound_store",
            "event_id": safe_event_id,
            "status": "blocked",
            "reason": "provider_not_configured",
            "mode": mode_norm,
            "language": language_norm,
            "provider": provider_norm,
            "media_type": media_type,
            "media": media,
            "local_status_only": True,
            "transcript_available": False,
            "status_persisted": False,
            **flags,
        }
        if persist_status:
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO media_transcriptions (
                        id, event_id, status, reason, mode, language, provider,
                        media_type, media_metadata_json, safety_flags_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        status_id,
                        safe_event_id,
                        "blocked",
                        "provider_not_configured",
                        mode_norm,
                        language_norm,
                        provider_norm,
                        media_type,
                        json.dumps(media, ensure_ascii=False, sort_keys=True),
                        json.dumps(flags, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
            else:
                status_id = existing["id"]
                conn.execute(
                    """
                    UPDATE media_transcriptions
                    SET status=?, reason=?, mode=?, language=?, provider=?,
                        media_type=?, media_metadata_json=?, safety_flags_json=?,
                        updated_at=?
                    WHERE event_id=?
                    """,
                    (
                        "blocked",
                        "provider_not_configured",
                        mode_norm,
                        language_norm,
                        provider_norm,
                        media_type,
                        json.dumps(media, ensure_ascii=False, sort_keys=True),
                        json.dumps(flags, ensure_ascii=False, sort_keys=True),
                        now,
                        safe_event_id,
                    ),
                )
            result["status_id"] = status_id
            result["status_persisted"] = True
        return result


def get_media_transcription_status(event_id: str = "", limit: int = 20) -> dict[str, Any]:
    """Read fail-closed media transcription status rows from the local store only."""
    init_db()
    try:
        limit_value = int(limit or 20)
    except (TypeError, ValueError):
        limit_value = 20
    limit_value = max(1, min(limit_value, 100))
    safe_event_id = _safe_inbound_event_id(event_id) if str(event_id or "").strip() else ""
    if str(event_id or "").strip() and not safe_event_id:
        return {
            "ok": False,
            "status": "invalid_event_id",
            "error": "event_id_must_be_internal_inbound_id",
            "event_id_accepted": False,
            "statuses": [],
            **_transcription_safety_flags(),
        }
    with _connect() as conn:
        if safe_event_id:
            rows = conn.execute(
                """
                SELECT * FROM media_transcriptions
                WHERE event_id=?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (safe_event_id, limit_value),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM media_transcriptions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit_value,),
            ).fetchall()
    return {
        "ok": True,
        "source": "local_inbound_store",
        "local_status_only": True,
        "event_filter_set": bool(safe_event_id),
        "limit": limit_value,
        "count": len(rows),
        "statuses": [_media_transcription_row_to_status(row) for row in rows],
        **_transcription_safety_flags(),
    }


_SUMMARY_MODES = {"stats", "brief", "timeline", "evidence"}
_SUMMARY_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|api[_-]?key|authorization|password|secret|access[_-]?token)\s*[:=]\s*[^\s,;]+"
)
_SUMMARY_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}")
_SUMMARY_TOKEN_PREFIX_RE = re.compile(r"\b(?:sk|ghp|xoxb)-[A-Za-z0-9_-]{8,}\b")
_SUMMARY_URL_RE = re.compile(r"(?i)\b(?:https?://|data:|blob:)[^\s]+")
_SUMMARY_PHONE_RE = re.compile(r"(?<!\d)\+?\d{10,15}(?!\d)")
_SUMMARY_WA_REF_RE = re.compile(r"@[A-Za-z0-9._-]*(?:g\.us|lid|s\.whatsapp\.net)", re.IGNORECASE)
_SUMMARY_LONG_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b")


def _sanitize_summary_text(value: Any, max_chars: int = 500) -> str:
    text = _truncate_text(str(value or ""), max_chars)
    if not text:
        return ""
    text = _SUMMARY_URL_RE.sub("<redacted-url>", text)
    text = _SUMMARY_SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _SUMMARY_BEARER_RE.sub("Bearer <redacted>", text)
    text = _SUMMARY_TOKEN_PREFIX_RE.sub("<redacted-token>", text)
    text = _SUMMARY_WA_REF_RE.sub("<redacted-ref>", text)
    text = _replace_phone_mentions_with_names(text)
    text = _SUMMARY_PHONE_RE.sub(lambda match: _mask_phone_fragment(match.group(0)), text)
    text = _SUMMARY_LONG_B64_RE.sub("<redacted-base64>", text)
    return _truncate_text(text, max_chars)


def _clamp_int(value: Any, default: int, low: int, high: int) -> tuple[int, bool]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default, True
    clamped = max(low, min(number, high))
    return clamped, clamped != number


def _summary_window(events_asc: list[dict[str, Any]]) -> dict[str, Any]:
    if not events_asc:
        return {"first_created_at": None, "last_created_at": None}
    return {
        "first_created_at": events_asc[0].get("created_at") or None,
        "last_created_at": events_asc[-1].get("created_at") or None,
    }


def _safe_media_for_summary(media: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in media.items():
        key_text = str(key or "")[:80]
        key_lower = key_text.casefold()
        if any(part in key_lower for part in ("url", "uri", "token", "secret", "key", "path", "sha")):
            continue
        if isinstance(value, bool):
            safe[key_text] = value
        elif isinstance(value, int | float):
            safe[key_text] = value
        elif isinstance(value, str):
            cleaned = _sanitize_summary_text(_sanitize_payload(value), 120)
            if not isinstance(cleaned, str):
                continue
            if cleaned.startswith("<redacted") or re.search(r"(?i)(?:https?://|data:|blob:)", cleaned):
                continue
            safe[key_text] = cleaned
    return safe


def _participant_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("participant", "sender", "author", "from"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    for key in ("participant", "sender", "author", "from"):
        value = body.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _sender_label_from_payload(payload: dict[str, Any]) -> tuple[str, str, str]:
    participant = _participant_payload(payload)
    name = ""
    for key in ("pushName", "displayName", "name", "title", "notify", "verifiedName"):
        name = _safe_person_name(participant.get(key), limit=60)
        if name:
            break
    phone_masked = ""
    for key in ("phone", "phone_e164", "number"):
        phone_masked = _safe_existing_phone_mask(participant.get(key))
        if phone_masked:
            break
    if name and phone_masked:
        return f"{name} ({phone_masked})", name, phone_masked
    if name:
        return name, name, ""
    if phone_masked:
        return phone_masked, "", phone_masked
    return "", "", ""


def _summary_event(event: dict[str, Any], max_text_chars: int) -> dict[str, Any]:
    payload_obj = event.get("payload")
    payload: dict[str, Any] = payload_obj if isinstance(payload_obj, dict) else {}
    message_type = _message_type_from_payload(payload)
    media = _safe_media_for_summary(_media_summary_from_payload(payload, message_type))
    item: dict[str, Any] = {
        "safe_event_id": event.get("event_id", ""),
        "created_at": event.get("created_at", ""),
        "status": event.get("status", ""),
        "message_type": message_type,
        "has_media": bool(media),
    }
    if media:
        item["media"] = media
    sender_label, sender_display_name, sender_phone_masked = _sender_label_from_payload(payload)
    if sender_label:
        item["sender_label"] = sender_label
    if sender_display_name:
        item["sender_display_name"] = sender_display_name
    if sender_phone_masked:
        item["sender_phone_masked"] = sender_phone_masked
    if max_text_chars > 0:
        preview = _first_text_preview(payload, max_text_chars)
        if preview:
            item["text_preview"] = _sanitize_summary_text(preview, max_text_chars)
    return item


def _summary_counts(events: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    type_counts: dict[str, int] = {}
    media_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for event in events:
        message_type = str(event.get("message_type") or "unknown")
        status = str(event.get("status") or "unknown")
        type_counts[message_type] = type_counts.get(message_type, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
        if event.get("has_media"):
            media_type = str((event.get("media") or {}).get("type") or message_type or "media")
            media_counts[media_type] = media_counts.get(media_type, 0) + 1
    return type_counts, media_counts, status_counts


def _conversation_headline(message_count: int, type_counts: dict[str, int], media_counts: dict[str, int]) -> str:
    if message_count == 0:
        return "Nenhum evento local encontrado."
    type_bits = ", ".join(f"{key}: {value}" for key, value in sorted(type_counts.items())) or "sem tipo"
    media_total = sum(media_counts.values())
    media_bit = f"; {media_total} com mídia" if media_total else ""
    return f"{message_count} evento(s) local(is); tipos: {type_bits}{media_bit}."


def _conversation_bullets(
    message_count: int,
    events_desc: list[dict[str, Any]],
    type_counts: dict[str, int],
    status_counts: dict[str, int],
) -> list[str]:
    if message_count == 0:
        return ["Sem mensagens no inbound_events local para o escopo solicitado."]
    bullets = [
        f"Total local analisado: {message_count} evento(s).",
        "Tipos: " + (", ".join(f"{key}={value}" for key, value in sorted(type_counts.items())) or "nenhum"),
        "Status: " + (", ".join(f"{key}={value}" for key, value in sorted(status_counts.items())) or "nenhum"),
    ]
    latest = events_desc[0] if events_desc else {}
    if latest:
        bullets.append(
            "Mais recente: "
            f"{latest.get('message_type', 'unknown')} em {latest.get('created_at') or 'data_indisponivel'}."
        )
    return bullets


def get_conversation_summary(
    thread: str = "",
    contact: str = "",
    limit: int = 50,
    mode: str = "brief",
    max_text_chars: int = 160,
    include_evidence: bool = False,
) -> dict[str, Any]:
    """Return a deterministic, read-only summary from the local inbound store.

    This lane uses only ``lookup_inbound_events`` / ``inbound_events`` data that
    has already been sanitized at ingest time. It never calls an LLM, provider
    history, send/draft/approval/outbox paths, and never persists the summary.
    Raw thread/contact filters are intentionally not echoed back.
    """
    warnings: list[str] = []
    mode_norm = str(mode or "brief").strip().lower()
    if mode_norm not in _SUMMARY_MODES:
        mode_norm = "brief"
        warnings.append("invalid_mode_defaulted_to_brief")

    limit_value, limit_clamped = _clamp_int(limit, 50, 1, 100)
    if limit_clamped:
        warnings.append("limit_clamped")
    max_text_value, max_text_clamped = _clamp_int(max_text_chars, 160, 0, 500)
    if max_text_clamped:
        warnings.append("max_text_chars_clamped")

    thread_value = str(thread or "").strip()
    contact_value = str(contact or "").strip()
    thread_filter_set = bool(thread_value)
    contact_filter_set = bool(contact_value)
    if not thread_filter_set and not contact_filter_set:
        warnings.append("unscoped_local_scan")

    raw_events_desc_all = lookup_inbound_events(thread=thread_value, contact=contact_value, limit=limit_value)
    raw_events_desc = []
    hidden_system_events = 0
    for event in raw_events_desc_all:
        payload_obj = event.get("payload") if isinstance(event, dict) else {}
        payload = payload_obj if isinstance(payload_obj, dict) else {}
        if _payload_message_type(payload) == "system":
            hidden_system_events += 1
            continue
        raw_events_desc.append(event)
    if hidden_system_events:
        warnings.append("system_events_hidden")
    events_desc = [_summary_event(event, max_text_value) for event in raw_events_desc]
    events_asc = list(reversed(events_desc))
    if not events_desc:
        warnings.append("no_local_events")

    type_counts, media_counts, status_counts = _summary_counts(events_desc)
    result: dict[str, Any] = {
        "ok": True,
        "source": "local_inbound_store",
        "generated_by": "deterministic_local_v1",
        "llm_used": False,
        "provider_history_used": False,
        "send_performed": False,
        "summary_persisted": False,
        "read_only": True,
        "local_store_only": True,
        "sends_messages": False,
        "fetches_provider_history": False,
        "exposes_raw_refs": False,
        "mode": mode_norm,
        "thread_filter_set": thread_filter_set,
        "contact_filter_set": contact_filter_set,
        "limit": limit_value,
        "max_text_chars": max_text_value,
        "message_count": len(events_desc),
        "type_counts": type_counts,
        "media_counts": media_counts,
        "status_counts": status_counts,
        "window": _summary_window(events_asc),
        "warnings": warnings,
        "hidden_system_events": hidden_system_events,
    }

    if mode_norm == "brief":
        result["headline"] = _conversation_headline(len(events_desc), type_counts, media_counts)
        result["bullets"] = _conversation_bullets(len(events_desc), events_desc, type_counts, status_counts)
        previews = [
            {
                key: event[key]
                for key in ("safe_event_id", "created_at", "message_type", "text_preview")
                if key in event
            }
            for event in events_desc[:5]
            if event.get("text_preview")
        ]
        if previews:
            result["latest_previews"] = previews
        actions: list[str] = []
        for event in events_desc:
            for action in _suggested_actions(str(event.get("message_type") or "unknown"), bool(event.get("has_media"))):
                if action not in actions:
                    actions.append(action)
        result["suggested_actions"] = actions
    elif mode_norm == "timeline":
        result["timeline"] = events_asc
    elif mode_norm == "evidence":
        result["timeline"] = events_asc
        result["evidence"] = [
            {
                key: event[key]
                for key in ("safe_event_id", "created_at", "status", "message_type", "has_media", "media", "text_preview")
                if key in event
            }
            for event in events_asc
        ]

    if include_evidence and mode_norm != "evidence":
        result["evidence"] = [
            {
                key: event[key]
                for key in ("safe_event_id", "created_at", "status", "message_type", "has_media", "media", "text_preview")
                if key in event
            }
            for event in events_asc
        ]

    return result


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


def _queue_registration_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    subtype = str(item.get("kind") or "contact").strip().lower()
    command = "/addgp" if subtype == "group" else "/addct"
    label = "grupo" if subtype == "group" else "contato"
    title = str(item.get("display_name") or item.get("phone_masked") or item.get("safe_id") or "sem nome")[:120]
    suggested_name = title if title and title != "sem nome" else "Nome"
    primary_action = f"{command} {suggested_name} --item {index}"
    origin = str(item.get("source_group_name") or item.get("source_group_safe_id") or "").strip()
    message_type = str(item.get("last_message_type") or "").strip()
    why = (
        "grupo novo com atividade recente; cadastrar libera contexto e ações controladas"
        if subtype == "group"
        else "contato novo com interação recente; cadastrar evita perder follow-up comercial"
    )
    details: list[str] = []
    if origin:
        details.append(f"origem: {origin}")
    if message_type:
        details.append(f"última msg: {message_type}")
    if item.get("message_count") and int(item.get("message_count") or 0) > 1:
        details.append(f"{int(item.get('message_count') or 0)} mensagens")
    if subtype == "contact" and item.get("phone_status") == "unresolved":
        details.append("número não resolvido")
    if item.get("last_text_preview"):
        details.append(f"msg: {str(item.get('last_text_preview'))[:80]}")
    result: dict[str, Any] = {
        "kind": "registration",
        "subtype": subtype,
        "priority": "normal",
        "title": f"Cadastrar {label}: {title}",
        "operator_state": "ACTION_REQUIRED",
        "operator_title": f"Cadastrar {label}",
        "operator_summary": " · ".join(details) if details else why,
        "why_it_matters": why,
        "suggested_label": suggested_name,
        "safe_origin": origin,
        "primary_action": primary_action,
        "ignore_action": f"/ignorar {index}",
        "secondary_actions": [f"/ignorar {index}", "/ctxwpp", "/fila debug"],
        "staging_id": item.get("staging_id", ""),
        "display_name": title,
        "safe_id": item.get("safe_id", ""),
        "message_count": int(item.get("message_count") or 1),
        "created_at": item.get("created_at"),
        "last_seen_at": item.get("last_seen_at") or item.get("created_at"),
        "actions": [primary_action],
    }
    for key in (
        "phone_masked", "last4", "source_group_safe_id", "source_group_name",
        "last_message_type", "has_media", "last_text_preview", "context_summary",
        "recent_messages", "phone_status", "identity_note",
    ):
        if item.get(key) not in (None, "", []):
            result[key] = item[key]
    return result


def _queue_context_item(event: dict[str, Any]) -> dict[str, Any]:
    context = _event_context(event, "operator", 140)
    preview = context.get("text_preview")
    if preview:
        context["text_preview"] = _sanitize_summary_text(preview, 140)
    message_type = context.get("message_type") or "unknown"
    has_media = bool(context.get("has_media"))
    safe_preview = context.get("text_preview", "")
    operator_summary = safe_preview or ("mídia recebida" if has_media else f"mensagem {message_type} recebida")
    return {
        "kind": "context",
        "priority": "info",
        "title": f"Inbound recente: {message_type}",
        "operator_state": "ACTION_REQUIRED" if safe_preview or has_media else "INFO",
        "operator_title": "Revisar mensagem recente",
        "operator_summary": operator_summary,
        "why_it_matters": "contexto recente pode exigir resposta, cadastro ou follow-up",
        "safe_preview": safe_preview,
        "primary_action": "/ctxwpp",
        "secondary_actions": ["/fila debug"],
        "safe_event_id": context.get("safe_event_id", ""),
        "created_at": context.get("created_at", ""),
        "status": context.get("status", ""),
        "message_type": message_type,
        "has_media": has_media,
        "text_preview": safe_preview,
        "media": context.get("media", {}),
        "actions": list(context.get("suggested_actions") or []),
    }


def _queue_operator_summary(items: list[dict[str, Any]], counts: dict[str, Any], warnings: list[str], latest_inbound_created_at: Any) -> dict[str, Any]:
    total = int(counts.get("total") or len(items) or 0)
    if total:
        headline = f"{total} " + ("ação" if total == 1 else "ações") + " no WhatsApp profissional"
        health = "warning" if warnings else "ok"
    elif "stale_local_inbound_store" in warnings:
        headline = "Sem ação atual — contexto antigo oculto"
        health = "warning"
    else:
        headline = "Sem ação agora no WhatsApp profissional"
        health = "ok"

    best_item = next((item for item in items if item.get("primary_action")), None)
    best_next_action = ""
    if best_item:
        title = str(best_item.get("operator_title") or best_item.get("title") or "agir").strip()
        action = str(best_item.get("primary_action") or "").strip()
        best_next_action = f"{title}: {action}" if action else title
    elif not items:
        best_next_action = "Aguardar novo inbound ou abrir /crm next"

    risk_bits: list[str] = []
    if "expired_pending_approvals_hidden" in warnings:
        risk_bits.append(f"{counts.get('expired_pending_approvals', 0)} approval(s) expirado(s) oculto(s)")
    if "stale_local_inbound_store" in warnings:
        risk_bits.append("contexto local antigo oculto")

    return {
        "headline": headline,
        "health": health,
        "best_next_action": best_next_action,
        "risk_note": "; ".join(risk_bits),
        "identity_note": "fila usa local_inbound_store recente; raw refs permanecem ocultas",
        "latest_inbound_created_at": latest_inbound_created_at,
    }


def get_actionable_queue(limit: int = 10) -> dict[str, Any]:
    """Return a read-only sanitized operator queue for WhatsApp Ops.

    Combines pending approvals, registration staging, and recent local inbound
    context. This never sends, drafts, approves, fetches provider history, or
    exposes raw WhatsApp refs/message ids/approval tokens.
    """
    init_db()
    limit_value, limit_clamped = _clamp_int(limit, 10, 1, 50)
    with _connect() as conn:
        approval_rows = conn.execute(
            """
            SELECT id, draft_id, status, expires_at, created_at, resolved_at
            FROM approvals
            WHERE status='pending' AND expires_at > ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (utc_now(), limit_value),
        ).fetchall()
        expired_pending_approvals = int(conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status='pending' AND expires_at <= ?",
            (utc_now(),),
        ).fetchone()[0])
        counts = {
            "pending_approvals": int(approval_rows and len(approval_rows) or 0),
            "expired_pending_approvals": expired_pending_approvals,
            "active_staging": 0,
            "inbound_events": _count_rows(conn, "inbound_events"),
            "drafts_by_status": _count_by_status(conn, "drafts"),
            "outbox_by_status": _count_by_status(conn, "outbox"),
        }

    items: list[dict[str, Any]] = []
    for row in approval_rows:
        draft_id = row["draft_id"]
        items.append({
            "kind": "approval",
            "priority": "high",
            "title": "Aprovação WhatsApp pendente",
            "operator_state": "WAITING_APPROVAL",
            "operator_title": "Aprovar ou negar WhatsApp",
            "operator_summary": "há um draft aguardando decisão humana no card Telegram",
            "why_it_matters": "sem clique humano o Hunter não deve enviar nem executar ação real",
            "primary_action": "aprovar/negar no card Telegram",
            "secondary_actions": [f"wpp_status({draft_id})", "/fila debug"],
            "approval_id": row["id"],
            "draft_id": draft_id,
            "status": row["status"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "resolved_at": row["resolved_at"],
            "actions": [
                "aprovar/negar no card Telegram",
                f"wpp_status({draft_id})",
                f"wpp_send_approved({draft_id}) após aprovação",
            ],
        })

    staged_all = peek_staging()
    staged = [
        item for item in staged_all
        if str(item.get("last_message_type") or "").strip().lower() != "system"
    ][:limit_value]
    counts["active_staging"] = len(staged)
    for index, staged_item in enumerate(staged, 1):
        items.append(_queue_registration_item(staged_item, index))

    latest_inbound_created_at = None
    max_context_age = timedelta(hours=24)
    recent_events: list[dict[str, Any]] = []
    for event in lookup_inbound_events(limit=max(limit_value * 5, 20)):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if _message_type_from_payload(payload) == "system":
            continue
        if latest_inbound_created_at is None:
            latest_inbound_created_at = event.get("created_at") or None
        if _payload_from_self(payload):
            continue
        if not _is_recent_iso(event.get("created_at"), max_age=max_context_age):
            continue
        recent_events.append(event)
        if len(recent_events) >= limit_value:
            break
    for event in recent_events:
        items.append(_queue_context_item(event))

    counts["registration_items"] = len(staged)
    counts["context_items"] = len(recent_events)
    counts["total"] = len(items)
    warnings = ["limit_clamped"] if limit_clamped else []
    if latest_inbound_created_at and not _is_recent_iso(latest_inbound_created_at, max_age=max_context_age):
        warnings.append("stale_local_inbound_store")
    if expired_pending_approvals:
        warnings.append("expired_pending_approvals_hidden")
    if not items:
        warnings.append("empty_queue")
    operator_summary = _queue_operator_summary(items, counts, warnings, latest_inbound_created_at)
    return {
        "ok": True,
        "source": "local_inbound_store",
        "generated_by": "deterministic_local_v1",
        "read_only": True,
        "local_store_only": True,
        "send_performed": False,
        "draft_created": False,
        "approval_resolved": False,
        "provider_history_used": False,
        "summary_persisted": False,
        "exposes_raw_refs": False,
        "limit": limit_value,
        "context_max_age_hours": int(max_context_age.total_seconds() // 3600),
        "latest_inbound_created_at": latest_inbound_created_at,
        "operator_summary": operator_summary,
        "counts": counts,
        "items": items[: limit_value * 3],
        "operator_actions": [
            "wpp_actionable_queue",
            "wpp_cockpit_overview",
            "wpp_thread_context",
            "wpp_conversation_summary",
            "wpp_register_staging_status",
            "wpp_request_approval",
            "wpp_send_approved",
        ],
        "warnings": warnings,
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
