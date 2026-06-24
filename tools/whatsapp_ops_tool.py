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
    consume_latest_raw_ref,
    create_approval,
    create_draft,
    get_cockpit_overview,
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


def wpp_sync_allowlist() -> str:
    return _json(sync_allowlist_from_env())


def wpp_inbound_lookup(thread: str = "", contact: str = "", limit: int = 20) -> str:
    events = lookup_inbound_events(thread=str(thread or ""), contact=str(contact or ""), limit=limit)
    return _json({"ok": True, "thread_filter_set": bool(thread), "contact_filter_set": bool(contact), "events": events})


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
        # Stage raw refs temporarily for registration use.
        if result.get("ok"):
            try:
                stage_raw_ref(
                    contact_ref=contact_ref,
                    thread_ref=thread_ref,
                    display_name=data.get("pushName", "") or payload.get("senderName", "") or "",
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

    # Resolve raw ref: explicit > staging consume > error
    if not ref_raw:
        consumed = consume_latest_raw_ref(kind=tipo_norm)
        if not consumed:
            return _json({
                "ok": False,
                "error": "ref_required",
                "hint": "Informe 'ref' como o número/ID do WhatsApp, ou aguarde um inbound novo.",
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
        },
        ["nome"],
    ),
    handler=lambda args, **kw: wpp_register_alias(
        nome=args.get("nome", ""),
        ref=args.get("ref", ""),
        tipo=args.get("tipo", "contact"),
        policy_group=args.get("policy_group", "manual"),
        allow_send=bool(args.get("allow_send", False)),
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
        },
        ["nome"],
    ),
    handler=lambda args, **kw: wpp_register_group(
        nome=args.get("nome", ""),
        ref=args.get("ref", ""),
        policy_group=args.get("policy_group", "manual"),
        allow_send=bool(args.get("allow_send", False)),
    ),
    check_fn=check_whatsapp_ops_requirements,
    emoji="📲",
)
