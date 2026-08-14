"""Deterministic, fail-closed autonomy policy for WhatsApp Ops.

This module is deliberately pure: it does not dispatch tools, write state, send
messages, or mutate configuration.  Callers must declare an action and receive a
bounded decision before entering an autonomous code path.  Operator-directed
paths remain separate and external effects retain their existing hard gates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from tools.whatsapp_ops_sales import is_opaque_ref

VALID_AUTONOMY_MODES = frozenset({"off", "assist", "safe_auto"})

# Safe actions are local-only.  Adding an external action here is insufficient to
# enable it: HARD_DENIED_ACTIONS always wins and is tested as an invariant.
ACTION_CATALOG: dict[str, dict[str, str]] = {
    "conversation.read_local": {"area": "local_context", "effect": "read_only"},
    "queue.inspect": {"area": "local_context", "effect": "read_only"},
    "opportunity.score": {"area": "commercial_discovery", "effect": "read_only"},
    "draft.preview": {"area": "commercial_drafts", "effect": "read_only"},
    "draft.create_local": {"area": "commercial_drafts", "effect": "local_write"},
    "approval.request_local": {"area": "commercial_drafts", "effect": "local_write"},
}

HARD_DENIED_ACTIONS = frozenset(
    {
        "approval.resolve",
        "crm.append",
        "cron.activate",
        "group.create",
        "inbound.ingest",
        "provider_history.pull",
        "runtime.promote",
        "runtime.restart",
        "whatsapp.send",
    }
)


@dataclass(frozen=True)
class AutonomyDecision:
    allowed: bool
    action: str
    area: str
    mode: str
    effect: str
    reasons: tuple[str, ...]
    max_items: int
    effective_items: int
    requires_human_gate: bool
    min_confidence: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "area": self.area,
            "mode": self.mode,
            "effect": self.effect,
            "reasons": list(self.reasons),
            "max_items": self.max_items,
            "effective_items": self.effective_items,
            "requires_human_gate": self.requires_human_gate,
            "min_confidence": self.min_confidence,
        }


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _bounded_int(value: Any, default: int = 1, minimum: int = 1, maximum: int = 20) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: Any, default: float = 0.75) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(0.0, min(parsed, 1.0))


def _policy_config(config: dict[str, Any] | None) -> tuple[dict[str, Any], str, list[str]]:
    root = config if isinstance(config, dict) else {}
    raw = root.get("autonomy")
    policy = raw if isinstance(raw, dict) else {}
    raw_mode = str(policy.get("mode") or "off").strip().lower()
    reasons: list[str] = []
    if raw_mode not in VALID_AUTONOMY_MODES:
        reasons.append("autonomy_mode_invalid")
        mode = "off"
    else:
        mode = raw_mode
    return policy, mode, reasons


def evaluate_autonomy(
    config: dict[str, Any] | None,
    *,
    action: str,
    area: str = "",
    requested_items: Any = 1,
) -> AutonomyDecision:
    """Return a deterministic decision for one explicitly named action.

    The function is a policy aid, not an authentication boundary.  External
    actions are hard-denied here and must continue through their dedicated human,
    allowlist, provider, and runtime gates.
    """

    root = config if isinstance(config, dict) else {}
    policy, mode, reasons = _policy_config(root)
    normalized_action = str(action or "").strip().lower()
    requested_area = str(area or "").strip().lower()
    spec = ACTION_CATALOG.get(normalized_action)
    expected_area = str((spec or {}).get("area") or "")
    effect = str((spec or {}).get("effect") or "external")
    allowed_actions = _string_set(policy.get("allowed_actions"))
    allowed_areas = _string_set(policy.get("allowed_areas"))
    max_items = _bounded_int(policy.get("max_items_per_run"), default=1)
    effective_items = min(_bounded_int(requested_items, default=1), max_items)
    min_confidence = _bounded_float(policy.get("min_confidence"), default=0.75)

    if bool(root.get("kill_switch", False)):
        reasons.append("kill_switch_active")
    if mode == "off":
        reasons.append("autonomy_off")

    if normalized_action in HARD_DENIED_ACTIONS:
        reasons.append("action_hard_denied")
    elif spec is None:
        reasons.append("action_unknown")
    else:
        if requested_area and requested_area != expected_area:
            reasons.append("area_mismatch")
        if normalized_action not in allowed_actions:
            reasons.append("action_not_allowlisted")
        if expected_area not in allowed_areas:
            reasons.append("area_not_allowlisted")
        if effect == "local_write" and mode != "safe_auto":
            reasons.append("safe_auto_required")

    deduped = tuple(dict.fromkeys(reasons))
    requires_human_gate = normalized_action in HARD_DENIED_ACTIONS or effect != "read_only"
    return AutonomyDecision(
        allowed=not deduped,
        action=normalized_action,
        area=expected_area or requested_area,
        mode=mode,
        effect=effect,
        reasons=deduped,
        max_items=max_items,
        effective_items=effective_items,
        requires_human_gate=requires_human_gate,
        min_confidence=min_confidence,
    )


def autonomy_status(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return a sanitized policy snapshot with no secrets or runtime identifiers."""

    root = config if isinstance(config, dict) else {}
    policy, mode, mode_reasons = _policy_config(root)
    allowed_actions = sorted(_string_set(policy.get("allowed_actions")))
    allowed_areas = sorted(_string_set(policy.get("allowed_areas")))
    return {
        "ok": not mode_reasons,
        "mode": mode,
        "enabled": mode != "off" and not bool(root.get("kill_switch", False)),
        "reasons": mode_reasons + (["kill_switch_active"] if bool(root.get("kill_switch", False)) else []),
        "allowed_actions": allowed_actions,
        "allowed_areas": allowed_areas,
        "max_items_per_run": _bounded_int(policy.get("max_items_per_run"), default=1),
        "min_confidence": _bounded_float(policy.get("min_confidence"), default=0.75),
        "hard_denied_actions": sorted(HARD_DENIED_ACTIONS),
        "external_effects_allowed": False,
        "approval_resolution_allowed": False,
        "deny_by_default": True,
    }


