"""Hermes WhatsApp Ops tools backed by a local SQLite store.

Transport is fail-closed by default.  ``wpp_send_approved`` refuses unless all
code-level guardrails pass; prompt instructions are never the send gate.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from tools.registry import registry
from tools.whatsapp_ops_policy import evaluate_send_guardrails

try:  # config loading is best-effort; tool remains fail-closed if unavailable
    from hermes_cli.config import load_config
except Exception:  # pragma: no cover - defensive for stripped runtimes
    load_config = None
from tools.whatsapp_ops_store import (
    consume_latest_raw_ref,
    create_approval,
    create_draft,
    get_actionable_queue,
    get_cockpit_overview,
    get_conversation_summary,
    get_media_transcription_status,
    get_thread_context,
    get_transport_contact_ref,
    get_transport_group_ref,
    import_contact_list_local,
    ignore_staging_item,
    list_contact_segment_members,
    list_contact_segments,
    registration_staging_diagnostics,
    get_draft,
    get_latest_approval,
    get_send_allowlist_ids,
    get_valid_approval,
    hash_text,
    idempotency_used,
    init_db,
    list_contacts,
    lookup_inbound_events,
    mark_outbox_blocked,
    mark_outbox_result,
    peek_staging,
    register_contact_local,
    register_group_local,
    request_media_transcription,
    reserve_outbox_send,
    resolve_approval,
    _payload_from_self,
    resolve_contact,
    stage_raw_ref,
    sync_allowlist_from_env,
    update_draft_status,
)

TOOLSET = "whatsapp_ops"


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _default_config() -> dict[str, Any]:
    return {
        "send_enabled": False,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60, "telegram": {}},
        "allowlists": {"contacts": [], "groups": []},
        "quepasa": {"backend": "n8n_or_http", "send_enabled": False},
        "humanized_send": {
            "enabled": False,
            "delay_mode": "fixed",
            "delay_seconds": 0,
            "min_delay_seconds": 0.8,
            "max_delay_seconds": 6.0,
            "chars_per_second": 38.0,
            "max_blocks": 4,
            "max_blocks_max": 5,
            "min_blocks": 1,
            "target_block_chars": 260,
            "max_block_chars": 360,
            "typing": {"enabled": False, "presence_type": "text"},
        },
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _runtime_config() -> dict[str, Any]:
    """Load profile-scoped whatsapp_ops config, preserving fail-closed defaults."""
    cfg = _default_config()
    if load_config is None:
        return cfg
    try:
        loaded = load_config() or {}
    except Exception:
        return cfg
    whatsapp_ops = loaded.get("whatsapp_ops")
    if isinstance(whatsapp_ops, dict):
        return _deep_merge(cfg, whatsapp_ops)
    return cfg


def _draft_for_policy(draft: dict[str, Any] | None) -> dict[str, Any] | None:
    if draft is None:
        return None
    try:
        raw_targets = json.loads(draft["targets_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        raw_targets = []
    targets = [_normalize_target_for_policy(target) for target in (raw_targets or [])]
    media = _load_draft_media(draft)
    return {
        "id": draft.get("id"),
        "status": draft.get("status"),
        "targets": targets,
        "message": draft.get("message", ""),
        "message_hash": draft.get("message_hash", ""),
        "idempotency_key": draft.get("idempotency_key", ""),
        "has_untrusted_media": bool(media.get("_invalid")),
    }


def _normalize_target_for_policy(target: Any) -> dict[str, Any]:
    if not isinstance(target, dict):
        return {"type": "invalid", "ambiguous": True}
    target_type = str(target.get("type") or "contact").strip().lower()
    normalized = dict(target)
    normalized["type"] = target_type
    if target_type == "contact":
        if not normalized.get("contact_id"):
            query = str(normalized.get("ref") or normalized.get("alias") or normalized.get("name") or "").strip()
            resolved = resolve_contact(query) if query else {"ok": True, "ambiguous": True, "matches": []}
            if resolved.get("ambiguous") or not resolved.get("match"):
                normalized["ambiguous"] = True
            else:
                match = resolved["match"]
                normalized["contact_id"] = match.get("contact_id")
                normalized["display_name"] = match.get("display_name")
                normalized["whitelisted"] = bool(match.get("whitelisted"))
        return normalized
    if target_type == "group_create":
        members = normalized.get("member_aliases") or normalized.get("participants") or []
        if not isinstance(members, list):
            members = []
        participant_contact_ids: list[str] = []
        unresolved = 0
        for member in members:
            query = str(member or "").strip()
            resolved = resolve_contact(query) if query else {"ok": True, "ambiguous": True, "matches": []}
            if resolved.get("ambiguous") or not resolved.get("match"):
                unresolved += 1
                continue
            contact_id = str((resolved.get("match") or {}).get("contact_id") or "").strip()
            if contact_id:
                participant_contact_ids.append(contact_id)
        normalized["participant_contact_ids"] = participant_contact_ids
        normalized["participant_count"] = len(participant_contact_ids)
        if unresolved or not participant_contact_ids:
            normalized["ambiguous"] = True
            normalized["unresolved_members"] = unresolved or len(members)
        return normalized
    return normalized


def _config_with_runtime_allowlist(cfg: dict[str, Any]) -> dict[str, Any]:
    merged = dict(cfg or {})
    allowlists_raw = merged.get("allowlists")
    allowlists = dict(allowlists_raw) if isinstance(allowlists_raw, dict) else {}
    try:
        runtime_allowlists = get_send_allowlist_ids()
    except Exception:
        runtime_allowlists = {"contacts": [], "groups": []}
    allowlists["contacts"] = sorted(set(allowlists.get("contacts") or []) | set(runtime_allowlists.get("contacts") or []))
    allowlists["groups"] = sorted(set(allowlists.get("groups") or []) | set(runtime_allowlists.get("groups") or []))
    merged["allowlists"] = allowlists
    return merged


def _approval_for_policy(approval: dict[str, Any] | None) -> dict[str, Any] | None:
    if approval is None:
        return None
    return {
        "status": approval.get("status"),
        "token_valid": True,
        "expires_at": approval.get("expires_at"),
        "message_hash": approval.get("message_hash"),
    }


_ALLOWED_MEDIA_TYPES = {"image", "audio", "video", "document"}
_FORBIDDEN_URL_HINTS = {"token", "secret", "sig", "signature", "key", "apikey", "api_key", "auth", "expires"}


def _normalize_media_for_draft(media: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    if media in (None, {}, ""):
        return None, None
    if not isinstance(media, dict):
        return None, "media_invalid"
    media_type = str(media.get("type") or media.get("kind") or "document").strip().lower()
    if media_type not in _ALLOWED_MEDIA_TYPES:
        return None, "media_type_invalid"
    url = str(media.get("url") or "").strip()
    content = str(media.get("content") or "").strip()
    if bool(url) == bool(content):
        return None, "media_requires_exactly_one_source"
    if content:
        # Persistent drafts must not store raw media/base64 blobs. Use a URL to a
        # trusted/static asset instead, then approve/send through normal gates.
        return None, "media_content_not_allowed_in_draft"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "media_url_invalid"
    if parsed.query or parsed.fragment:
        return None, "media_url_must_be_token_free"
    lower_url = url.casefold()
    if any(hint in lower_url for hint in _FORBIDDEN_URL_HINTS):
        return None, "media_url_must_be_token_free"
    filename = str(media.get("filename") or media.get("fileName") or "").strip()[:120]
    mime = str(media.get("mime") or media.get("mimetype") or "").strip()[:120]
    normalized: dict[str, Any] = {
        "type": media_type,
        "url": url,
        "as_document": bool(media.get("as_document") or media_type == "document"),
    }
    if filename:
        normalized["filename"] = filename
    if mime:
        normalized["mime"] = mime
    return normalized, None


def _load_draft_media(draft: dict[str, Any] | None) -> dict[str, Any]:
    if not draft:
        return {}
    raw = draft.get("media_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {"_invalid": True}
    return parsed if isinstance(parsed, dict) else {"_invalid": True}


def _safe_media_summary(media: dict[str, Any] | None) -> dict[str, Any] | None:
    if not media:
        return None
    return {
        "type": str(media.get("type") or "")[:40],
        "filename": str(media.get("filename") or "")[:120],
        "mime": str(media.get("mime") or "")[:120],
        "as_document": bool(media.get("as_document")),
        "url_present": bool(media.get("url")),
    }


def _load_json_env(env_name: str, default: Any, *fallback_env_names: str) -> Any:
    for name in (env_name, *fallback_env_names):
        raw = os.environ.get(name)
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return default
    return default


def _raw_contact_entries() -> list[dict[str, Any]]:
    contacts = _load_json_env("CONTACTS_JSON", [], "WHATSAPP_OPS_ALLOWLIST_CONTACTS_JSON")
    alias_map = _load_json_env("ALIAS_MAP_JSON", {}, "WHATSAPP_OPS_ALIAS_MAP_JSON")
    entries: list[dict[str, Any]] = []
    if isinstance(contacts, list):
        entries.extend(item for item in contacts if isinstance(item, dict))
    if isinstance(alias_map, dict):
        for alias, value in alias_map.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("alias", alias)
                entries.append(item)
            elif isinstance(value, str):
                entries.append({"alias": alias, "target_ref": value, "kind": "contact"})
    return entries


def _raw_group_entries() -> list[dict[str, Any]]:
    groups = _load_json_env("GROUPS_JSON", [], "WHATSAPP_OPS_ALLOWLIST_GROUPS_JSON")
    return [item for item in groups if isinstance(item, dict)] if isinstance(groups, list) else []


def _raw_contact_entries_from_config(cfg: dict[str, Any] | None) -> list[dict[str, Any]]:
    allowlists = (cfg or {}).get("allowlists") if isinstance(cfg, dict) else {}
    contacts = allowlists.get("contacts") if isinstance(allowlists, dict) else []
    entries: list[dict[str, Any]] = []
    if isinstance(contacts, list):
        for raw_ref in contacts:
            text = str(raw_ref or "").strip()
            if not text or text.startswith("contact_"):
                continue
            digits = _digits(text)
            if "@" not in text and len(digits) < 8:
                continue
            entries.append({"target_ref": text, "kind": "contact", "allow_send": True})
    return entries


def _raw_group_entries_from_config(cfg: dict[str, Any] | None) -> list[dict[str, Any]]:
    allowlists = (cfg or {}).get("allowlists") if isinstance(cfg, dict) else {}
    groups = allowlists.get("groups") if isinstance(allowlists, dict) else []
    entries: list[dict[str, Any]] = []
    if isinstance(groups, list):
        for raw_ref in groups:
            text = str(raw_ref or "").strip()
            if not text or text.startswith("list_"):
                continue
            digits = _digits(text)
            if "@" not in text and len(digits) < 8:
                continue
            entries.append({"target_ref": text, "kind": "group", "allow_send": True})
    return entries


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _resolve_raw_contact_ref(query: str, cfg: dict[str, Any] | None = None) -> str:
    """Resolve an operator alias to a raw provider ref for transport only.

    Raw refs are sourced from env/Infisical-rendered allowlists and are never
    returned to model/user output. Empty string means fail closed.
    """
    needle = str(query or "").strip()
    if not needle:
        return ""
    needle_norm = needle.casefold()
    needle_digits = _digits(needle)
    matches: list[str] = []
    for item in [*_raw_contact_entries(), *_raw_contact_entries_from_config(cfg)]:
        kind = str(item.get("kind") or "contact").strip().lower()
        if kind not in {"contact", "dm"}:
            continue
        raw_ref = str(item.get("target_ref") or item.get("ref") or item.get("contact_ref") or "").strip()
        if not raw_ref:
            continue
        candidates = {
            str(item.get("alias") or "").strip().casefold(),
            str(item.get("display_name") or "").strip().casefold(),
            str(item.get("contact_id") or "").strip().casefold(),
            ("contact_" + hash_text(raw_ref)[:16]).casefold(),
            raw_ref.casefold(),
        }
        raw_digits = _digits(raw_ref)
        if needle_norm in candidates or (needle_digits and needle_digits == raw_digits):
            if bool(item.get("allow_send", False)):
                matches.append(raw_ref)
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0]
    return get_transport_contact_ref(needle)


def _resolve_raw_group_ref(query: str, cfg: dict[str, Any] | None = None) -> str:
    """Resolve a group/list alias or sanitized list id to a raw provider ref for transport only."""
    needle = str(query or "").strip()
    if not needle:
        return ""
    needle_norm = needle.casefold()
    matches: list[str] = []
    for item in [*_raw_group_entries(), *_raw_group_entries_from_config(cfg)]:
        kind = str(item.get("kind") or "group").strip().lower()
        if kind not in {"group", "list"}:
            continue
        raw_ref = str(item.get("target_ref") or item.get("ref") or item.get("group_ref") or "").strip()
        if not raw_ref:
            continue
        candidates = {
            str(item.get("alias") or item.get("name") or "").strip().casefold(),
            str(item.get("display_name") or "").strip().casefold(),
            str(item.get("group_id") or item.get("list_id") or "").strip().casefold(),
            ("list_" + hash_text(raw_ref)[:16]).casefold(),
            raw_ref.casefold(),
        }
        if needle_norm in candidates and bool(item.get("allow_send", False)):
            matches.append(raw_ref)
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0]
    return get_transport_group_ref(needle)


def _provider_targets_for_transport(targets: list[dict[str, Any]], cfg: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], str | None]:
    provider_targets: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            return [], "payload_invalid"
        target_type = str(target.get("type") or "").strip().lower()
        if target_type == "contact":
            raw_ref = _resolve_raw_contact_ref(
                str(target.get("contact_id") or target.get("alias") or target.get("name") or target.get("ref") or ""),
                cfg,
            )
            if not raw_ref:
                return [], "target_ref_unresolved"
            provider_targets.append({"type": "contact", "contact_id": raw_ref})
        elif target_type in {"group", "list"}:
            raw_ref = _resolve_raw_group_ref(
                str(target.get("group_id") or target.get("list_id") or target.get("alias") or target.get("name") or ""),
                cfg,
            )
            if not raw_ref:
                return [], "target_ref_unresolved"
            provider_targets.append({"type": "group", "group_id": raw_ref})
        else:
            return [], "payload_invalid"
    return provider_targets, None


def _extract_group_create_target(draft: dict[str, Any] | None) -> dict[str, Any] | None:
    if not draft:
        return None
    try:
        raw_targets = json.loads(draft.get("targets_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw_targets, list):
        return None
    matches = [t for t in raw_targets if isinstance(t, dict) and str(t.get("type") or "").lower() == "group_create"]
    return matches[0] if len(matches) == 1 else None


def _group_create_provider_payload(
    draft_id: str,
    draft: dict[str, Any] | None,
    idempotency_key: str,
    cfg: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    target = _extract_group_create_target(draft)
    if not target:
        return None, "payload_invalid"
    title = str(target.get("name") or target.get("title") or "").strip()
    members = target.get("member_aliases") or target.get("participants") or []
    if not title or not isinstance(members, list) or not members:
        return None, "payload_invalid"
    participants: list[str] = []
    for member in members:
        raw_ref = _resolve_raw_contact_ref(str(member or ""), cfg)
        if not raw_ref:
            return None, "participant_ref_unresolved"
        participants.append(raw_ref)
    return {
        "draft_id": draft_id,
        "group_create": {"title": title, "participants": participants},
        "idempotency_key": idempotency_key,
    }, None


def _is_group_create_policy_draft(policy_draft: dict[str, Any] | None) -> bool:
    targets = (policy_draft or {}).get("targets") or []
    return any(isinstance(t, dict) and t.get("type") == "group_create" for t in targets)


def _telegram_destination(cfg: dict[str, Any]) -> tuple[str, str | None]:
    raw_approval_cfg = cfg.get("approval")
    approval_cfg: dict[str, Any] = raw_approval_cfg if isinstance(raw_approval_cfg, dict) else {}
    raw_tg_cfg = approval_cfg.get("telegram")
    tg_cfg: dict[str, Any] = raw_tg_cfg if isinstance(raw_tg_cfg, dict) else {}
    chat_id = str(
        tg_cfg.get("chat_id")
        or os.environ.get("WHATSAPP_OPS_APPROVAL_TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_HOME_CHANNEL")
        or ""
    ).strip()
    thread_id = str(
        tg_cfg.get("thread_id")
        or os.environ.get("WHATSAPP_OPS_APPROVAL_TELEGRAM_THREAD_ID")
        or os.environ.get("TELEGRAM_HOME_CHANNEL_THREAD_ID")
        or ""
    ).strip() or None
    return chat_id, thread_id


def _send_telegram_approval_card(approval: dict[str, Any], draft: dict[str, Any] | None, cfg: dict[str, Any]) -> dict[str, Any]:
    """Send a Telegram inline approval card without exposing approval secrets."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id, thread_id = _telegram_destination(cfg)
    if not token:
        return {"ok": False, "reason": "telegram_token_missing"}
    if not chat_id:
        return {"ok": False, "reason": "telegram_chat_missing"}
    approval_id = approval.get("approval_id", "")
    draft_id = approval.get("draft_id") or (draft or {}).get("id", "")
    text_preview = str((draft or {}).get("message", ""))[:700]
    media_summary = _safe_media_summary(_load_draft_media(draft))
    media_line = ""
    if media_summary:
        media_line = (
            "\nMídia: "
            f"{media_summary.get('type') or 'arquivo'}"
            f" · arquivo={bool(media_summary.get('filename'))}"
            f" · url_presente={bool(media_summary.get('url_present'))}\n"
        )
    is_group_create = False
    try:
        targets = json.loads(str((draft or {}).get("targets_json") or "[]"))
        is_group_create = any(isinstance(t, dict) and t.get("type") == "group_create" for t in targets)
    except Exception:
        is_group_create = False
    approval_note = (
        "Aprovar e enviar executa a criação via QuePasa/direct agora. Editar pede revisão sem enviar."
        if is_group_create
        else "Aprovar e enviar dispara via QuePasa/direct agora. Editar pede revisão sem enviar."
    )
    text = (
        "📲 <b>WhatsApp Ops approval</b>\n\n"
        f"Draft: <code>{draft_id}</code>\n"
        f"Approval: <code>{approval_id}</code>\n"
        f"{media_line}\n"
        f"<pre>{text_preview}</pre>\n\n"
        f"{approval_note}"
    )
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Aprovar e Enviar", "callback_data": f"wpp:a:{approval_id}"},
                {"text": "✏️ Editar", "callback_data": f"wpp:e:{approval_id}"},
                {"text": "❌ Negar", "callback_data": f"wpp:d:{approval_id}"},
            ]]
        },
        "disable_web_page_preview": True,
    }
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            payload["message_thread_id"] = thread_id
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "reason": "telegram_http_error", "status": exc.code}
    except Exception as exc:
        return {"ok": False, "reason": "telegram_error", "error": str(exc)[:120]}
    result = body.get("result") if isinstance(body, dict) else None
    return {
        "ok": bool(body.get("ok")) if isinstance(body, dict) else False,
        "message_id": str((result or {}).get("message_id", "")) if isinstance(result, dict) else "",
    }


