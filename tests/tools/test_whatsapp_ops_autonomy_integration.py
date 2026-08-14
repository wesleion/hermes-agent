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


def _mission_leads(count=3):
    return [
        {
            "lead_id": f"LEAD-{idx}",
            "name": f"Projeto {idx}",
            "status_pipeline": "proposta enviada",
            "last_interaction_days": 21,
            "open_tasks": 2,
            "context": "cliente pediu preço e quer marcar próximo passo",
            "target": {"type": "contact", "contact_id": f"lead_{idx}"},
        }
        for idx in range(1, count + 1)
    ]


def _register_mission(*, max_items=4, max_local_writes=2, mode="safe_auto"):
    from datetime import datetime, timedelta, timezone
    from tools.whatsapp_ops_store import register_mission_envelope

    return register_mission_envelope(
        project_ref=f"project_mission_{max_items}_{max_local_writes}_{mode}",
        sales_pack_digest="8" * 64,
        mode=mode,
        max_items=max_items,
        max_local_writes=max_local_writes,
        window_ref=f"window_mission_{max_items}_{max_local_writes}_{mode}",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )


def _business_counts(db):
    with sqlite3.connect(db) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("mission_envelopes", "autonomy_runs", "drafts", "approvals", "outbox", "contacts")
        }


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


def test_safe_auto_config_cannot_be_bypassed_with_operator_context(tmp_path):
    from tools.whatsapp_ops_store import get_db_path, init_db
    from tools.whatsapp_ops_tool import wpp_opportunity_scores, wpp_proactive_draft_queue

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        db = get_db_path()
        scored = _parse(
            wpp_opportunity_scores(
                leads=_candidates(count=5),
                limit=50,
                execution_context="operator",
                config=_autonomy_config(max_items=2),
            )
        )
        blocked = _parse(
            wpp_proactive_draft_queue(
                candidates=_candidates(count=3),
                mode="create",
                create_approvals=False,
                execution_context="operator",
                config=_autonomy_config(actions=["opportunity.score", "draft.preview"]),
            )
        )
        with sqlite3.connect(db) as conn:
            drafts = conn.execute("SELECT count(*) FROM drafts").fetchone()[0]
    finally:
        reset_hermes_home_override(token)

    assert scored["ok"] is True
    assert scored["execution_context"] == "operator"
    assert scored["autonomy"]["mode"] == "safe_auto"
    assert scored["limit"] == 2
    assert scored["count"] == 2
    assert blocked["ok"] is False
    assert blocked["error"] == "autonomy_denied"
    assert "action_not_allowlisted" in blocked["autonomy"]["reasons"]
    assert blocked["drafts_created"] == 0
    assert drafts == 0


def test_tool_schemas_expose_only_bounded_execution_context():
    from tools.registry import registry
    import tools.whatsapp_ops_tool  # noqa: F401

    for name in ("wpp_opportunity_scores", "wpp_proactive_draft_queue"):
        entry = registry._tools.get(name)
        assert entry is not None
        prop = entry.schema["parameters"]["properties"]["execution_context"]
        assert prop["enum"] == ["operator", "autonomous"]

    autonomous = registry._tools.get("wpp_autonomous_run")
    assert autonomous is not None
    properties = autonomous.schema["parameters"]["properties"]
    assert set(properties) == {"mission_envelope_digest", "run_key", "lead_inputs", "mode"}
    assert properties["mode"]["enum"] == ["preview", "queue"]
    assert "config" not in properties
    assert "fence" not in properties


def test_autonomous_preview_composes_score_without_business_mutation(tmp_path, monkeypatch):
    import tools.whatsapp_ops_tool as tool
    from tools.whatsapp_ops_store import get_db_path

    calls = {"score": 0}
    original_score = tool.wpp_opportunity_scores

    def observed_score(*args, **kwargs):
        calls["score"] += 1
        return original_score(*args, **kwargs)

    monkeypatch.setattr(tool, "wpp_opportunity_scores", observed_score)
    token = set_hermes_home_override(tmp_path)
    try:
        envelope = _register_mission(max_items=4, max_local_writes=2)
        db = get_db_path()
        before = _business_counts(db)
        result = _parse(
            tool.wpp_autonomous_run(
                envelope["envelope_digest"],
                "run_preview_01",
                _mission_leads(count=4),
                "preview",
                _autonomy_config(max_items=2),
            )
        )
        after = _business_counts(db)
    finally:
        reset_hermes_home_override(token)

    assert calls == {"score": 1}
    assert result["ok"] is True
    assert result["mode"] == "preview"
    assert result["status"] == "previewed"
    assert result["counters"] == {"input_leads": 4, "opportunities": 2, "actionable": 2, "blocked": 0, "local_writes": 0}
    assert len(result["preview_digest"]) == 64
    assert before == after
    for flag in (
        "send_performed",
        "crm_write_performed",
        "provider_history_used",
        "approval_resolved",
        "telegram_notification_sent",
        "cron_activation",
    ):
        assert result[flag] is False


