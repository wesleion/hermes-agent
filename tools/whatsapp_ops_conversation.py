"""Restricted Hermes generator for the opt-in friends pilot.

This module intentionally owns no transport.  It produces a validated immutable
message plan; the existing batch sender remains the only outbound boundary.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

_FORBIDDEN = (
    "http://",
    "https://",
    "preço",
    "price",
    "desconto",
    "discount",
    "garantia",
    "guarantee",
    "sla",
    "case study",
    "cases",
)
_ACTIONS = {"qualify", "present", "brief", "refer", "escalate", "stop"}


def _session_id(
    profile_id: str, grant_id: str, contact_id: str, channel_id: str
) -> str:
    value = "friends-v1|" + "|".join((profile_id, grant_id, contact_id, channel_id))
    return "friends_" + sha256(value.encode()).hexdigest()[:32]


def validate_friends_generation(value: Any) -> dict[str, Any] | None:
    """Validate model output; unknown commercial claims fail closed."""
    if not isinstance(value, dict) or set(value) != {
        "stage",
        "qualification",
        "action",
        "blocks",
        "next_step",
        "escalation",
    }:
        return None
    blocks, action = value.get("blocks"), value.get("action")
    if (
        action not in _ACTIONS
        or not isinstance(blocks, list)
        or not 1 <= len(blocks) <= 3
    ):
        return None
    if not all(
        isinstance(x, str) and x.strip() == x and 1 <= len(x) <= 4096 for x in blocks
    ):
        return None
    rendered = " ".join(blocks).casefold()
    if any(token in rendered for token in _FORBIDDEN):
        return None
    if (
        not isinstance(value["qualification"], dict)
        or not isinstance(value["next_step"], str)
        or not isinstance(value["escalation"], bool)
    ):
        return None
    # External actions are never a generation result; a human/operator handles
    # escalation and the caller sends only frozen text blocks.
    if action in {"escalate", "stop"} and blocks:
        return None
    return value


class FriendsHermesGenerator:
    """Concrete dedicated AIAgent adapter with a zero-tool, private session."""

    def __init__(
        self,
        *,
        profile_id: str = "default",
        agent_factory: Any = None,
        config_loader: Any = None,
        runtime_resolver: Any = None,
    ) -> None:
        self.profile_id = profile_id
        self._agent_factory = agent_factory
        self._config_loader = config_loader
        self._runtime_resolver = runtime_resolver

    def _agent(self, grant_id: str, contact_id: str, channel_id: str) -> Any:
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent

        cfg = (self._config_loader or load_config)()
        model_cfg = cfg.get("model") if isinstance(cfg, dict) else {}
        model_cfg = model_cfg if isinstance(model_cfg, dict) else {}
        requested = str(model_cfg.get("provider") or "").strip() or None
        runtime = (self._runtime_resolver or resolve_runtime_provider)(
            requested=requested
        )
        kwargs = {
            "base_url": runtime.get("base_url"),
            "api_key": runtime.get("api_key"),
            "provider": runtime.get("provider"),
            "api_mode": runtime.get("api_mode"),
            "model": str(model_cfg.get("default") or ""),
            "session_id": _session_id(
                self.profile_id, grant_id, contact_id, channel_id
            ),
            "enabled_toolsets": [],
            "disabled_toolsets": ["memory", "terminal", "filesystem", "browser"],
            "skip_memory": True,
            "skip_context_files": True,
            "load_soul_identity": False,
            "max_iterations": 6,
            "save_trajectories": False,
            "fallback_model": None,
            "quiet_mode": True,
            "platform": "whatsapp_friends_private",
        }
        agent = (self._agent_factory or AIAgent)(**kwargs)
        # AIAgent construction may merge defaults; inspect the resulting object
        # before any model call so a future core change cannot silently add tools.
        tools = getattr(agent, "tools", [])
        if tools or not getattr(agent, "skip_memory", True):
            raise RuntimeError("friends_agent_isolation_failed")
        return agent

    def generate(
        self,
        *,
        grant_id: str,
        contact_id: str,
        channel_id: str,
        messages: list[dict[str, str]],
        offer: dict[str, Any],
        highwatermark: str,
    ) -> dict[str, Any] | None:
        agent = self._agent(grant_id, contact_id, channel_id)
        prompt = {
            "task": "Responda somente JSON. Conversa comercial consentida sobre infraestrutura agêntica.",
            "constraints": "Nunca invente preço, prazo, desconto, garantia, SLA, cases ou links. Sem ações externas. Se faltar fato, escalone.",
            "schema": {
                "stage": "string",
                "qualification": {
                    "problem": "string",
                    "process": "string",
                    "interest": "string",
                    "investment": "string",
                    "referral": "string",
                },
                "action": "qualify|present|brief|refer|escalate|stop",
                "blocks": ["1-3 textos"],
                "next_step": "string",
                "escalation": "boolean",
            },
            "offer": offer,
            "highwatermark": highwatermark,
            "messages": messages[-12:],
        }
        result = agent.run_conversation(
            json.dumps(prompt, ensure_ascii=False), system_message=""
        )
        raw = result.get("final_response") if isinstance(result, dict) else result
        try:
            return validate_friends_generation(json.loads(str(raw)))
        except (TypeError, ValueError):
            return None