def wpp_create_draft(
    targets: list[dict[str, Any]],
    message: str,
    send_at: str | None = None,
    media: dict[str, Any] | None = None,
    send_client: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> str:
    """Create a local WhatsApp draft. Never sends."""
    normalized_media, media_error = _normalize_media_for_draft(media)
    if media_error:
        return _json({"ok": False, "error": media_error})
    try:
        init_db()
        draft = create_draft(targets=targets, message=message, send_at=send_at, media=normalized_media)
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)[:200]})
    response = {"ok": True, **draft}
    media_summary = _safe_media_summary(normalized_media)
    if media_summary:
        response["media"] = media_summary
    return _json(response)


def _humanized_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _humanized_send_options(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = (cfg or {}).get("humanized_send")
    options = raw if isinstance(raw, dict) else {}
    typing_raw = options.get("typing")
    typing_options = typing_raw if isinstance(typing_raw, dict) else {}

    def _int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(options.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _float(name: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(options.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _str(name: str, default: str) -> str:
        value = str(options.get(name, default) or default).strip().lower()
        return value or default

    presence_type = str(typing_options.get("presence_type") or "text").strip().lower()
    if presence_type not in {"text", "audio"}:
        presence_type = "text"
    min_delay = _float("min_delay_seconds", 0.8, 0.0, 20.0)
    max_delay = _float("max_delay_seconds", max(min_delay, 6.0), min_delay, 30.0)
    chars_per_second = _float("chars_per_second", 38.0, 8.0, 120.0)
    max_block_chars = _int("max_block_chars", 360, 80, 1200)

    raw_max_blocks = options.get("max_blocks", 4)
    max_blocks_mode = "fixed"
    if isinstance(raw_max_blocks, str) and raw_max_blocks.strip().lower() in {"auto", "dynamic"}:
        max_blocks_mode = "dynamic"
        max_blocks = _int("max_blocks_max", 5, 2, 8)
    else:
        max_blocks = _int("max_blocks", 4, 2, 8)
    min_blocks = _int("min_blocks", 1, 1, max_blocks)
    target_block_chars = _int("target_block_chars", min(260, max_block_chars), 80, max_block_chars)

    return {
        "enabled": _humanized_truthy(options.get("enabled", False)),
        "delay_mode": _str("delay_mode", "fixed"),
        "delay_seconds": _float("delay_seconds", 0.0, 0.0, 30.0),
        "min_delay_seconds": min_delay,
        "max_delay_seconds": max_delay,
        "chars_per_second": chars_per_second,
        "max_blocks": max_blocks,
        "max_blocks_mode": max_blocks_mode,
        "min_blocks": min_blocks,
        "target_block_chars": target_block_chars,
        "max_block_chars": max_block_chars,
        "typing_enabled": _humanized_truthy(typing_options.get("enabled", False)),
        "typing_presence_type": presence_type,
    }


def _humanized_block_delay_seconds(block: str, options: dict[str, Any]) -> float:
    if str(options.get("delay_mode") or "fixed").lower() != "adaptive":
        return round(float(options.get("delay_seconds") or 0.0), 2)
    text = str(block or "").strip()
    if not text:
        return 0.0
    weighted_chars = len(text) + 20 * text.count("\n") + 8 * len(re.findall(r"[,;:!?]", text))
    estimate = weighted_chars / float(options.get("chars_per_second") or 38.0)
    bounded = max(float(options.get("min_delay_seconds") or 0.0), min(float(options.get("max_delay_seconds") or estimate), estimate))
    return round(bounded, 2)


def _effective_humanized_max_blocks(text: str, candidate_count: int, options: dict[str, Any]) -> int:
    cap = max(1, int(options.get("max_blocks") or 1))
    if str(options.get("max_blocks_mode") or "fixed").lower() != "dynamic":
        return cap
    clean_text = str(text or "").strip()
    if not clean_text:
        return 1
    target_chars = max(1, int(options.get("target_block_chars") or options.get("max_block_chars") or 360))
    by_size = max(1, (len(clean_text) + target_chars - 1) // target_chars)
    by_structure = max(1, int(candidate_count or 1))
    min_blocks = max(1, int(options.get("min_blocks") or 1))
    needed = max(min_blocks, min(cap, max(by_size, by_structure)))
    return max(1, min(cap, needed))


def _expand_oversized_humanized_candidates(candidates: list[str], max_block_chars: int) -> list[str]:
    expanded: list[str] = []
    for candidate in candidates:
        part = str(candidate or "").strip()
        if not part:
            continue
        if len(part) <= max_block_chars:
            expanded.append(part)
            continue
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", part) if s.strip()]
        if len(sentences) <= 1:
            expanded.append(part)
            continue
        expanded.extend(_pack_message_blocks(sentences, max_block_chars, 50))
    return expanded


def _limit_humanized_blocks(blocks: list[str], max_blocks: int) -> list[str]:
    clean = [str(block or "").strip() for block in blocks if str(block or "").strip()]
    if len(clean) > max_blocks:
        head = clean[: max_blocks - 1]
        tail = "\n\n".join(clean[max_blocks - 1:]).strip()
        return [*head, tail] if tail else head
    return clean


def _pack_message_blocks(candidates: list[str], max_block_chars: int, max_blocks: int) -> list[str]:
    blocks: list[str] = []
    current = ""
    for candidate in candidates:
        part = str(candidate or "").strip()
        if not part:
            continue
        separator = "\n\n" if "\n" in current or "\n" in part else " "
        proposed = f"{current}{separator}{part}" if current else part
        if current and len(proposed) > max_block_chars:
            blocks.append(current.strip())
            current = part
        else:
            current = proposed
    if current.strip():
        blocks.append(current.strip())
    if len(blocks) > max_blocks:
        head = blocks[: max_blocks - 1]
        tail = "\n\n".join(blocks[max_blocks - 1:]).strip()
        blocks = [*head, tail] if tail else head
    return blocks


def _split_humanized_message(message: str, cfg: dict[str, Any]) -> list[str]:
    options = _humanized_send_options(cfg)
    text = str(message or "").replace("\r\n", "\n").strip()
    if not options["enabled"] or not text:
        return [text]
    max_chars = int(options["max_block_chars"])
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if len(paragraphs) > 1:
        candidates = _expand_oversized_humanized_candidates(paragraphs, max_chars)
        max_blocks = _effective_humanized_max_blocks(text, len(candidates), options)
        return _limit_humanized_blocks(candidates, max_blocks) or [text]
    if len(text) <= max_chars:
        return [text]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(sentences) <= 1:
        return [text]
    max_blocks = _effective_humanized_max_blocks(text, len(sentences), options)
    return _pack_message_blocks(sentences, max_chars, max_blocks) or [text]


def _send_humanized_or_single(
    client: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    payload: dict[str, Any],
    cfg: dict[str, Any],
    sleep_fn: Callable[[float], None] | None = None,
    presence_client: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if payload.get("media"):
        return client(payload, cfg)
    blocks = _split_humanized_message(str(payload.get("message") or ""), cfg)
    if len(blocks) <= 1:
        return client(payload, cfg)
    options = _humanized_send_options(cfg)
    sleeper = sleep_fn or time.sleep
    results: list[dict[str, Any]] = []
    presence_results: list[dict[str, Any]] = []
    block_delays = [_humanized_block_delay_seconds(block, options) for block in blocks]
    typing_enabled = bool(options.get("typing_enabled")) and presence_client is not None

    for index, block in enumerate(blocks, start=1):
        wait_seconds = float(block_delays[index - 1])
        if typing_enabled and wait_seconds > 0:
            presence_payload = {
                "targets": payload.get("targets") or [],
                "presence_type": options.get("typing_presence_type") or "text",
                "duration_ms": int(wait_seconds * 1000),
                "humanized": {"block_index": index, "block_count": len(blocks)},
            }
            presence_result = presence_client(presence_payload, cfg)
            presence_results.append(presence_result)
            # Typing is best-effort; do not fail an already-approved send if the
            # indicator endpoint is unavailable.
            sleeper(wait_seconds)
        elif index > 1 and wait_seconds > 0:
            sleeper(wait_seconds)

        block_payload = dict(payload)
        block_payload["message"] = block
        block_payload["humanized"] = {"block_index": index, "block_count": len(blocks)}
        result = client(block_payload, cfg)
        results.append(result)
        if not bool(result.get("ok")):
            return {
                "ok": False,
                "transport": "humanized_sequence",
                "error": str(result.get("error") or "block_send_failed")[:120],
                "failed_block_index": index,
                "blocks_attempted": len(results),
                "delays_seconds": block_delays,
                "typing_attempted": bool(presence_results),
                "messages": results,
            }
    response = {
        "ok": True,
        "transport": "humanized_sequence",
        "blocks_sent": len(results),
        "delay_mode": str(options.get("delay_mode") or "fixed"),
        "max_blocks_mode": str(options.get("max_blocks_mode") or "fixed"),
        "effective_blocks": len(blocks),
        "max_blocks_cap": int(options.get("max_blocks") or len(blocks)),
        "delays_seconds": block_delays,
        "typing_attempted": bool(presence_results),
        "messages": results,
    }
    if str(options.get("delay_mode") or "fixed").lower() == "fixed":
        response["delay_seconds"] = float(options.get("delay_seconds") or 0.0)
    return response


def wpp_send_approved(
    draft_id: str,
    approval_token: str | None = None,
    config: dict[str, Any] | None = None,
    send_client: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    presence_client: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> str:
    """Send an explicitly human-approved draft only if guardrails allow it."""
    init_db()
    cfg = _config_with_runtime_allowlist(config if config is not None else _runtime_config())
    draft = get_draft(draft_id)
    approval = get_valid_approval(draft_id, approval_token)
    policy_draft = _draft_for_policy(draft)
    idempotency_key = (draft or {}).get("idempotency_key", "")
    result = evaluate_send_guardrails(
        config=cfg,
        draft=policy_draft,
        approval=_approval_for_policy(approval),
        idempotency_used=idempotency_used(idempotency_key) if idempotency_key else False,
    )
    if not result.allowed:
        return _json({"ok": False, "draft_id": draft_id, "reasons": result.reasons})

    is_group_create = _is_group_create_policy_draft(policy_draft)
    if is_group_create:
        payload, payload_error = _group_create_provider_payload(draft_id, draft, idempotency_key, cfg)
        if payload_error:
            return _json({"ok": False, "draft_id": draft_id, "reasons": [payload_error]})
        if send_client is None:
            from tools.whatsapp_ops_quepasa import create_group_via_quepasa

            client = create_group_via_quepasa
        else:
            client = send_client
    else:
        if send_client is None:
            from tools.whatsapp_ops_quepasa import send_presence_via_quepasa, send_via_quepasa

            client = send_via_quepasa
            presence = presence_client or send_presence_via_quepasa
        else:
            client = send_client
            presence = presence_client
        media = _load_draft_media(draft)
        provider_targets, target_error = _provider_targets_for_transport(policy_draft["targets"] if policy_draft else [], cfg)
        if target_error:
            return _json({"ok": False, "draft_id": draft_id, "reasons": [target_error]})
        payload = {
            "draft_id": draft_id,
            "targets": provider_targets,
            "message": draft["message"] if draft else "",
            "idempotency_key": idempotency_key,
        }
        if media:
            payload["media"] = media

    if idempotency_key and not reserve_outbox_send(draft_id, idempotency_key):
        return _json({"ok": False, "draft_id": draft_id, "reasons": ["idempotency_duplicate"]})

    send_result = (
        client(payload, cfg)
        if is_group_create
        else _send_humanized_or_single(client, payload, cfg, sleep_fn=sleep_fn, presence_client=presence)
    )
    if idempotency_key:
        if bool(send_result.get("ok")):
            mark_outbox_result(draft_id, idempotency_key, "sent")
            update_draft_status(draft_id, "sent")
        else:
            mark_outbox_result(draft_id, idempotency_key, "failed", str(send_result.get("error", "send_failed")))
            update_draft_status(draft_id, "failed")
    return _json({"ok": bool(send_result.get("ok")), "draft_id": draft_id, "send_result": send_result})


def wpp_request_approval(draft_id: str) -> str:
    cfg = _runtime_config()
    timeout = int((cfg.get("approval") or {}).get("timeout_minutes", 60))
    try:
        approval = create_approval(draft_id, timeout_minutes=timeout)
        draft = get_draft(draft_id)
        notification = _send_telegram_approval_card(approval, draft, cfg)
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)[:200]})
    return _json({"ok": True, "draft_id": draft_id, **approval, "notification": notification})


def wpp_resolve_approval(approval_id: str, decision: str, approver_ref: str = "") -> str:
    if os.environ.get("WHATSAPP_OPS_TRUSTED_APPROVAL_CONTEXT") != "telegram_callback":
        return _json({
            "ok": False,
            "approval_id": approval_id,
            "error": "trusted_approval_context_required",
        })
    try:
        resolved = resolve_approval(approval_id, decision=decision, approver_ref=approver_ref)
    except Exception as exc:
        return _json({"ok": False, "approval_id": approval_id, "error": str(exc)[:200]})
    return _json(resolved)


def wpp_schedule_draft(draft_id: str, send_at: str) -> str:
    update_draft_status(draft_id, "scheduled", send_at=send_at)
    return _json({"ok": True, "draft_id": draft_id, "status": "scheduled", "send_at": send_at})


def wpp_cancel(draft_id: str) -> str:
    update_draft_status(draft_id, "cancelled")
    return _json({"ok": True, "draft_id": draft_id, "status": "cancelled"})


def wpp_status(draft_id: str) -> str:
    draft = get_draft(draft_id)
    if draft is None:
        return _json({"ok": False, "error": "draft_not_found"})
    approval = get_latest_approval(draft_id)
    approval_safe = None
    if approval:
        approval_safe = {
            "approval_id": approval.get("id"),
            "status": approval.get("status"),
            "expires_at": approval.get("expires_at"),
            "created_at": approval.get("created_at"),
            "resolved_at": approval.get("resolved_at"),
        }
    response = {
        "ok": True,
        "draft_id": draft_id,
        "status": draft.get("status"),
        "send_at": draft.get("send_at"),
        "created_at": draft.get("created_at"),
        "approval": approval_safe,
    }
    media_summary = _safe_media_summary(_load_draft_media(draft))
    if media_summary:
        response["media"] = media_summary
    return _json(response)


def wpp_resolve_contact(nome_ou_numero: str) -> str:
    return _json(resolve_contact(nome_ou_numero))


def wpp_list_contacts(filtro: str = "") -> str:
    return _json({"ok": True, "filter": str(filtro)[:80], "contacts": list_contacts(filtro)})


def wpp_import_contact_list(
    list_name: str,
    contacts: list[dict[str, Any]],
    allow_send: bool = False,
    policy_group: str = "lead",
) -> str:
    return _json(import_contact_list_local(
        list_name=str(list_name or ""),
        contacts=contacts if isinstance(contacts, list) else [],
        allow_send=bool(allow_send),
        policy_group=str(policy_group or "lead")[:80],
    ))


def wpp_list_contact_segments(limit: int = 50) -> str:
    return _json({"ok": True, "segments": list_contact_segments(limit=limit)})


def wpp_list_contact_segment_members(list_ref: str, limit: int = 50) -> str:
    return _json(list_contact_segment_members(str(list_ref or ""), limit=limit))


def wpp_sync_allowlist() -> str:
    return _json(sync_allowlist_from_env())


def wpp_inbound_lookup(thread: str = "", contact: str = "", limit: int = 20) -> str:
    events = lookup_inbound_events(thread=str(thread or ""), contact=str(contact or ""), limit=limit)
    return _json({"ok": True, "thread_filter_set": bool(thread), "contact_filter_set": bool(contact), "events": events})


def wpp_thread_context(
    thread: str = "",
    contact: str = "",
    limit: int = 20,
    mode: str = "summary",
    max_text_chars: int = 160,
) -> str:
    context = get_thread_context(
        thread=str(thread or ""),
        contact=str(contact or ""),
        limit=limit,
        mode=str(mode or "summary"),
        max_text_chars=max_text_chars,
    )
    return _json(context)


def wpp_conversation_summary(
    thread: str = "",
    contact: str = "",
    limit: int = 50,
    mode: str = "brief",
    max_text_chars: int = 160,
    include_evidence: bool = False,
) -> str:
    summary = get_conversation_summary(
        thread=str(thread or ""),
        contact=str(contact or ""),
        limit=limit,
        mode=str(mode or "brief"),
        max_text_chars=max_text_chars,
        include_evidence=bool(include_evidence),
    )
    return _json(summary)


def wpp_transcribe_media(
    event_id: str,
    mode: str = "on_request",
    language: str = "",
    provider: str = "disabled",
    persist_status: bool = True,
) -> str:
    """Record a fail-closed local transcription status for an inbound media event.

    Phase 1 never downloads media, never calls STT/cloud/LLM, never sends, and
    never fetches provider history. It only inspects already-sanitized local
    inbound payload metadata for an internal ``inbound_*`` event id.
    """
    result = request_media_transcription(
        event_id=str(event_id or ""),
        mode=str(mode or "on_request"),
        language=str(language or ""),
        provider=str(provider or "disabled"),
        persist_status=bool(persist_status),
    )
    return _json(result)


def wpp_media_transcription_status(event_id: str = "", limit: int = 20) -> str:
    """Return sanitized local media transcription status rows only."""
    return _json(get_media_transcription_status(event_id=str(event_id or ""), limit=limit))


def wpp_cockpit_overview(limit: int = 10) -> str:
    cfg = _runtime_config()
    overview = get_cockpit_overview(limit=limit)
    overview["send_flags"] = {
        "send_enabled": bool(cfg.get("send_enabled", False)),
        "quepasa_send_enabled": bool((cfg.get("quepasa") or {}).get("send_enabled", False)),
        "approval_required": bool((cfg.get("approval") or {}).get("required", True)),
        "require_target_whitelist": bool(cfg.get("require_target_whitelist", True)),
    }
    return _json(overview)


def wpp_actionable_queue(limit: int = 10) -> str:
    """Return a read-only WhatsApp Ops operator queue with safe next actions."""
    cfg = _runtime_config()
    queue = get_actionable_queue(limit=limit)
    queue["send_flags"] = {
        "send_enabled": bool(cfg.get("send_enabled", False)),
        "quepasa_send_enabled": bool((cfg.get("quepasa") or {}).get("send_enabled", False)),
        "approval_required": bool((cfg.get("approval") or {}).get("required", True)),
        "require_target_whitelist": bool(cfg.get("require_target_whitelist", True)),
    }
    return _json(queue)


def wpp_ignore_staging_item(item: int = 0, staging_id: str = "") -> str:
    """Ignore a WhatsApp Ops registration queue item without sending anything."""
    return _json(ignore_staging_item(staging_id=str(staging_id or ""), item_index=item))


def _nested_dict(value: Any, *keys: str) -> dict[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}



def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""



def _registration_message_type(payload: dict[str, Any], data: dict[str, Any]) -> str:
    msg_type = _first_text(
        payload.get("type"),
        payload.get("messageType"),
        data.get("messageType"),
        data.get("type"),
    ).lower()
    if msg_type:
        return msg_type[:40]
    if payload.get("attachment") or payload.get("mediaUrl"):
        return "media"
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    for key in ("imageMessage", "audioMessage", "videoMessage", "documentMessage"):
        if key in message:
            return key.removesuffix("Message").lower()
    return "text" if payload.get("text") or message.get("conversation") else "unknown"



def _registration_has_media(payload: dict[str, Any], data: dict[str, Any], msg_type: str) -> bool:
    if payload.get("attachment") or payload.get("mediaUrl"):
        return True
    if msg_type in {"image", "audio", "video", "document", "media"}:
        return True
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    return any(key in message for key in ("imageMessage", "audioMessage", "videoMessage", "documentMessage"))



def _registration_hints(
    payload: dict[str, Any],
    data: dict[str, Any],
    chat: dict[str, Any],
    participant: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    msg_type = _registration_message_type(payload, data)
    has_media = _registration_has_media(payload, data, msg_type)
    group_name = _first_text(
        chat.get("subject"),
        chat.get("name"),
        chat.get("title"),
        chat.get("displayName"),
        payload.get("groupName"),
        payload.get("chatName"),
        data.get("groupName"),
        data.get("chatName"),
    )
    participant_name = _first_text(
        participant.get("title"),
        participant.get("name"),
        participant.get("pushName"),
        payload.get("senderName"),
        payload.get("pushName"),
        data.get("pushName"),
    )
    base = {"last_message_type": msg_type, "has_media": has_media}
    group_hint = dict(base)
    contact_hint = dict(base)
    if group_name:
        group_hint["group_name"] = group_name
        contact_hint["source_group_name"] = group_name
    if participant_name:
        contact_hint["participant_name"] = participant_name
    participant_phone = _first_text(
        participant.get("phone"),
        participant.get("phone_e164"),
        participant.get("phoneNumber"),
        participant.get("phone_number"),
        payload.get("senderPhone"),
        payload.get("participantPhone"),
        payload.get("phone"),
        data.get("senderPhone"),
        data.get("participantPhone"),
        data.get("phone"),
    )
    if participant_phone:
        contact_hint["phone"] = participant_phone
    return group_hint, contact_hint



def wpp_ingest_inbound_event(payload: dict[str, Any]) -> str:
    """Ingest a raw QuePasa inbound webhook payload into the sanitized local store."""
    try:
        from tools.whatsapp_ops_store import record_inbound_event
        body = _nested_dict(payload, "body")
        body_data = _nested_dict(body, "data")
        top_data = _nested_dict(payload, "data")
        data = body_data or top_data
        key = _nested_dict(data, "key")
        chat_obj = payload.get("chat")
        chat: dict[str, Any] = chat_obj if isinstance(chat_obj, dict) else {}
        participant_obj = payload.get("participant")
        participant: dict[str, Any] = participant_obj if isinstance(participant_obj, dict) else {}

        source_event_id = str(
            payload.get("id")
            or payload.get("message_id")
            or payload.get("msgid")
            or key.get("id")
            or data.get("id")
            or ""
        )
        contact_ref = str(
            participant.get("id")
            or key.get("participant")
            or chat.get("id")
            or payload.get("contact")
            or ""
        )
        thread_ref = str(
            chat.get("id")
            or payload.get("chatId")
            or payload.get("thread")
            or key.get("remoteJid")
            or data.get("remoteJid")
            or ""
        )
        result = record_inbound_event(
            source_event_id=source_event_id,
            contact_ref=contact_ref,
            thread_ref=thread_ref,
            payload=payload,
            status="received",
        )
        # Stage raw refs temporarily for registration use. For group messages,
        # expose both actionable targets: the group and the participant/contact.
        # Provider echoes from the connected account (fromme/key.fromMe) are not
        # external leads: keep the group registration item if useful, but never
        # offer the operator's own number as a contact to register.
        from_self = _payload_from_self(payload)
        registration_msg_type = _registration_message_type(payload, data)
        if result.get("ok") and not result.get("deduped") and registration_msg_type != "system":
            try:
                group_hint, contact_hint = _registration_hints(payload, data, chat, participant)
                if thread_ref and thread_ref != contact_ref:
                    stage_raw_ref(
                        contact_ref=contact_ref,
                        thread_ref=thread_ref,
                        display_name=group_hint.get("group_name", ""),
                        kind="group",
                        safe_hint=group_hint,
                    )
                    if contact_ref and not from_self:
                        stage_raw_ref(
                            contact_ref=contact_ref,
                            thread_ref=thread_ref,
                            display_name=contact_hint.get("participant_name", ""),
                            kind="contact",
                            safe_hint=contact_hint,
                        )
                elif not from_self:
                    stage_raw_ref(
                        contact_ref=contact_ref or thread_ref,
                        thread_ref=thread_ref or contact_ref,
                        display_name=contact_hint.get("participant_name", ""),
                        kind="contact",
                        safe_hint=contact_hint,
                    )
            except Exception:
                pass  # staging is best-effort; never fail ingest
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)[:200]})
    return _json(result)


def wpp_register_alias(
    nome: str,
    ref: str = "",
    tipo: str = "contact",
    policy_group: str = "manual",
    allow_send: bool = False,
    staging_id: str = "",
) -> str:
    """Register a contact or group alias in the local allowlist cache.

    If ``ref`` is empty, uses the most recently received inbound's raw ref.
    Use ``wpp_register_staging_status`` to see what's available for registration.

    Fail-closed: never sends, never exposes raw refs in output.
    """
    init_db()
    if not nome.strip():
        return _json({"ok": False, "error": "nome_required"})
    ref_raw = str(ref or "").strip()
    tipo_norm = str(tipo or "contact").strip().lower()
    if tipo_norm not in ("contact", "group"):
        return _json({"ok": False, "error": "tipo_invalid"})

    # Resolve raw ref: explicit > selected staging > latest staging > error
    if not ref_raw:
        consumed = consume_latest_raw_ref(kind=tipo_norm, staging_id=str(staging_id or "").strip())
        if not consumed:
            hint = "Item de staging não encontrado/expirado. Use /fila e escolha um item válido." if staging_id else "Informe 'ref' como o número/ID do WhatsApp, ou aguarde um inbound novo."
            return _json({
                "ok": False,
                "error": "ref_required",
                "hint": hint,
            })
        ref_raw = consumed

    policy_group = str(policy_group or "manual").strip().lower()
    if tipo_norm == "contact":
        result = register_contact_local(
            alias=nome.strip(),
            raw_ref=ref_raw,
            policy_group=policy_group,
            allow_send=bool(allow_send),
        )
    else:
        result = register_group_local(
            alias=nome.strip(),
            raw_ref=ref_raw,
            policy_group=policy_group,
            allow_send=bool(allow_send),
        )
    if not result.get("ok"):
        return _json(result)
    return _json({
        "ok": True,
        "tipo": tipo_norm,
        "alias": nome.strip(),
        "policy_group": policy_group,
        "allow_send": bool(allow_send),
        "contact_id": result.get("contact_id", ""),
        "display_name": result.get("display_name") or result.get("name", ""),
    })


def wpp_register_staging_status() -> str:
    """Show what inbound refs are staged and ready for registration.

    Returns sanitized info only (no raw WhatsApp refs).
    Use this before 'ref_from_staging' to see what's available.
    """
    try:
        staged = peek_staging()
        diagnostics = registration_staging_diagnostics()
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)[:200]})
    hint = "Use wpp_register_alias(nome='...', ref='...') com o número, ou wpp_register_alias(nome='...') após um inbound novo."
    if not staged and diagnostics.get("empty_reason") == "no_active_staging_or_expired":
        hint = "Nenhum inbound ativo em staging. Envie uma nova mensagem WhatsApp do contato/grupo e cadastre dentro da janela TTL."
    return _json({
        "ok": True,
        "staged": staged,
        "staged_count": len(staged),
        "staging_ttl_seconds": diagnostics.get("staging_ttl_seconds"),
        "inbound_count": diagnostics.get("inbound_count"),
        "latest_inbound_created_at": diagnostics.get("latest_inbound_created_at"),
        "empty_reason": diagnostics.get("empty_reason"),
        "hint": hint,
    })


