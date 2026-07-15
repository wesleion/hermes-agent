from __future__ import annotations

import json
import sqlite3

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _parse(value: str) -> dict:
    return json.loads(value)


def _autonomy_config(*, mode="safe_auto", max_items=2, actions=None):
    return {
        "autonomy": {
            "mode": mode,
            "allowed_areas": ["commercial_discovery", "commercial_drafts", "local_context"],
            "allowed_actions": actions
            if actions is not None
            else [
                "conversation.read_local",
                "queue.inspect",
                "opportunity.score",
                "draft.preview",
                "draft.create_local",
                "approval.request_local",
            ],
            "max_items_per_run": max_items,
            "min_confidence": 0.75,
        }
    }


def _candidates(count=3, confidence=0.82):
    return [
        {
            "candidate_id": f"LEAD-{idx}",
            "target": {"type": "contact", "contact_id": f"lead_{idx}"},
            "confidence": confidence,
            "priority": "high",
            "rationale": "follow-up seguro",
            "message": f"Oi! Podemos alinhar o próximo passo do projeto {idx}?",
        }
        for idx in range(1, count + 1)
    ]


def test_default_config_keeps_autonomy_assisted_and_external_actions_denied():
    from tools.whatsapp_ops_tool import _default_config

    config = _default_config()

    assert config["autonomy"]["mode"] == "assist"
    assert "opportunity.score" in config["autonomy"]["allowed_actions"]
    assert "draft.preview" in config["autonomy"]["allowed_actions"]
    assert "whatsapp.send" not in config["autonomy"]["allowed_actions"]
    assert "crm.append" not in config["autonomy"]["allowed_actions"]
    assert config["autonomy"]["max_items_per_run"] <= 3


def test_autonomy_status_tool_is_read_only_and_registered():
    from tools.registry import registry
    from tools.whatsapp_ops_tool import wpp_autonomy_status

    result = _parse(wpp_autonomy_status(config=_autonomy_config()))
    entry = registry._tools.get("wpp_autonomy_status")

    assert result["ok"] is True
    assert result["mode"] == "safe_auto"
    assert result["external_effects_allowed"] is False
    assert result["approval_resolution_allowed"] is False
    assert entry is not None
    assert entry.schema["parameters"]["properties"] == {}
    assert "read-only" in entry.schema["description"].lower()


def test_autonomous_score_is_allowlisted_and_policy_caps_items(tmp_path):
    from tools.whatsapp_ops_tool import wpp_opportunity_scores

    token = set_hermes_home_override(tmp_path)
    try:
        result = _parse(
            wpp_opportunity_scores(
                lead_inputs=_candidates(count=5),
                limit=50,
                execution_context="autonomous",
                config=_autonomy_config(max_items=2),
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    assert result["execution_context"] == "autonomous"
    assert result["autonomy"]["allowed"] is True
    assert result["limit"] == 2
    assert result["count"] == 2
    assert result["send_performed"] is False
    assert result["crm_write_performed"] is False


def test_autonomous_draft_create_is_blocked_in_assist_without_local_writes(tmp_path):
    from tools.whatsapp_ops_store import get_db_path, init_db
    from tools.whatsapp_ops_tool import wpp_proactive_draft_queue

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        db = get_db_path()
        with sqlite3.connect(db) as conn:
            before = conn.execute("SELECT count(*) FROM drafts").fetchone()[0]
        result = _parse(
            wpp_proactive_draft_queue(
                candidates=_candidates(count=1),
                mode="create",
                execution_context="autonomous",
                config=_autonomy_config(mode="assist"),
            )
        )
        with sqlite3.connect(db) as conn:
            after = conn.execute("SELECT count(*) FROM drafts").fetchone()[0]
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is False
    assert result["error"] == "autonomy_denied"
    assert "safe_auto_required" in result["autonomy"]["reasons"]
    assert result["drafts_created"] == 0
    assert result["send_performed"] is False
    assert before == after


def test_safe_auto_draft_create_enforces_policy_cap_threshold_and_local_approval(tmp_path):
    from tools.whatsapp_ops_store import get_db_path, init_db
    from tools.whatsapp_ops_tool import wpp_proactive_draft_queue

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        db = get_db_path()
        result = _parse(
            wpp_proactive_draft_queue(
                candidates=_candidates(count=4, confidence=0.80),
                mode="create",
                limit=5,
                min_confidence=0.1,
                create_approvals=True,
                execution_context="autonomous",
                config=_autonomy_config(max_items=2),
            )
        )
        with sqlite3.connect(db) as conn:
            draft_count = conn.execute("SELECT count(*) FROM drafts").fetchone()[0]
            approval_count = conn.execute("SELECT count(*) FROM approvals").fetchone()[0]
            outbox_count = conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    assert result["execution_context"] == "autonomous"
    assert result["autonomy"]["allowed"] is True
    assert result["limit"] == 2
    assert result["min_confidence"] == 0.75
    assert result["drafts_created"] == 2
    assert result["approvals_created"] == 2
    assert draft_count == 2
    assert approval_count == 2
    assert outbox_count == 0
    assert result["send_performed"] is False
    assert result["crm_write_performed"] is False
    assert result["approval_resolved"] is False


def test_autonomous_local_approval_requires_its_own_action_allowlist(tmp_path):
    from tools.whatsapp_ops_store import get_db_path, init_db
    from tools.whatsapp_ops_tool import wpp_proactive_draft_queue

    config = _autonomy_config(actions=["draft.create_local", "draft.preview"])
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        db = get_db_path()
        result = _parse(
            wpp_proactive_draft_queue(
                candidates=_candidates(count=1),
                mode="create",
                create_approvals=True,
                execution_context="autonomous",
                config=config,
            )
        )
        with sqlite3.connect(db) as conn:
            drafts = conn.execute("SELECT count(*) FROM drafts").fetchone()[0]
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is False
    assert result["error"] == "autonomy_denied"
    assert result["autonomy"]["action"] == "approval.request_local"
    assert "action_not_allowlisted" in result["autonomy"]["reasons"]
    assert drafts == 0


def test_tool_schemas_expose_only_bounded_execution_context():
    from tools.registry import registry
    import tools.whatsapp_ops_tool  # noqa: F401

    for name in ("wpp_opportunity_scores", "wpp_proactive_draft_queue"):
        entry = registry._tools.get(name)
        assert entry is not None
        prop = entry.schema["parameters"]["properties"]["execution_context"]
        assert prop["enum"] == ["operator", "autonomous"]
