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
from tools.whatsapp_ops_quepasa import send_via_quepasa

try:  # config loading is best-effort; tool remains fail-closed if unavailable
    from hermes_cli.config import load_config
except Exception:  # pragma: no cover - defensive for stripped runtimes
    load_config = None
from tools.whatsapp_ops_store import (
    create_approval,
    create_draft,
    get_draft,
    get_latest_approval,
    get_valid_approval,
    idempotency_used,
    init_db,
    list_contacts,
    lookup_inbound_events,
    mark_outbox_blocked,
    mark_outbox_result,
    reserve_outbox_send,
    resolve_approval,
    resolve_contact,
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
        targets = json.loads(draft["targets_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        targets = []
    return {
        "id": draft.get("id"),
        "status": draft.get("status"),
        "targets": targets,
        "message": draft.get("message", ""),
        "message_hash": draft.get("message_hash", ""),
        "idempotency_key": draft.get("idempotency_key", ""),
        "has_untrusted_media": False,
    }


def _approval_for_policy(approval: dict[str, Any] | None) -> dict[str, Any] | None:
    if approval is None:
        return None
    return {
        "status": approval.get("status"),
        "token_valid": True,
        "expires_at": approval.get("expires_at"),
        "message_hash": approval.get("message_hash"),
    }


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
    text = (
        "📲 <b>WhatsApp Ops approval</b>\n\n"
        f"Draft: <code>{draft_id}</code>\n"
        f"Approval: <code>{approval_id}</code>\n\n"
        f"<pre>{text_preview}</pre>\n\n"
        "Aprovar só marca o draft como approved. O envio continua separado."
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
    cfg = config if config is not None else _runtime_config()
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

    if idempotency_key and not reserve_outbox_send(draft_id, idempotency_key):
        return _json({"ok": False, "draft_id": draft_id, "reasons": ["idempotency_duplicate"]})

    client = send_client or send_via_quepasa
    payload = {
        "draft_id": draft_id,
        "targets": policy_draft["targets"] if policy_draft else [],
        "message": draft["message"] if draft else "",
        "idempotency_key": idempotency_key,
    }
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


def wpp_inbound_lookup(thread: str = "", contact: str = "", limit: int = 20) -> str:
    events = lookup_inbound_events(thread=str(thread or ""), contact=str(contact or ""), limit=limit)
    return _json({"ok": True, "thread_filter_set": bool(thread), "contact_filter_set": bool(contact), "events": events})


def wpp_ingest_inbound_event(payload: dict[str, Any]) -> str:
    """Ingest a raw QuePasa inbound webhook payload into the sanitized local store."""
    try:
        from tools.whatsapp_ops_store import record_inbound_event
        source_event_id = str(payload.get("id") or payload.get("message_id") or payload.get("msgid") or "")
        chat_obj = payload.get("chat")
        chat: dict[str, Any] = chat_obj if isinstance(chat_obj, dict) else {}
        participant_obj = payload.get("participant")
        participant: dict[str, Any] = participant_obj if isinstance(participant_obj, dict) else {}
        contact_ref = str(participant.get("id") or chat.get("id") or payload.get("contact") or "")
        thread_ref = str(chat.get("id") or payload.get("chatId") or payload.get("thread") or "")
        result = record_inbound_event(
            source_event_id=source_event_id,
            contact_ref=contact_ref,
            thread_ref=thread_ref,
            payload=payload,
            status="received",
        )
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)[:200]})
    return _json(result)


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