def wpp_register_group(
    nome: str,
    ref: str = "",
    policy_group: str = "manual",
    allow_send: bool = False,
    staging_id: str = "",
) -> str:
    """Register a group alias in the local allowlist cache.  Shorthand for ``wpp_register_alias`` with ``tipo=group``.

    Fail-closed: never sends, never exposes raw refs in output.
    """
    return wpp_register_alias(
        nome=nome,
        ref=ref,
        tipo="group",
        policy_group=policy_group,
        allow_send=allow_send,
        staging_id=staging_id,
    )


def check_whatsapp_ops_requirements() -> bool:
    return True


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------

registry.register(
    name="wpp_resolve_contact",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_resolve_contact",
        "Resolve a WhatsApp contact by name or number without sending messages.",
        {"nome_ou_numero": {"type": "string"}},
        ["nome_ou_numero"],
    ),
    handler=lambda args, **kw: wpp_resolve_contact(args.get("nome_ou_numero", "")),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_list_contacts",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_list_contacts",
        "List known WhatsApp contacts in sanitized form.",
        {"filtro": {"type": "string"}},
        [],
    ),
    handler=lambda args, **kw: wpp_list_contacts(args.get("filtro", "")),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_create_draft",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_create_draft",
        "Create a WhatsApp draft in the local SQLite store. This never sends.",
        {
            "targets": {"type": "array", "items": {"type": "object"}},
            "message": {"type": "string"},
            "send_at": {"type": "string"},
        },
        ["targets", "message"],
    ),
    handler=lambda args, **kw: wpp_create_draft(
        targets=args.get("targets", []),
        message=args.get("message", ""),
        send_at=args.get("send_at"),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_request_approval",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_request_approval",
        "Create a pending human approval request for a draft. Does not expose approval tokens.",
        {"draft_id": {"type": "string"}},
        ["draft_id"],
    ),
    handler=lambda args, **kw: wpp_request_approval(args.get("draft_id", "")),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_resolve_approval",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_resolve_approval",
        "Resolve a WhatsApp draft approval as approved or denied from a trusted human approval context. This does not send.",
        {
            "approval_id": {"type": "string"},
            "decision": {"type": "string", "enum": ["approved", "denied"]},
            "approver_ref": {"type": "string"},
        },
        ["approval_id", "decision"],
    ),
    handler=lambda args, **kw: wpp_resolve_approval(
        approval_id=args.get("approval_id", ""),
        decision=args.get("decision", ""),
        approver_ref=args.get("approver_ref", ""),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_schedule_draft",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_schedule_draft",
        "Schedule a draft without sending it.",
        {"draft_id": {"type": "string"}, "send_at": {"type": "string"}},
        ["draft_id", "send_at"],
    ),
    handler=lambda args, **kw: wpp_schedule_draft(
        args.get("draft_id", ""), args.get("send_at", "")
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_send_approved",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_send_approved",
        "Attempt to send a human-approved WhatsApp draft. Fails closed unless all guardrails pass.",
        {"draft_id": {"type": "string"}},
        ["draft_id"],
    ),
    handler=lambda args, **kw: wpp_send_approved(
        draft_id=args.get("draft_id", ""),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_cancel",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_cancel",
        "Cancel a WhatsApp draft.",
        {"draft_id": {"type": "string"}},
        ["draft_id"],
    ),
    handler=lambda args, **kw: wpp_cancel(args.get("draft_id", "")),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_status",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_status",
        "Return sanitized draft status.",
        {"draft_id": {"type": "string"}},
        ["draft_id"],
    ),
    handler=lambda args, **kw: wpp_status(args.get("draft_id", "")),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_sync_allowlist",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_sync_allowlist",
        "Sync Infisical-rendered WhatsApp allowlist env into the sanitized local runtime cache. This never sends.",
        {},
        [],
    ),
    handler=lambda args, **kw: wpp_sync_allowlist(),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_import_contact_list",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_import_contact_list",
        "Import a commercial contact segment into the local sanitized WhatsApp Ops cache. Defaults allow_send=false and never sends.",
        {
            "list_name": {"type": "string"},
            "contacts": {"type": "array", "items": {"type": "object"}},
            "allow_send": {"type": "boolean"},
            "policy_group": {"type": "string"},
        },
        ["list_name", "contacts"],
    ),
    handler=lambda args, **kw: wpp_import_contact_list(
        list_name=args.get("list_name", ""),
        contacts=args.get("contacts", []),
        allow_send=bool(args.get("allow_send", False)),
        policy_group=args.get("policy_group", "lead"),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_list_contact_segments",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_list_contact_segments",
        "List sanitized commercial contact segments imported into WhatsApp Ops. Never sends.",
        {"limit": {"type": "integer"}},
        [],
    ),
    handler=lambda args, **kw: wpp_list_contact_segments(limit=args.get("limit", 50)),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_list_contact_segment_members",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_list_contact_segment_members",
        "List sanitized members of a commercial contact segment. Never sends and never exposes raw refs.",
        {"list_ref": {"type": "string"}, "limit": {"type": "integer"}},
        ["list_ref"],
    ),
    handler=lambda args, **kw: wpp_list_contact_segment_members(
        list_ref=args.get("list_ref", ""),
        limit=args.get("limit", 50),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_inbound_lookup",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_inbound_lookup",
        "Lookup sanitized inbound WhatsApp events.",
        {"thread": {"type": "string"}, "contact": {"type": "string"}, "limit": {"type": "integer"}},
        [],
    ),
    handler=lambda args, **kw: wpp_inbound_lookup(
        thread=args.get("thread", ""), contact=args.get("contact", ""), limit=args.get("limit", 20)
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_thread_context",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_thread_context",
        "Return a bounded sanitized local WhatsApp thread/context summary in summary/operator/debug mode. This never sends or fetches provider history.",
        {
            "thread": {"type": "string"},
            "contact": {"type": "string"},
            "limit": {"type": "integer"},
            "mode": {"type": "string", "enum": ["summary", "operator", "debug"]},
            "max_text_chars": {"type": "integer"},
        },
        [],
    ),
    handler=lambda args, **kw: wpp_thread_context(
        thread=args.get("thread", ""),
        contact=args.get("contact", ""),
        limit=args.get("limit", 20),
        mode=args.get("mode", "summary"),
        max_text_chars=args.get("max_text_chars", 160),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_conversation_summary",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_conversation_summary",
        "Return a deterministic read-only local WhatsApp conversation summary from sanitized inbound_events. Never sends, never creates drafts/approvals/outbox, never calls an LLM, never fetches provider history, and never persists summaries.",
        {
            "thread": {"type": "string"},
            "contact": {"type": "string"},
            "limit": {"type": "integer"},
            "mode": {"type": "string", "enum": ["stats", "brief", "timeline", "evidence"]},
            "max_text_chars": {"type": "integer"},
            "include_evidence": {"type": "boolean"},
        },
        [],
    ),
    handler=lambda args, **kw: wpp_conversation_summary(
        thread=args.get("thread", ""),
        contact=args.get("contact", ""),
        limit=args.get("limit", 50),
        mode=args.get("mode", "brief"),
        max_text_chars=args.get("max_text_chars", 160),
        include_evidence=bool(args.get("include_evidence", False)),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_transcribe_media",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_transcribe_media",
        "Fail-closed phase-1 transcription request for a sanitized inbound_* media event. Local status only: never downloads media, never calls STT/cloud/LLM, never sends, and never fetches provider history.",
        {
            "event_id": {"type": "string"},
            "mode": {"type": "string", "enum": ["on_request"]},
            "language": {"type": "string"},
            "provider": {"type": "string", "enum": ["disabled"]},
            "persist_status": {"type": "boolean"},
        },
        ["event_id"],
    ),
    handler=lambda args, **kw: wpp_transcribe_media(
        event_id=args.get("event_id", ""),
        mode=args.get("mode", "on_request"),
        language=args.get("language", ""),
        provider=args.get("provider", "disabled"),
        persist_status=bool(args.get("persist_status", True)),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_media_transcription_status",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_media_transcription_status",
        "Read sanitized local fail-closed media transcription status rows. Never downloads media, never calls STT/cloud/LLM, never sends, and never fetches provider history.",
        {"event_id": {"type": "string"}, "limit": {"type": "integer"}},
        [],
    ),
    handler=lambda args, **kw: wpp_media_transcription_status(
        event_id=args.get("event_id", ""), limit=args.get("limit", 20)
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_ingest_inbound_event",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_ingest_inbound_event",
        "Ingest a raw QuePasa inbound webhook payload into the sanitized local store. This never sends.",
        {"payload": {"type": "object"}},
        ["payload"],
    ),
    handler=lambda args, **kw: wpp_ingest_inbound_event(args.get("payload", {})),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_cockpit_overview",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_cockpit_overview",
        "Return a sanitized WhatsApp Ops cockpit overview: counts, recent inbound, drafts, approvals, and fail-closed flags. This never sends.",
        {"limit": {"type": "integer"}},
        [],
    ),
    handler=lambda args, **kw: wpp_cockpit_overview(limit=args.get("limit", 10)),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_actionable_queue",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_actionable_queue",
        "Return a sanitized read-only WhatsApp Ops operator queue combining pending approvals, registration staging, recent local inbound context, and safe next actions. Never sends, never approves, never creates drafts, and never fetches provider history.",
        {"limit": {"type": "integer"}},
        [],
    ),
    handler=lambda args, **kw: wpp_actionable_queue(limit=args.get("limit", 10)),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)


