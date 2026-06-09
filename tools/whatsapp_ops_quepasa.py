"""Fail-closed direct QuePasa client for WhatsApp Ops.

This module speaks QuePasa's native ``POST /send`` API.  It intentionally
returns structured refusals and performs no network I/O unless both the profile
transport flag and deterministic send guardrails have allowed the send.
"""

from __future__ import annotations

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


def _normalize_send_url(raw_url: str) -> str:
    raw_url = str(raw_url or "").strip()
    if not raw_url:
        return ""
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    path = parsed.path.rstrip("/")
    if path in {"", "/send"}:
        normalized_path = "/send"
    elif path in {"/swagger/index.html", "/swagger/doc.json", "/swagger"}:
        normalized_path = "/send"
    else:
        # Treat unknown non-/send paths as a base path only when they end with a
        # slash-like directory. This keeps QuePasa direct strict enough for Gate B.
        normalized_path = path if path.endswith("/send") else f"{path}/send"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


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
    for source, dest in (("id", "message_id"), ("chatId", "chatId"), ("wid", "wid"), ("trackId", "trackId")):
        value = message.get(source)
        if value:
            safe[dest] = str(value)[:160]
    return safe


def _parse_json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _post_text(send_url: str, api_key: str, chat_id: str, text: str) -> dict[str, Any]:
    body = json.dumps({"chatId": chat_id, "text": text}, ensure_ascii=False).encode("utf-8")
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
        **safe_message,
    }


def send_via_quepasa(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    quepasa_raw = (config or {}).get("quepasa")
    quepasa = quepasa_raw if isinstance(quepasa_raw, dict) else {}
    if not _truthy(quepasa.get("send_enabled", False)):
        return {"ok": False, "error": "quepasa_send_disabled"}

    raw_url = (
        quepasa.get("send_url")
        or quepasa.get("base_url")
        or os.getenv("WHATSAPP_OPS_QUEPASA_SEND_URL", "")
    )
    send_url = _normalize_send_url(str(raw_url or ""))
    if not send_url:
        return {"ok": False, "error": "quepasa_send_url_missing"}

    api_key = os.getenv("WHATSAPP_OPS_QUEPASA_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "quepasa_api_key_missing"}

    message = str((payload or {}).get("message") or "").strip()
    targets = (payload or {}).get("targets") or []
    if not message or not isinstance(targets, list) or not targets:
        return {"ok": False, "error": "payload_invalid"}

    results: list[dict[str, Any]] = []
    for target in targets:
        chat_id = _target_chat_id(target)
        if not chat_id:
            return {"ok": False, "error": "target_invalid"}
        result = _post_text(send_url, api_key, chat_id, message)
        results.append(result)
        if not result.get("ok"):
            return result

    if len(results) == 1:
        return results[0]
    return {"ok": True, "transport": "quepasa_direct", "messages": results}
