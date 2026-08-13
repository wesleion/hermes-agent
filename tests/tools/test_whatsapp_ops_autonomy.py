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
