from datetime import datetime, timedelta, timezone


def _base_config(**overrides):
    config = {
        "send_enabled": True,
        "kill_switch": False,
        "approval": {"required": True, "timeout_minutes": 60},
        "allowlists": {"contacts": ["c_1"], "groups": []},
        "quepasa": {"send_enabled": True},
    }
    config.update(overrides)
    return config


def _draft(**overrides):
    draft = {
        "id": "d_1",
        "status": "approved",
        "targets": [{"type": "contact", "contact_id": "c_1", "ambiguous": False}],
        "message": "Oi",
        "message_hash": "hash-ok",
        "approved_message_hash": "hash-ok",
        "idempotency_key": "idem-1",
        "has_untrusted_media": False,
    }
    draft.update(overrides)
    return draft


def _approval(**overrides):
    approval = {
        "status": "approved",
        "token_valid": True,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        "message_hash": "hash-ok",
    }
    approval.update(overrides)
    return approval


def test_send_guardrails_reject_when_send_enabled_false():
    from tools.whatsapp_ops_policy import evaluate_send_guardrails

    result = evaluate_send_guardrails(
        config=_base_config(send_enabled=False),
        draft=_draft(),
        approval=_approval(),
        idempotency_used=False,
    )

    assert result.allowed is False
    assert "send_disabled" in result.reasons


def test_send_guardrails_reject_without_approval_token():
    from tools.whatsapp_ops_policy import evaluate_send_guardrails

    result = evaluate_send_guardrails(
        config=_base_config(),
        draft=_draft(),
        approval=None,
        idempotency_used=False,
    )

    assert result.allowed is False
    assert "approval_missing" in result.reasons


def test_send_guardrails_reject_target_outside_whitelist():
    from tools.whatsapp_ops_policy import evaluate_send_guardrails

    result = evaluate_send_guardrails(
        config=_base_config(allowlists={"contacts": ["other"], "groups": []}),
        draft=_draft(),
        approval=_approval(),
        idempotency_used=False,
    )

    assert result.allowed is False
    assert "target_not_whitelisted" in result.reasons


def test_send_guardrails_reject_message_changed_after_approval():
    from tools.whatsapp_ops_policy import evaluate_send_guardrails

    result = evaluate_send_guardrails(
        config=_base_config(),
        draft=_draft(message_hash="changed"),
        approval=_approval(message_hash="hash-ok"),
        idempotency_used=False,
    )

    assert result.allowed is False
    assert "message_changed_after_approval" in result.reasons


def test_send_guardrails_reject_duplicate_idempotency():
    from tools.whatsapp_ops_policy import evaluate_send_guardrails

    result = evaluate_send_guardrails(
        config=_base_config(),
        draft=_draft(),
        approval=_approval(),
        idempotency_used=True,
    )

    assert result.allowed is False
    assert "idempotency_duplicate" in result.reasons


def test_send_guardrails_reject_global_blocks():
    from tools.whatsapp_ops_policy import evaluate_send_guardrails

    result = evaluate_send_guardrails(
        config=_base_config(kill_switch=True, quepasa={"send_enabled": False}),
        draft=_draft(),
        approval=_approval(),
        idempotency_used=False,
    )

    assert result.allowed is False
    assert "kill_switch_active" in result.reasons
    assert "quepasa_send_disabled" in result.reasons


def test_send_guardrails_reject_ambiguous_contact_and_untrusted_media():
    from tools.whatsapp_ops_policy import evaluate_send_guardrails

    result = evaluate_send_guardrails(
        config=_base_config(),
        draft=_draft(
            targets=[{"type": "contact", "contact_id": "c_1", "ambiguous": True}],
            has_untrusted_media=True,
        ),
        approval=_approval(),
        idempotency_used=False,
    )

    assert result.allowed is False
    assert "target_ambiguous" in result.reasons
    assert "media_untrusted" in result.reasons


def test_send_guardrails_reject_malformed_string_target_without_crashing():
    from tools.whatsapp_ops_policy import evaluate_send_guardrails

    result = evaluate_send_guardrails(
        config=_base_config(),
        draft=_draft(targets=["+551****0000"]),
        approval=_approval(),
        idempotency_used=False,
    )

    assert result.allowed is False
    assert "payload_invalid" in result.reasons


def test_send_guardrails_allow_only_when_all_conditions_pass():
    from tools.whatsapp_ops_policy import evaluate_send_guardrails

    result = evaluate_send_guardrails(
        config=_base_config(),
        draft=_draft(),
        approval=_approval(),
        idempotency_used=False,
    )

    assert result.allowed is True
    assert result.reasons == []