def test_autonomous_queue_is_bounded_local_only_and_completed_duplicate_is_noop(tmp_path, monkeypatch):
    import tools.whatsapp_ops_tool as tool
    from tools.whatsapp_ops_store import get_db_path

    calls = {"score": 0, "queue": 0}
    original_score = tool.wpp_opportunity_scores
    original_queue = tool.wpp_proactive_draft_queue

    def observed_score(*args, **kwargs):
        calls["score"] += 1
        return original_score(*args, **kwargs)

    def observed_queue(*args, **kwargs):
        calls["queue"] += 1
        return original_queue(*args, **kwargs)

    monkeypatch.setattr(tool, "wpp_opportunity_scores", observed_score)
    monkeypatch.setattr(tool, "wpp_proactive_draft_queue", observed_queue)
    token = set_hermes_home_override(tmp_path)
    try:
        envelope = _register_mission(max_items=4, max_local_writes=2)
        db = get_db_path()
        before = _business_counts(db)
        first = _parse(
            tool.wpp_autonomous_run(
                envelope["envelope_digest"],
                "run_queue_01",
                _mission_leads(count=4),
                mode="queue",
                config=_autonomy_config(max_items=3),
            )
        )
        after_first = _business_counts(db)
        duplicate = _parse(
            tool.wpp_autonomous_run(
                envelope["envelope_digest"],
                "run_queue_01",
                _mission_leads(count=4),
                mode="queue",
                config=_autonomy_config(max_items=3),
            )
        )
        after_duplicate = _business_counts(db)
    finally:
        reset_hermes_home_override(token)

    assert calls == {"score": 1, "queue": 1}
    assert first["ok"] is True
    assert first["status"] == "completed"
    assert first["result_class"] == "success"
    assert first["counters"]["local_writes"] == 2
    assert after_first["drafts"] == before["drafts"] + 2
    assert after_first["approvals"] == before["approvals"] + 2
    assert after_first["outbox"] == before["outbox"]
    assert after_first["autonomy_runs"] == before["autonomy_runs"] + 1
    assert duplicate["ok"] is True
    assert duplicate["status"] == "completed"
    assert duplicate["deduped"] is True
    assert duplicate["result_class"] == "success"
    assert "fence" not in json.dumps(duplicate, ensure_ascii=False)
    assert after_duplicate == after_first


def test_autonomous_queue_denies_assist_envelope_before_reserving_run(tmp_path):
    from tools.whatsapp_ops_store import get_db_path
    from tools.whatsapp_ops_tool import wpp_autonomous_run

    token = set_hermes_home_override(tmp_path)
    try:
        envelope = _register_mission(max_items=2, max_local_writes=0, mode="assist")
        db = get_db_path()
        before = _business_counts(db)
        result = _parse(
            wpp_autonomous_run(
                envelope["envelope_digest"],
                "run_assist_denied_01",
                _mission_leads(count=1),
                mode="queue",
                config=_autonomy_config(mode="assist"),
            )
        )
        after = _business_counts(db)
    finally:
        reset_hermes_home_override(token)

    assert result == {
        "approval_resolved": False,
        "cron_activation": False,
        "crm_write_performed": False,
        "error": "mission_envelope_local_write_denied",
        "ok": False,
        "provider_history_used": False,
        "send_performed": False,
        "telegram_notification_sent": False,
    }
    assert after == before


