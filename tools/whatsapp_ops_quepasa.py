"""Fail-closed direct QuePasa client for WhatsApp Ops.

This module speaks QuePasa's native ``POST /send`` API.  It intentionally
returns structured refusals and performs no network I/O unless both the profile
transport flag and deterministic send guardrails have allowed the send.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _redact_error(text: str) -> str:
    redacted = str(text or "")
    for key in ("token", "api_key", "apikey", "authorization", "secret", "x-quepasa-token"):
        redacted = redacted.replace(key, "[redacted]").replace(key.upper(), "[redacted]")
    return redacted[:240]


def _normalize_endpoint_url(raw_url: str, endpoint: str) -> str:
    raw_url = str(raw_url or "").strip()
    endpoint = "/" + str(endpoint or "").strip("/")
    if not raw_url or endpoint == "/":
        return ""
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    path = parsed.path.rstrip("/")
    if path in {"", "/swagger/index.html", "/swagger/doc.json", "/swagger"}:
        normalized_path = endpoint
    elif path == endpoint or path.endswith(endpoint):
        normalized_path = path
    else:
        normalized_path = f"{path}{endpoint}"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def _normalize_send_url(raw_url: str) -> str:
    return _normalize_endpoint_url(raw_url, "/send")


def _normalize_group_create_url(raw_url: str) -> str:
    return _normalize_endpoint_url(raw_url, "/groups/create")


def _normalize_document_send_url(raw_url: str) -> str:
    return _normalize_endpoint_url(raw_url, "/senddocument")


def _target_chat_id(target: dict[str, Any]) -> str:
    if not isinstance(target, dict):
        return ""
    target_type = target.get("type")
    if target_type == "contact":
        return str(target.get("contact_id") or "").strip()
    if target_type == "group":
        return str(target.get("group_id") or "").strip()
    if target_type == "list":
        return str(target.get("list_id") or "").strip()
    return ""


def _safe_success_message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    safe: dict[str, Any] = {}
    for source, dest in (
        ("id", "message_id_hash"),
        ("chatId", "chat_ref_hash"),
        ("wid", "wid_hash"),
        ("trackId", "track_id_hash"),
    ):
        value = message.get(source)
        hashed = _hash_ref(value)
        if hashed:
            safe[dest] = hashed
    return safe


def _hash_ref(value: Any) -> str:
    raw = str(value or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] if raw else ""


def _safe_groupinfo(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    raw_ref = (
        value.get("id")
        or value.get("jid")
        or value.get("JID")
        or value.get("group_jid")
        or value.get("chatId")
    )
    safe: dict[str, Any] = {}
    hashed = _hash_ref(raw_ref)
    if hashed:
        safe["group_ref_hash"] = hashed
    if value.get("Name") or value.get("name") or value.get("title"):
        safe["name_present"] = True
    return safe


def _parse_json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _media_payload_fields(media: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not media:
        return None, None
    if not isinstance(media, dict):
        return None, "media_invalid"
    url = str(media.get("url") or "").strip()
    content = str(media.get("content") or "").strip()
    if bool(url) == bool(content):
        return None, "media_requires_exactly_one_source"
    fields: dict[str, Any] = {}
    if url:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None, "media_url_invalid"
        fields["url"] = url
    if content:
        if not content.lower().startswith("data:") or ";base64," not in content[:120].lower():
            return None, "media_content_invalid"
        fields["content"] = content
    filename = str(media.get("filename") or media.get("fileName") or "").strip()
    if filename:
        fields["fileName"] = filename[:160]
    return fields, None


def _post_message(send_url: str, api_key: str, chat_id: str, text: str, media: Any = None) -> dict[str, Any]:
    body_data: dict[str, Any] = {"chatId": chat_id}
    if text:
        body_data["text"] = text
    media_fields, media_error = _media_payload_fields(media)
    if media_error:
        return {"ok": False, "transport": "quepasa_direct", "error": media_error}
    if media_fields:
        body_data.update(media_fields)
    if not body_data.get("text") and not media_fields:
        return {"ok": False, "transport": "quepasa_direct", "error": "payload_invalid"}
    body = json.dumps(body_data, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-QUEPASA-TOKEN": api_key,
    }
    req = urllib.request.Request(send_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            parsed = _parse_json_body(resp.read(4096))
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "transport": "quepasa_direct",
            "error": "http_error",
            "status": int(exc.code),
        }
    except Exception as exc:  # pragma: no cover - defensive against transport stack
        return {"ok": False, "transport": "quepasa_direct", "error": _redact_error(str(exc))}

    if parsed.get("success") is not True:
        return {
            "ok": False,
            "transport": "quepasa_direct",
            "error": "quepasa_success_false",
            "status": status,
            "provider_status": str(parsed.get("status") or "")[:120],
        }

    safe_message = _safe_success_message(parsed.get("message"))
    return {
        "ok": True,
        "transport": "quepasa_direct",
        "status": status,
        "provider_status": str(parsed.get("status") or "")[:120],
        "media_sent": bool(media_fields),
        **safe_message,
    }


def _post_text(send_url: str, api_key: str, chat_id: str, text: str) -> dict[str, Any]:
    return _post_message(send_url, api_key, chat_id, text)


def _resolve_lid_participant(raw_url: str, api_key: str, participant: str) -> str:
    """Resolve a QuePasa LID JID to the phone string accepted by /groups/create.

    The returned phone is used only in the provider payload and is never exposed
    in tool output. Empty string means fail closed.
    """
    value = str(participant or "").strip()
    if not value.endswith("@lid"):
        return value
    useridentifier_url = _normalize_endpoint_url(raw_url, "/useridentifier")
    if not useridentifier_url:
        return ""
    url = useridentifier_url + "?" + urllib.parse.urlencode({"lid": value})
    req = urllib.request.Request(url, headers={"X-QUEPASA-TOKEN": api_key}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            parsed = _parse_json_body(resp.read(4096))
    except Exception:
        return ""
    if parsed.get("success") is not True:
        return ""
    phone = str(parsed.get("phone") or "").strip()
    return phone


def _normalize_group_participants(raw_url: str, api_key: str, participants: list[str]) -> list[str] | None:
    normalized: list[str] = []
    for participant in participants:
        resolved = _resolve_lid_participant(raw_url, api_key, str(participant).strip())
        if not resolved:
            return None
        normalized.append(resolved)
    return normalized


def _post_group_create(group_url: str, api_key: str, title: str, participants: list[str]) -> dict[str, Any]:
    body = json.dumps({"title": title, "participants": participants}, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-QUEPASA-TOKEN": api_key,
    }
    req = urllib.request.Request(group_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            parsed = _parse_json_body(resp.read(8192))
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "transport": "quepasa_direct_group_create",
            "error": "http_error",
            "status": int(exc.code),
        }
    except Exception as exc:  # pragma: no cover - defensive against transport stack
        return {"ok": False, "transport": "quepasa_direct_group_create", "error": _redact_error(str(exc))}

    if parsed.get("success") is not True:
        return {
            "ok": False,
            "transport": "quepasa_direct_group_create",
            "error": "quepasa_success_false",
            "status": status,
            "provider_status": str(parsed.get("status") or "")[:120],
        }

    return {
        "ok": True,
        "transport": "quepasa_direct_group_create",
        "status": status,
        "provider_status": str(parsed.get("status") or "")[:120],
        "participant_count": len(participants),
        **_safe_groupinfo(parsed.get("groupinfo")),
    }


def _quepasa_base_url(config: dict[str, Any]) -> str:
    quepasa_raw = (config or {}).get("quepasa")
    quepasa = quepasa_raw if isinstance(quepasa_raw, dict) else {}
    return str(
        quepasa.get("send_url")
        or quepasa.get("base_url")
        or os.getenv("WHATSAPP_OPS_QUEPASA_SEND_URL", "")
        or ""
    )


def _quepasa_api_key() -> str:
    return os.getenv("WHATSAPP_OPS_QUEPASA_API_KEY", "")


def send_via_quepasa(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    quepasa_raw = (config or {}).get("quepasa")
    quepasa = quepasa_raw if isinstance(quepasa_raw, dict) else {}
    if not _truthy(quepasa.get("send_enabled", False)):
        return {"ok": False, "error": "quepasa_send_disabled"}

    raw_url = _quepasa_base_url(config)
    media = (payload or {}).get("media") if isinstance(payload, dict) else None
    as_document = bool(isinstance(media, dict) and media.get("as_document"))
    send_url = (
        _normalize_document_send_url(str(raw_url or ""))
        if as_document
        else _normalize_send_url(str(raw_url or ""))
    )
    if not send_url:
        return {"ok": False, "error": "quepasa_send_url_missing"}

    api_key = _quepasa_api_key()
    if not api_key:
        return {"ok": False, "error": "quepasa_api_key_missing"}

    message = str((payload or {}).get("message") or "").strip()
    targets = (payload or {}).get("targets") or []
    if not isinstance(targets, list) or not targets:
        return {"ok": False, "error": "payload_invalid"}
    if not message and not media:
        return {"ok": False, "error": "payload_invalid"}

    results: list[dict[str, Any]] = []
    for target in targets:
        chat_id = _target_chat_id(target)
        if not chat_id:
            return {"ok": False, "error": "target_invalid"}
        result = _post_message(send_url, api_key, chat_id, message, media=media)
        results.append(result)
        if not result.get("ok"):
            return result

    if len(results) == 1:
        return results[0]
    return {"ok": True, "transport": "quepasa_direct", "messages": results}


def create_group_via_quepasa(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Create a WhatsApp group via QuePasa POST /groups/create.

    Fail-closed: requires both normal QuePasa sends and an explicit group-create
    flag. Never returns raw participant refs or raw group JIDs.
    """
    quepasa_raw = (config or {}).get("quepasa")
    quepasa = quepasa_raw if isinstance(quepasa_raw, dict) else {}
    if not _truthy(quepasa.get("send_enabled", False)):
        return {"ok": False, "error": "quepasa_send_disabled"}
    if not _truthy(quepasa.get("group_create_enabled", False)):
        return {"ok": False, "error": "quepasa_group_create_disabled"}

    raw_url = _quepasa_base_url(config)
    group_url = _normalize_group_create_url(str(raw_url or ""))
    if not group_url:
        return {"ok": False, "error": "quepasa_group_create_url_missing"}

    api_key = _quepasa_api_key()
    if not api_key:
        return {"ok": False, "error": "quepasa_api_key_missing"}

    group = (payload or {}).get("group_create") if isinstance(payload, dict) else None
    group = group if isinstance(group, dict) else {}
    title = str(group.get("title") or "").strip()
    participants = group.get("participants") or []
    if not title or not isinstance(participants, list) or not participants:
        return {"ok": False, "error": "payload_invalid"}
    clean_participants = [str(p).strip() for p in participants if str(p).strip()]
    if len(clean_participants) != len(participants) or not clean_participants:
        return {"ok": False, "error": "payload_invalid"}
    provider_participants = _normalize_group_participants(str(raw_url or ""), api_key, clean_participants)
    if not provider_participants:
        return {"ok": False, "error": "participant_lid_resolution_failed"}

    return _post_group_create(group_url, api_key, title, provider_participants)
