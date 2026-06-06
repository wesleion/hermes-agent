"""Hermes WhatsApp Ops tools backed by a local SQLite store.

Transport is fail-closed by default.  ``wpp_send_approved`` refuses unless all
code-level guardrails pass; prompt instructions are never the send gate.
"""

from __future__ import annotations

import json
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
    get_valid_approval,
    idempotency_used,
    init_db,
    list_contacts,
    mark_outbox_blocked,
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
        "approval": {"required": True, "timeout_minutes": 60},
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
    approval_token: str,
    config: dict[str, Any] | None = None,
    send_client: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> str:
    """Send an approved draft only if deterministic guardrails allow it."""
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
        if draft and idempotency_key:
            mark_outbox_blocked(draft_id, idempotency_key, ",".join(result.reasons))
        return _json({"ok": False, "draft_id": draft_id, "reasons": result.reasons})

    client = send_client or send_via_quepasa
    payload = {
        "draft_id": draft_id,
        "targets": policy_draft["targets"] if policy_draft else [],
        "message": draft["message"] if draft else "",
        "idempotency_key": idempotency_key,
    }
    send_result = client(payload, cfg)
    return _json({"ok": bool(send_result.get("ok")), "draft_id": draft_id, "send_result": send_result})


def wpp_request_approval(draft_id: str) -> str:
    cfg = _default_config()
    timeout = int((cfg.get("approval") or {}).get("timeout_minutes", 60))
    try:
        approval = create_approval(draft_id, timeout_minutes=timeout)
        update_draft_status(draft_id, "approved")
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)[:200]})
    return _json({"ok": True, "draft_id": draft_id, **approval})


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
    return _json({
        "ok": True,
        "draft_id": draft_id,
        "status": draft.get("status"),
        "send_at": draft.get("send_at"),
        "created_at": draft.get("created_at"),
    })


def wpp_resolve_contact(nome_ou_numero: str) -> str:
    return _json(resolve_contact(nome_ou_numero))


def wpp_list_contacts(filtro: str = "") -> str:
    return _json({"ok": True, "filter": str(filtro)[:80], "contacts": list_contacts(filtro)})


def wpp_inbound_lookup(thread: str = "", contact: str = "") -> str:
    return _json({"ok": True, "thread": str(thread)[:80], "contact": str(contact)[:80], "events": []})


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
        "Create a one-time approval token for a draft.",
        {"draft_id": {"type": "string"}},
        ["draft_id"],
    ),
    handler=lambda args, **kw: wpp_request_approval(args.get("draft_id", "")),
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
        "Attempt to send an approved WhatsApp draft. Fails closed unless all guardrails pass.",
        {"draft_id": {"type": "string"}, "approval_token": {"type": "string"}},
        ["draft_id", "approval_token"],
    ),
    handler=lambda args, **kw: wpp_send_approved(
        draft_id=args.get("draft_id", ""),
        approval_token=args.get("approval_token", ""),
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
        {"thread": {"type": "string"}, "contact": {"type": "string"}},
        [],
    ),
    handler=lambda args, **kw: wpp_inbound_lookup(
        thread=args.get("thread", ""), contact=args.get("contact", "")
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)
