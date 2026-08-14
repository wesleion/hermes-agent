from __future__ import annotations


def _config(*, mode="assist", actions=None, areas=None, max_items=3, kill_switch=False):
    return {
        "kill_switch": kill_switch,
        "autonomy": {
            "mode": mode,
            "allowed_actions": actions if actions is not None else [
                "opportunity.score",
                "draft.preview",
                "draft.create_local",
            ],
            "allowed_areas": areas if areas is not None else [
                "commercial_discovery",
                "commercial_drafts",
            ],
            "max_items_per_run": max_items,
            "min_confidence": 0.75,
        },
    }


def test_missing_config_is_fail_closed():
    from tools.whatsapp_ops_autonomy import evaluate_autonomy

    decision = evaluate_autonomy({}, action="opportunity.score")

    assert decision.allowed is False
    assert decision.mode == "off"
    assert "autonomy_off" in decision.reasons


def test_assist_allows_only_allowlisted_read_only_actions():
    from tools.whatsapp_ops_autonomy import evaluate_autonomy

    score = evaluate_autonomy(_config(), action="opportunity.score", requested_items=9)
    preview = evaluate_autonomy(_config(), action="draft.preview")
    create = evaluate_autonomy(_config(), action="draft.create_local")

    assert score.allowed is True
    assert score.effect == "read_only"
    assert score.effective_items == 3
    assert preview.allowed is True
    assert create.allowed is False
    assert "safe_auto_required" in create.reasons


def test_safe_auto_allows_bounded_local_drafts_only_when_area_and_action_are_allowlisted():
    from tools.whatsapp_ops_autonomy import evaluate_autonomy

    allowed = evaluate_autonomy(
        _config(mode="safe_auto", max_items=2),
        action="draft.create_local",
        area="commercial_drafts",
        requested_items=50,
    )
    wrong_area = evaluate_autonomy(
        _config(mode="safe_auto"),
        action="draft.create_local",
        area="commercial_discovery",
    )
    missing_action = evaluate_autonomy(
        _config(mode="safe_auto", actions=["draft.preview"]),
        action="draft.create_local",
    )

    assert allowed.allowed is True
    assert allowed.effective_items == 2
    assert allowed.max_items == 2
    assert wrong_area.allowed is False
    assert "area_mismatch" in wrong_area.reasons
    assert missing_action.allowed is False
    assert "action_not_allowlisted" in missing_action.reasons


def test_sensitive_actions_are_hard_denied_even_if_allowlisted():
    from tools.whatsapp_ops_autonomy import HARD_DENIED_ACTIONS, evaluate_autonomy

    for action in sorted(HARD_DENIED_ACTIONS):
        decision = evaluate_autonomy(
            _config(
                mode="safe_auto",
                actions=[action],
                areas=["external_effects", "runtime", "data_ingestion"],
            ),
            action=action,
        )
        assert decision.allowed is False, action
        assert "action_hard_denied" in decision.reasons, action
        assert decision.requires_human_gate is True


def test_kill_switch_and_invalid_modes_fail_closed():
    from tools.whatsapp_ops_autonomy import evaluate_autonomy

    killed = evaluate_autonomy(_config(kill_switch=True), action="opportunity.score")
    invalid = evaluate_autonomy(_config(mode="yolo"), action="opportunity.score")

    assert killed.allowed is False
    assert "kill_switch_active" in killed.reasons
    assert invalid.allowed is False
    assert invalid.mode == "off"
    assert "autonomy_mode_invalid" in invalid.reasons


def test_unknown_action_and_bad_config_shapes_fail_closed_without_exception():
    from tools.whatsapp_ops_autonomy import evaluate_autonomy

    unknown = evaluate_autonomy(_config(mode="safe_auto"), action="shell.anything")
    malformed = evaluate_autonomy(
        {"autonomy": {"mode": "safe_auto", "allowed_actions": "*", "allowed_areas": "*"}},
        action="opportunity.score",
    )

    assert unknown.allowed is False
    assert "action_unknown" in unknown.reasons
    assert malformed.allowed is False
    assert "action_not_allowlisted" in malformed.reasons
    assert "area_not_allowlisted" in malformed.reasons


