"""Hermes WhatsApp Ops tools backed by a local SQLite store.

Transport is fail-closed by default.  ``wpp_send_approved`` refuses unless all
code-level guardrails pass; prompt instructions are never the send gate.
"""

from __future__ import annotations

import json
import os
import urllib.error
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
    get_cockpit_overview,
    get_conversation_summary,
    get_thread_context,
    registration_staging_diagnostics,
    get_draft,
    get_latest_approval,
    get_send_allowlist_ids,
    get_valid_approval,
    idempotency_used,
    init_db,
    list_contacts,
    lookup_inbound_events,
    mark_outbox_blocked,
    mark_outbox_result,
    peek_staging,
    register_contact_local,
    register_group_local,
    reserve_outbox_send,
    resolve_approval,
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
    return {
        "id": draft.get("id"),
        "status": draft.get("status"),
        "targets": targets,
        "message": draft.get("message", ""),
        "message_hash": draft.get("message_hash", ""),
        "idempotency_key": draft.get("idempotency_key", ""),
        "has_untrusted_media": False,
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


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _resolve_raw_contact_ref(query: str) -> str:
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
    for item in _raw_contact_entries():
        kind = str(item.get("kind") or "contact").strip().lower()
        if kind not in {"contact", "dm"}:
            continue
        raw_ref = str(item.get("target_ref") or item.get("ref") or item.get("contact_ref") or "").strip()
        if not raw_ref:
            continue
        candidates = {
            str(item.get("alias") or "").strip().casefold(),
            str(item.get("display_name") or "").strip().casefold(),
            raw_ref.casefold(),
        }
        raw_digits = _digits(raw_ref)
        if needle_norm in candidates or (needle_digits and needle_digits == raw_digits):
            if bool(item.get("allow_send", False)):
                matches.append(raw_ref)
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else ""


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


def _group_create_provider_payload(draft_id: str, draft: dict[str, Any] | None, idempotency_key: str) -> tuple[dict[str, Any] | None, str | None]:
    target = _extract_group_create_target(draft)
    if not target:
        return None, "payload_invalid"
    title = str(target.get("name") or target.get("title") or "").strip()
    members = target.get("member_aliases") or target.get("participants") or []
    if not title or not isinstance(members, list) or not members:
        return None, "payload_invalid"
    participants: list[str] = []
    for member in members:
        raw_ref = _resolve_raw_contact_ref(str(member or ""))
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
    is_group_create = False
    try:
        targets = json.loads(str((draft or {}).get("targets_json") or "[]"))
        is_group_create = any(isinstance(t, dict) and t.get("type") == "group_create" for t in targets)
    except Exception:
        is_group_create = False
    approval_note = (
        "Aprovar tenta executar a criação via QuePasa/direct, respeitando flags e allowlist."
        if is_group_create
        else "Aprovar só marca o draft como approved. O envio continua separado."
    )
    text = (
        "📲 <b>WhatsApp Ops approval</b>\n\n"
        f"Draft: <code>{draft_id}</code>\n"
        f"Approval: <code>{approval_id}</code>\n\n"
        f"<pre>{text_preview}</pre>\n\n"
        f"{approval_note}"
    )
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Aprovar", "callback_data": f"wpp:a:{approval_id}"},
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
    send_client: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> str:
    """Create a local WhatsApp draft. Never sends."""
    try:
        init_db()
        draft = create_draft(targets=targets, message=message, send_at=send_at)
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)[:200]})
    return _json({"ok": True, **draft})


def wpp_send_approved(
    draft_id: str,
    approval_token: str | None = None,
    config: dict[str, Any] | None = None,
    send_client: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
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
        payload, payload_error = _group_create_provider_payload(draft_id, draft, idempotency_key)
        if payload_error:
            return _json({"ok": False, "draft_id": draft_id, "reasons": [payload_error]})
        if send_client is None:
            from tools.whatsapp_ops_quepasa import create_group_via_quepasa

            client = create_group_via_quepasa
        else:
            client = send_client
    else:
        if send_client is None:
            from tools.whatsapp_ops_quepasa import send_via_quepasa

            client = send_via_quepasa
        else:
            client = send_client
        payload = {
            "draft_id": draft_id,
            "targets": policy_draft["targets"] if policy_draft else [],
            "message": draft["message"] if draft else "",
            "idempotency_key": idempotency_key,
        }

    if idempotency_key and not reserve_outbox_send(draft_id, idempotency_key):
        return _json({"ok": False, "draft_id": draft_id, "reasons": ["idempotency_duplicate"]})

    send_result = client(payload, cfg)
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
    return _json({
        "ok": True,
        "draft_id": draft_id,
        "status": draft.get("status"),
        "send_at": draft.get("send_at"),
        "created_at": draft.get("created_at"),
        "approval": approval_safe,
    })


def wpp_resolve_contact(nome_ou_numero: str) -> str:
    return _json(resolve_contact(nome_ou_numero))


def wpp_list_contacts(filtro: str = "") -> str:
    return _json({"ok": True, "filter": str(filtro)[:80], "contacts": list_contacts(filtro)})


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
        if result.get("ok") and not result.get("deduped"):
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
                    if contact_ref:
                        stage_raw_ref(
                            contact_ref=contact_ref,
                            thread_ref=thread_ref,
                            display_name=contact_hint.get("participant_name", ""),
                            kind="contact",
                            safe_hint=contact_hint,
                        )
                else:
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