def test_autonomous_queue_blocks_unsafe_or_manual_inputs_without_local_writes(tmp_path):
    from tools.whatsapp_ops_store import get_db_path
    from tools.whatsapp_ops_tool import wpp_autonomous_run

    raw_phone = "553199998" + "765"
    raw_url = "https" + "://unsafe.example/path"
    cases = [
        {**_mission_leads(1)[0], "message": "fale no " + raw_phone},
        {**_mission_leads(1)[0], "context": "veja " + raw_url},
        {**_mission_leads(1)[0], "objective": "abra " + raw_url},
        {**_mission_leads(1)[0], "source_context": {"summary": "ligue " + raw_phone}},
        {**_mission_leads(1)[0], "target": {"type": "group", "group_id": "grp_demo"}},
        {**_mission_leads(1)[0], "last_direction": "outgoing"},
        {**_mission_leads(1)[0], "manual_required": True},
    ]
    token = set_hermes_home_override(tmp_path)
    try:
        envelope = _register_mission(max_items=1, max_local_writes=1)
        db = get_db_path()
        before = _business_counts(db)
        results = [
            _parse(
                wpp_autonomous_run(
                    envelope["envelope_digest"],
                    f"run_blocked_{idx}",
                    [lead],
                    mode="queue",
                    config=_autonomy_config(max_items=1),
                )
            )
            for idx, lead in enumerate(cases, start=1)
        ]
        after = _business_counts(db)
    finally:
        reset_hermes_home_override(token)

    assert all(item["status"] == "completed" for item in results)
    assert all(item["result_class"] == "policy_blocked" for item in results)
    assert all(item["counters"]["local_writes"] == 0 for item in results)
    assert after["drafts"] == before["drafts"]
    assert after["approvals"] == before["approvals"]
    assert after["outbox"] == before["outbox"]
    serialized = json.dumps(results, ensure_ascii=False)
    assert raw_phone not in serialized
    assert "unsafe.example" not in serialized


def test_autonomous_queue_exception_completes_safe_failure_and_is_not_blind_retried(tmp_path, monkeypatch):
    import tools.whatsapp_ops_tool as tool

    calls = {"queue": 0}

    def fail_queue(*args, **kwargs):
        calls["queue"] += 1
        raise RuntimeError("synthetic partial ambiguity")

    monkeypatch.setattr(tool, "wpp_proactive_draft_queue", fail_queue)
    token = set_hermes_home_override(tmp_path)
    try:
        envelope = _register_mission(max_items=1, max_local_writes=1)
        first = _parse(
            tool.wpp_autonomous_run(
                envelope["envelope_digest"],
                "run_safe_failure_01",
                _mission_leads(count=1),
                mode="queue",
                config=_autonomy_config(max_items=1),
            )
        )
        duplicate = _parse(
            tool.wpp_autonomous_run(
                envelope["envelope_digest"],
                "run_safe_failure_01",
                _mission_leads(count=1),
                mode="queue",
                config=_autonomy_config(max_items=1),
            )
        )
    finally:
        reset_hermes_home_override(token)

    assert calls == {"queue": 1}
    assert first["status"] == "completed"
    assert first["result_class"] == "safe_failure"
    assert first["partial_ambiguous"] is True
    assert duplicate["deduped"] is True
    assert duplicate["result_class"] == "safe_failure"


def test_autonomous_queue_revalidates_envelope_atomically_at_local_write_boundary(tmp_path, monkeypatch):
    import tools.whatsapp_ops_tool as tool
    from tools.whatsapp_ops_store import get_db_path, kill_mission_envelope

    original_queue = tool.wpp_proactive_draft_queue
    token = set_hermes_home_override(tmp_path)
    try:
        envelope = _register_mission(max_items=1, max_local_writes=1)
        db = get_db_path()

        def kill_then_queue(*args, **kwargs):
            killed = kill_mission_envelope(envelope["envelope_digest"])
            assert killed["killed"] is True
            return original_queue(*args, **kwargs)

        monkeypatch.setattr(tool, "wpp_proactive_draft_queue", kill_then_queue)
        before = _business_counts(db)
        result = _parse(
            tool.wpp_autonomous_run(
                envelope["envelope_digest"],
                "run_kill_boundary_01",
                _mission_leads(count=1),
                mode="queue",
                config=_autonomy_config(max_items=1),
            )
        )
        after = _business_counts(db)
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is False
    assert result["error"] == "mission_envelope_killed"
    assert after["drafts"] == before["drafts"]
    assert after["approvals"] == before["approvals"]
    assert after["outbox"] == before["outbox"]