def test_non_finite_policy_bounds_fail_closed_without_exception():
    from tools.whatsapp_ops_autonomy import autonomy_status, evaluate_autonomy

    config = _config(mode="safe_auto")
    config["autonomy"]["max_items_per_run"] = float("inf")
    config["autonomy"]["min_confidence"] = float("nan")

    decision = evaluate_autonomy(
        config,
        action="opportunity.score",
        requested_items=float("-inf"),
    )
    status = autonomy_status(config)

    assert decision.max_items == 1
    assert decision.min_confidence == 0.75
    assert status["max_items_per_run"] == 1
    assert status["min_confidence"] == 0.75



def test_status_is_sanitized_and_explicit_about_hard_denies():
    from tools.whatsapp_ops_autonomy import HARD_DENIED_ACTIONS, autonomy_status

    status = autonomy_status(_config(mode="safe_auto", max_items=2))

    assert status["ok"] is True
    assert status["mode"] == "safe_auto"
    assert status["max_items_per_run"] == 2
    assert status["allowed_actions"] == [
        "draft.create_local",
        "draft.preview",
        "opportunity.score",
    ]
    assert set(status["hard_denied_actions"]) == HARD_DENIED_ACTIONS
    assert status["external_effects_allowed"] is False
    assert status["approval_resolution_allowed"] is False
    assert "credentials" not in status


def test_commercial_state_transitions_are_pure_deterministic_and_payload_free():
    from tools.whatsapp_ops_autonomy import transition_sales_stage

    state = {
        "project_ref": "project_demo_01",
        "lead_ref": "lead_demo_01",
        "stage": "BDR",
    }
    expected = (
        ("lead_qualified", "SDR"),
        ("opportunity_qualified", "Closer"),
        ("deal_won", "Support"),
    )
    lifecycle = ["BDR"]
    for event, stage in expected:
        result = transition_sales_stage(
            project_ref=state["project_ref"],
            lead_ref=state["lead_ref"],
            stage=state["stage"],
            event=event,
        )
        assert result.transitioned is True
        assert result.code == "sales_stage_transitioned"
        assert result.from_stage == state["stage"]
        assert result.to_stage == stage
        state["stage"] = stage
        lifecycle.append(stage)

    assert lifecycle == ["BDR", "SDR", "Closer", "Support"]
    assert result.as_dict() == {
        "transitioned": True,
        "code": "sales_stage_transitioned",
        "project_ref": "project_demo_01",
        "lead_ref": "lead_demo_01",
        "from_stage": "Closer",
        "to_stage": "Support",
        "event": "deal_won",
    }


def test_commercial_state_invalid_stage_event_or_ref_fails_closed_without_echo():
    import json

    from tools.whatsapp_ops_autonomy import transition_sales_stage

    bad_stage = transition_sales_stage(
        project_ref="project_demo_01",
        lead_ref="lead_demo_01",
        stage="Unknown",
        event="lead_qualified",
    )
    bad_event = transition_sales_stage(
        project_ref="project_demo_01",
        lead_ref="lead_demo_01",
        stage="BDR",
        event="deal_won",
    )
    unknown_event = transition_sales_stage(
        project_ref="project_demo_01",
        lead_ref="lead_demo_01",
        stage="BDR",
        event="free_text_payload_canary",
    )
    raw_ref = "551199998888@s.whatsapp.net"
    bad_ref = transition_sales_stage(
        project_ref="project_demo_01",
        lead_ref=raw_ref,
        stage="BDR",
        event="lead_qualified",
    )

    assert bad_stage.transitioned is False
    assert bad_stage.code == "sales_stage_invalid"
    assert bad_event.transitioned is False
    assert bad_event.code == "sales_transition_denied"
    assert unknown_event.transitioned is False
    assert unknown_event.code == "sales_event_invalid"
    assert "free_text_payload_canary" not in json.dumps(unknown_event.as_dict())
    assert bad_ref.transitioned is False
    assert bad_ref.code == "sales_ref_invalid"
    assert raw_ref not in json.dumps(bad_ref.as_dict())