registry.register(
    name="wpp_ignore_staging_item",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_ignore_staging_item",
        "Ignore/remove a contact or group from the current registration queue without sending messages.",
        {
            "item": {"type": "integer", "description": "1-based item number from /fila", "default": 0},
            "staging_id": {"type": "string", "description": "Optional explicit staging id"},
        },
        [],
    ),
    handler=lambda args, **kw: wpp_ignore_staging_item(
        item=args.get("item", 0),
        staging_id=args.get("staging_id", ""),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_register_alias",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_register_alias",
        "Register a contact or group alias in the local allowlist cache by name. "
        "If 'ref' is empty, uses the most recently received inbound's raw ref. "
        "Use wpp_register_staging_status to see available inbounds. Never sends.",
        {
            "nome": {"type": "string"},
            "ref": {"type": "string"},
            "tipo": {"type": "string", "enum": ["contact", "group"]},
            "policy_group": {"type": "string"},
            "allow_send": {"type": "boolean"},
            "staging_id": {"type": "string"},
        },
        ["nome"],
    ),
    handler=lambda args, **kw: wpp_register_alias(
        nome=args.get("nome", ""),
        ref=args.get("ref", ""),
        tipo=args.get("tipo", "contact"),
        policy_group=args.get("policy_group", "manual"),
        allow_send=bool(args.get("allow_send", False)),
        staging_id=args.get("staging_id", ""),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_register_staging_status",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_register_staging_status",
        "Show what inbound refs are staged and available for registration. Sanitized output.",
        {},
        [],
    ),
    handler=lambda args, **kw: wpp_register_staging_status(),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)

registry.register(
    name="wpp_register_group",
    toolset=TOOLSET,
    schema=_schema(
        "wpp_register_group",
        "Register a group alias in the local allowlist cache. "
        "If 'ref' is empty, uses the most recently received inbound group ref. Never sends.",
        {
            "nome": {"type": "string"},
            "ref": {"type": "string"},
            "policy_group": {"type": "string"},
            "allow_send": {"type": "boolean"},
            "staging_id": {"type": "string"},
        },
        ["nome"],
    ),
    handler=lambda args, **kw: wpp_register_group(
        nome=args.get("nome", ""),
        ref=args.get("ref", ""),
        policy_group=args.get("policy_group", "manual"),
        allow_send=bool(args.get("allow_send", False)),
        staging_id=args.get("staging_id", ""),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)