# Commercial lifecycle persistence is intentionally deferred to the campaign
# slice.  This pure transition seam avoids a speculative third SQLite table.
SALES_STAGES = frozenset({"BDR", "SDR", "Closer", "Support"})
SALES_STAGE_TRANSITIONS = {
    ("BDR", "lead_qualified"): "SDR",
    ("SDR", "opportunity_qualified"): "Closer",
    ("Closer", "deal_won"): "Support",
}
_SALES_EVENTS = frozenset(event for _, event in SALES_STAGE_TRANSITIONS)


@dataclass(frozen=True)
class SalesTransition:
    transitioned: bool
    code: str
    project_ref: str
    lead_ref: str
    from_stage: str
    to_stage: str
    event: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "transitioned": self.transitioned,
            "code": self.code,
            "project_ref": self.project_ref,
            "lead_ref": self.lead_ref,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "event": self.event,
        }


def transition_sales_stage(
    *,
    project_ref: object,
    lead_ref: object,
    stage: object,
    event: object,
) -> SalesTransition:
    """Apply one deterministic BDR→SDR→Closer→Support lifecycle event.

    Only opaque project/lead refs and closed enum values can appear in output.
    Unknown or out-of-order input fails closed without returning text payloads.
    """

    if not is_opaque_ref(project_ref) or not is_opaque_ref(lead_ref):
        return SalesTransition(False, "sales_ref_invalid", "", "", "", "", "")

    project = str(project_ref)
    lead = str(lead_ref)
    if type(stage) is not str or stage not in SALES_STAGES:
        return SalesTransition(
            False,
            "sales_stage_invalid",
            project,
            lead,
            "",
            "",
            event if type(event) is str and event in _SALES_EVENTS else "",
        )
    if type(event) is not str or event not in _SALES_EVENTS:
        return SalesTransition(
            False,
            "sales_event_invalid",
            project,
            lead,
            stage,
            stage,
            "",
        )

    next_stage = SALES_STAGE_TRANSITIONS.get((stage, event))
    if next_stage is None:
        return SalesTransition(
            False,
            "sales_transition_denied",
            project,
            lead,
            stage,
            stage,
            event,
        )
    return SalesTransition(
        True,
        "sales_stage_transitioned",
        project,
        lead,
        stage,
        next_stage,
        event,
    )
