"""Deterministic, fail-closed autonomy policy for WhatsApp Ops.

This module is deliberately pure: it does not dispatch tools, write state, send
messages, or mutate configuration.  Callers must declare an action and receive a
bounded decision before entering an autonomous code path.  Operator-directed
paths remain separate and external effects retain their existing hard gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: Any, default: float = 0.75) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
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
    requested_items: int = 1,
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
