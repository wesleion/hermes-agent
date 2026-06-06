"""Deterministic send guardrails for WhatsApp Ops."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    reasons: list[str]


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _is_expired(value: str | None) -> bool:
    if not value:
        return True
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


def evaluate_send_guardrails(
    *,
    config: dict[str, Any],
    draft: dict[str, Any] | None,
    approval: dict[str, Any] | None,
    idempotency_used: bool,
) -> GuardrailResult:
    reasons: list[str] = []
    config = config or {}

    if not _truthy(config.get("send_enabled", False)):
        reasons.append("send_disabled")
    if _truthy(config.get("kill_switch", False)):
        reasons.append("kill_switch_active")

    quepasa_raw = config.get("quepasa")
    quepasa = quepasa_raw if isinstance(quepasa_raw, dict) else {}
    if not _truthy(quepasa.get("send_enabled", False)):
        reasons.append("quepasa_send_disabled")

    if draft is None:
        reasons.append("draft_missing")
        return GuardrailResult(False, reasons)

    if draft.get("has_untrusted_media"):
        reasons.append("media_untrusted")

    targets = draft.get("targets") or []
    if not targets:
        reasons.append("payload_invalid")

    allowlists_raw = config.get("allowlists")
    allowlists = allowlists_raw if isinstance(allowlists_raw, dict) else {}
    allowed_contacts = set(allowlists.get("contacts") or [])
    allowed_groups = set(allowlists.get("groups") or [])

    for target in targets:
        if not isinstance(target, dict):
            reasons.append("payload_invalid")
            continue
        if target.get("ambiguous"):
            reasons.append("target_ambiguous")
            continue
        target_type = target.get("type")
        if target_type == "contact":
            if target.get("contact_id") not in allowed_contacts:
                reasons.append("target_not_whitelisted")
        elif target_type in {"group", "list"}:
            group_id = target.get("group_id") or target.get("list_id")
            if group_id not in allowed_groups:
                reasons.append("target_not_whitelisted")
        else:
            reasons.append("payload_invalid")

    approval_raw = config.get("approval")
    approval_cfg = approval_raw if isinstance(approval_raw, dict) else {}
    approval_required = approval_cfg.get("required", True)
    if approval_required and approval is None:
        reasons.append("approval_missing")
    elif approval is not None:
        if approval.get("status") != "approved" or not approval.get("token_valid", True):
            reasons.append("approval_invalid")
        if _is_expired(approval.get("expires_at")):
            reasons.append("approval_expired")
        if draft.get("message_hash") != approval.get("message_hash"):
            reasons.append("message_changed_after_approval")

    if idempotency_used:
        reasons.append("idempotency_duplicate")

    # Keep deterministic, stable output.
    deduped = list(dict.fromkeys(reasons))
    return GuardrailResult(not deduped, deduped)
