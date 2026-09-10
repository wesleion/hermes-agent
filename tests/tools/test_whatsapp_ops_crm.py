import json
import sqlite3
from copy import deepcopy
from unittest.mock import Mock

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


CRM_HEADER = (
    "ID Interacao",
    "ID Lead",
    "ID Contato",
    "Tipo Interacao",
    "Data",
    "Resumo",
    "Proximo Passo",
)


@pytest.fixture(autouse=True)
def _trusted_contact_crm_binding(monkeypatch):
    import tools.whatsapp_ops_crm as crm

    monkeypatch.setattr(
        crm,
        "get_contact_crm_binding",
        lambda contact_id: {"lead_id": "L-001", "contact_id": "C-001"}
        if str(contact_id or "").strip()
        else None,
    )


def _config() -> dict:
    return {
        "crm": {
            "enabled": True,
            "read_only": False,
            "write_enabled": True,
            "append_after_send_enabled": True,
            "backend": "google_sheets_append",
            "mode": "append_only",
            "allowed_event_types": ["send_completed"],
            "google_sheets": {
                "spreadsheet_id": "sheet-prod-01",
                "range": "'Interacoes'!A:G",
                "lead_lookup_range": "'Leads'!A1:Z501",
                "allowed_spreadsheet_ids": ["sheet-prod-01"],
                "allowed_ranges": ["'Interacoes'!A:G"],
                "allowed_lead_lookup_ranges": ["'Leads'!A1:Z501"],
                "credentials_env": "GOOGLE_SERVICE_ACCOUNT_JSON",
                "timeout_seconds": 10,
            },
        }
    }


def _draft(targets=None) -> dict:
    targets = targets or [{
        "type": "contact",
        "contact_id": "contact_safe_01",
        "crm": {
            "lead_id": "L-001",
            "contact_id": "C-001",
            "interaction_type": "WhatsApp",
            "summary": "Primeiro contato supervisionado concluído",
            "next_step": "Aguardar retorno e qualificar interesse",
        },
    }]
    return {
        "id": "draft_safe_01",
        "targets_json": json.dumps(targets),
        "message": "conteudo que jamais pode ir ao CRM",
        "message_hash": "a" * 64,
        "idempotency_key": "b" * 64,
        "created_at": "2026-07-15T12:00:00+00:00",
    }


def _approval() -> dict:
    return {
        "id": "approval_safe_01",
        "status": "approved",
        "message_hash": "a" * 64,
        "draft_idempotency_key": "b" * 64,
        "approver_ref_hash": "c" * 64,
        "resolved_at": "2026-07-15T12:01:00+00:00",
    }


def _send_result() -> dict:
    return {"ok": True, "transport": "quepasa_direct", "provider_secret": "never-return"}


def _confirmed_response(payload: dict) -> dict:
    return {
        "updates": {
            "updatedRows": 1,
            "updatedRange": "'Interacoes'!A2:G2",
        },
        "read_back": {"values": [payload["row"]]},
    }


def test_crm_defaults_are_fail_closed_and_schema_is_stable():
    from tools.whatsapp_ops_crm import CRM_ROW_HEADER, default_crm_config
    from tools.whatsapp_ops_tool import _default_config

    defaults = default_crm_config()
    assert defaults == _default_config()["crm"]
    assert defaults["enabled"] is False
    assert defaults["read_only"] is True
    assert defaults["write_enabled"] is False
    assert defaults["append_after_send_enabled"] is False
    assert defaults["backend"] == "google_sheets_append"
    assert defaults["mode"] == "append_only"
    assert defaults["allowed_event_types"] == ["send_completed"]
    assert defaults["google_sheets"]["spreadsheet_id"] == ""
    assert defaults["google_sheets"]["range"] == ""
    assert defaults["google_sheets"]["lead_lookup_range"] == ""
    assert defaults["google_sheets"]["allowed_spreadsheet_ids"] == []
    assert defaults["google_sheets"]["allowed_ranges"] == []
    assert defaults["google_sheets"]["allowed_lead_lookup_ranges"] == []
    assert tuple(CRM_ROW_HEADER) == CRM_HEADER


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda c: c["crm"].update(enabled=False), "crm_disabled"),
        (lambda c: c["crm"].update(read_only=True), "crm_read_only"),
        (lambda c: c["crm"].update(write_enabled=False), "crm_write_disabled"),
        (
            lambda c: c["crm"].update(append_after_send_enabled=False),
            "crm_append_after_send_disabled",
        ),
        (lambda c: c["crm"].update(backend="other"), "crm_backend_invalid"),
        (lambda c: c["crm"].update(mode="upsert"), "crm_mode_invalid"),
        (lambda c: c["crm"].update(allowed_event_types=[]), "crm_event_type_not_allowed"),
        (lambda c: c["crm"]["google_sheets"].update(spreadsheet_id=""), "crm_spreadsheet_missing"),
        (lambda c: c["crm"]["google_sheets"].update(range=""), "crm_range_missing"),
        (
            lambda c: c["crm"]["google_sheets"].update(lead_lookup_range=""),
            "crm_lead_lookup_range_missing",
        ),
        (lambda c: c["crm"]["google_sheets"].update(timeout_seconds=0), "crm_timeout_invalid"),
        (lambda c: c["crm"]["google_sheets"].update(timeout_seconds=61), "crm_timeout_invalid"),
        (
            lambda c: c["crm"]["google_sheets"].update(allowed_spreadsheet_ids=["different"]),
            "crm_spreadsheet_not_allowed",
        ),
        (
            lambda c: c["crm"]["google_sheets"].update(allowed_ranges=["Other!A:G"]),
            "crm_range_not_allowed",
        ),
        (
            lambda c: c["crm"]["google_sheets"].update(
                allowed_lead_lookup_ranges=["'Other'!A1:Z501"]
            ),
            "crm_lead_lookup_range_not_allowed",
        ),
        (
            lambda c: c["crm"]["google_sheets"].update(
                spreadsheet_id="*", allowed_spreadsheet_ids=["*"]
            ),
            "crm_wildcard_forbidden",
        ),
        (
            lambda c: c["crm"]["google_sheets"].update(range="Hunter*!A:G", allowed_ranges=["Hunter*!A:G"]),
            "crm_wildcard_forbidden",
        ),
    ],
)
def test_preflight_blocks_each_missing_gate_without_calling_client(tmp_path, mutate, reason):
    from tools.whatsapp_ops_crm import append_approved_send_event

    config = deepcopy(_config())
    mutate(config)
    client = Mock()
    token = set_hermes_home_override(tmp_path)
    try:
        result = append_approved_send_event(
            draft=_draft(),
            approval=_approval(),
            send_result=_send_result(),
            config=config,
            client=client,
        )
    finally:
        reset_hermes_home_override(token)

    assert result["result"] == "blocked"
    assert result["reason"] == reason
    assert result["attempted"] is False
    assert result["write_performed"] is False
    client.assert_not_called()


def test_unsafe_credentials_env_is_blocked_before_client(tmp_path):
    from tools.whatsapp_ops_crm import append_approved_send_event

    config = _config()
    config["crm"]["google_sheets"]["credentials_env"] = "bad-env-name"
    client = Mock()
    token = set_hermes_home_override(tmp_path)
    try:
        result = append_approved_send_event(
            draft=_draft(), approval=_approval(), send_result=_send_result(), config=config, client=client
        )
    finally:
        reset_hermes_home_override(token)

    assert result["reason"] == "crm_credentials_env_invalid"
    client.assert_not_called()


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda approval: approval.update(approver_ref_hash=""), "approval_human_actor_missing"),
        (lambda approval: approval.update(resolved_at=""), "approval_resolution_missing"),
        (lambda approval: approval.update(message_hash="d" * 64), "approval_message_mismatch"),
        (
            lambda approval: approval.update(draft_idempotency_key="d" * 64),
            "approval_draft_mismatch",
        ),
    ],
)
def test_preflight_requires_resolved_human_approval_for_exact_message(tmp_path, mutate, reason):
    from tools.whatsapp_ops_crm import append_approved_send_event

    approval = _approval()
    mutate(approval)
    client = Mock()
    token = set_hermes_home_override(tmp_path)
    try:
        result = append_approved_send_event(
            draft=_draft(),
            approval=approval,
            send_result=_send_result(),
            config=_config(),
            client=client,
        )
    finally:
        reset_hermes_home_override(token)

    assert result["result"] == "blocked"
    assert result["reason"] == reason
    assert result["attempted"] is False
    client.assert_not_called()


@pytest.mark.parametrize(
    ("binding", "reason"),
    [
        (None, "crm_contact_binding_missing"),
        ({"lead_id": "L-999", "contact_id": "C-001"}, "crm_contact_binding_mismatch"),
        ({"lead_id": "L-001", "contact_id": "C-999"}, "crm_contact_binding_mismatch"),
    ],
)
def test_crm_context_must_match_trusted_local_contact_binding(tmp_path, monkeypatch, binding, reason):
    import tools.whatsapp_ops_crm as crm

    monkeypatch.setattr(crm, "get_contact_crm_binding", lambda _contact_id: binding)
    client = Mock()
    token = set_hermes_home_override(tmp_path)
    try:
        result = crm.append_approved_send_event(
            draft=_draft(),
            approval=_approval(),
            send_result=_send_result(),
            config=_config(),
            client=client,
        )
    finally:
        reset_hermes_home_override(token)

    assert result["result"] == "blocked"
    assert result["reason"] == reason
    assert result["attempted"] is False
    client.assert_not_called()


def test_confirmed_append_uses_business_schema_without_message_or_raw_target(tmp_path):
    from tools.whatsapp_ops_crm import append_approved_send_event

    captured = {}

    def client(payload, config):
        captured["payload"] = payload
        captured["config"] = config
        return _confirmed_response(payload)

    raw_target = "5511999990000@s.whatsapp.net"
    target = json.loads(_draft()["targets_json"])[0]
    target["contact_id"] = raw_target
    token = set_hermes_home_override(tmp_path)
    try:
        result = append_approved_send_event(
            draft=_draft([target]),
            approval=_approval(),
            send_result=_send_result(),
            config=_config(),
            client=client,
        )
    finally:
        reset_hermes_home_override(token)

    payload = captured["payload"]
    row = payload["row"]
    serialized = json.dumps({"result": result, "payload": payload}, ensure_ascii=False)
    assert result == {
        "enabled": True,
        "attempted": True,
        "result": "appended",
        "reason": "append_confirmed",
        "write_performed": True,
    }
    assert tuple(payload["header"]) == CRM_HEADER
    assert len(row) == len(CRM_HEADER)
    assert all(isinstance(value, str) for value in row)
    assert row == [
        row[0],
        "L-001",
        "C-001",
        "WhatsApp",
        "2026-07-15",
        "Primeiro contato supervisionado concluído",
        "Aguardar retorno e qualificar interesse",
    ]
    assert row[0].startswith("INT-")
    assert "conteudo que jamais" not in serialized
    assert raw_target not in serialized
    assert "5511999990000" not in serialized


def test_default_google_client_uses_append_raw_insert_rows_and_exact_scope(tmp_path, monkeypatch):
    import tools.whatsapp_ops_crm as crm

    config = _config()["crm"]
    info = {
        "type": "service_account",
        "private_key": "secret-key\\nsecond-line",
        "client_email": "svc@example.invalid",
    }
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(info))
    calls = {}
    row = ["INT-0123456789ab", "L-001", "C-001", "WhatsApp", "2026-08-14", "Resumo", "Próximo passo"]

    class Credentials:
        @classmethod
        def from_service_account_info(cls, supplied_info, scopes):
            calls["credentials"] = (supplied_info, scopes)
            return "credentials-object"

    class Request:
        def __init__(self, response):
            self.response = response

        def execute(self):
            calls["executed"] = calls.get("executed", 0) + 1
            return self.response

    class Values:
        def append(self, **kwargs):
            calls["append"] = kwargs
            return Request({"updates": {"updatedRows": 1, "updatedRange": "'Interacoes'!A2:G2"}})

        def get(self, **kwargs):
            calls.setdefault("get", []).append(kwargs)
            if kwargs["range"] == "'Interacoes'!A1:G1":
                return Request({"values": [list(CRM_HEADER)]})
            if kwargs["range"] == "'Leads'!A1:Z501":
                return Request({"values": [["ID Lead", "Nome"], ["L-001", "Safe"]]})
            return Request({"values": [row]})

        def update(self, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("update must never be called")

        def delete(self, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("delete must never be called")

        def batchUpdate(self, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("batchUpdate must never be called")

    class Spreadsheets:
        def values(self):
            return Values()

        def create(self, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("create must never be called")

    class Service:
        def spreadsheets(self):
            return Spreadsheets()

    def authorized_http(credentials, http):
        calls["authorized_http"] = (credentials, http)
        return "authorized-http"

    def http_factory(*, timeout):
        calls["http_timeout"] = timeout
        return {"timeout": timeout}

    def build(*args, **kwargs):
        calls["build"] = (args, kwargs)
        return Service()

    monkeypatch.setattr(
        crm,
        "_google_dependencies",
        lambda: (Credentials, build, authorized_http, http_factory),
    )
    token = set_hermes_home_override(tmp_path)
    try:
        result = crm.append_google_sheets_row({"header": list(CRM_HEADER), "row": row}, config)
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    normalized_info = dict(info)
    normalized_info["private_key"] = "secret-key\nsecond-line"
    assert calls["credentials"] == (
        normalized_info,
        ["https://www.googleapis.com/auth/spreadsheets"],
    )
    assert calls["http_timeout"] == 10
    assert calls["authorized_http"] == ("credentials-object", {"timeout": 10})
    assert calls["build"] == (
        ("sheets", "v4"),
        {"http": "authorized-http", "cache_discovery": False},
    )
    assert calls["append"] == {
        "spreadsheetId": "sheet-prod-01",
        "range": "'Interacoes'!A:G",
        "valueInputOption": "RAW",
        "insertDataOption": "INSERT_ROWS",
        "includeValuesInResponse": True,
        "body": {"values": [row]},
    }
    assert calls["get"] == [
        {
            "spreadsheetId": "sheet-prod-01",
            "range": "'Interacoes'!A1:G1",
            "majorDimension": "ROWS",
        },
        {
            "spreadsheetId": "sheet-prod-01",
            "range": "'Leads'!A1:Z501",
            "majorDimension": "ROWS",
        },
        {
            "spreadsheetId": "sheet-prod-01",
            "range": "'Interacoes'!A2:G2",
            "majorDimension": "ROWS",
        },
    ]
    assert calls["executed"] == 4


@pytest.mark.parametrize(
    ("interaction_header", "lead_values", "reason"),
    [
        (["wrong", "header"], [["ID Lead"], ["L-001"]], "crm_interacoes_schema_mismatch"),
        (list(CRM_HEADER), [["ID Lead"], ["L-999"]], "crm_lead_not_found"),
        (list(CRM_HEADER), [["ID Lead"], ["L-001"], ["L-001"]], "crm_lead_ambiguous"),
    ],
)
def test_default_google_client_blocks_before_append_when_schema_or_lead_is_invalid(
    tmp_path, monkeypatch, interaction_header, lead_values, reason
):
    import tools.whatsapp_ops_crm as crm

    config = _config()["crm"]
    monkeypatch.setenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        json.dumps({"type": "service_account", "private_key": "secret-key", "client_email": "svc@example.invalid"}),
    )
    append_calls = []

    class Credentials:
        @classmethod
        def from_service_account_info(cls, supplied_info, scopes):
            return "credentials-object"

    class Request:
        def __init__(self, response):
            self.response = response

        def execute(self):
            return self.response

    class Values:
        def get(self, **kwargs):
            if kwargs["range"] == "'Interacoes'!A1:G1":
                return Request({"values": [interaction_header]})
            if kwargs["range"] == "'Leads'!A1:Z501":
                return Request({"values": lead_values})
            raise AssertionError("unexpected read-back")

        def append(self, **kwargs):
            append_calls.append(kwargs)
            raise AssertionError("append must be blocked")

    class Service:
        def spreadsheets(self):
            return self

        def values(self):
            return Values()

    monkeypatch.setattr(
        crm,
        "_google_dependencies",
        lambda: (Credentials, lambda *a, **k: Service(), lambda credentials, http: "authorized", lambda **k: object()),
    )
    row = ["INT-0123456789ab", "L-001", "C-001", "WhatsApp", "2026-08-14", "Resumo", "Próximo passo"]
    token = set_hermes_home_override(tmp_path)
    try:
        result = crm.append_google_sheets_row({"header": list(CRM_HEADER), "row": row}, config)
    finally:
        reset_hermes_home_override(token)

    assert result == {"ok": False, "reason": reason}
    assert append_calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"updates": {"updatedRows": 0, "updatedRange": "'Interacoes'!A2:G2"}, "read_back": {"values": []}},
        {"updates": {"updatedRows": 1, "updatedRange": "'Interacoes'!A2:G2"}, "read_back": {"values": [["different"]]}},
        {"updates": {"updatedCells": 6, "updatedRange": "'Interacoes'!A2:G2"}, "read_back": {"values": []}},
        {},
    ],
)
def test_append_verification_mismatch_is_failed_unknown_and_never_retried(tmp_path, response):
    from tools.whatsapp_ops_crm import append_approved_send_event

    client = Mock(return_value=response)
    token = set_hermes_home_override(tmp_path)
    try:
        first = append_approved_send_event(
            draft=_draft(), approval=_approval(), send_result=_send_result(), config=_config(), client=client
        )
        second = append_approved_send_event(
            draft=_draft(), approval=_approval(), send_result=_send_result(), config=_config(), client=client
        )
    finally:
        reset_hermes_home_override(token)

    assert first["result"] == "failed_unknown"
    assert first["reason"] == "append_unverified"
    assert first["write_performed"] is False
    assert second["result"] == "blocked"
    assert second["reason"] == "idempotency_uncertain"
    client.assert_called_once()


def test_exception_is_sanitized_and_audited_without_secrets_or_pii(tmp_path):
    from tools.whatsapp_ops_crm import append_approved_send_event

    secret = "super-secret-private-key"
    spreadsheet_id = _config()["crm"]["google_sheets"]["spreadsheet_id"]
    raw_target = "5511999990000@s.whatsapp.net"
    target = json.loads(_draft()["targets_json"])[0]
    target["contact_id"] = raw_target
    client = Mock(side_effect=RuntimeError(f"{secret} {spreadsheet_id} {raw_target}"))
    token = set_hermes_home_override(tmp_path)
    try:
        result = append_approved_send_event(
            draft=_draft([target]),
            approval=_approval(),
            send_result=_send_result(),
            config=_config(),
            client=client,
        )
        db_path = tmp_path / "wpp_ops.sqlite"
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["result"] == "failed_unknown"
    assert result["reason"] == "crm_client_exception"
    assert result["error_class"] == "RuntimeError"
    assert secret not in serialized
    assert spreadsheet_id not in serialized
    assert raw_target not in serialized
    with sqlite3.connect(db_path) as conn:
        crm_rows = conn.execute(
            "SELECT idempotency_key, event_id, status, last_reason FROM crm_append_log"
        ).fetchall()
        audit_rows = conn.execute(
            "SELECT event_type, safe_summary, metadata_redacted_json FROM audit_log WHERE event_type LIKE 'crm_append_%'"
        ).fetchall()
    persisted = json.dumps({"crm": crm_rows, "audit": audit_rows}, ensure_ascii=False)
    assert crm_rows[0][2:] == ("failed_unknown", "crm_client_exception")
    assert audit_rows[0][0] == "crm_append_failed"
    assert secret not in persisted
    assert spreadsheet_id not in persisted
    assert raw_target not in persisted
    assert "5511999990000" not in persisted


def test_duplicate_confirmed_append_returns_replay_without_second_client_call(tmp_path):
    from tools.whatsapp_ops_crm import append_approved_send_event

    client = Mock(side_effect=lambda payload, config: _confirmed_response(payload))
    token = set_hermes_home_override(tmp_path)
    try:
        first = append_approved_send_event(
            draft=_draft(), approval=_approval(), send_result=_send_result(), config=_config(), client=client
        )
        second = append_approved_send_event(
            draft=_draft(), approval=_approval(), send_result=_send_result(), config=_config(), client=client
        )
    finally:
        reset_hermes_home_override(token)

    assert first["result"] == "appended"
    assert second == {
        "enabled": True,
        "attempted": False,
        "result": "idempotent_replay",
        "reason": "already_appended",
        "write_performed": False,
    }
    client.assert_called_once()


@pytest.mark.parametrize(
    ("targets", "reason"),
    [
        ([{"type": "contact", "contact_id": "contact_safe_01"}], "crm_context_missing"),
        ([{"type": "group_create", "name": "Grupo", "crm": {}}], "crm_contact_target_required"),
        ([{"type": "contact", "contact_id": "contact_safe_01", "crm": {"lead_id": "L-001"}}], "crm_context_invalid"),
    ],
)
def test_crm_context_and_single_contact_target_are_required(tmp_path, targets, reason):
    from tools.whatsapp_ops_crm import append_approved_send_event

    client = Mock()
    token = set_hermes_home_override(tmp_path)
    try:
        result = append_approved_send_event(
            draft=_draft(targets),
            approval=_approval(),
            send_result=_send_result(),
            config=_config(),
            client=client,
        )
    finally:
        reset_hermes_home_override(token)

    assert result["result"] == "blocked"
    assert result["reason"] == reason
    assert result["write_performed"] is False
    client.assert_not_called()


def test_store_reservation_is_atomic_and_uncertain_states_block(tmp_path):
    from tools.whatsapp_ops_store import mark_crm_append_result, reserve_crm_append

    token = set_hermes_home_override(tmp_path)
    try:
        first = reserve_crm_append("c" * 64, "crm_evt_safe")
        duplicate_reserved = reserve_crm_append("c" * 64, "crm_evt_safe")
        mark_crm_append_result("c" * 64, status="failed_unknown", reason="append_unverified")
        duplicate_failed = reserve_crm_append("c" * 64, "crm_evt_safe")
    finally:
        reset_hermes_home_override(token)

    assert first == {"reserved": True, "status": "reserved"}
    assert duplicate_reserved == {"reserved": False, "status": "reserved"}
    assert duplicate_failed == {"reserved": False, "status": "failed_unknown"}


def test_no_public_generic_crm_write_tool_is_registered():
    from tools.registry import registry
    import tools.whatsapp_ops_tool  # noqa: F401

    names = registry.get_tool_names_for_toolset("whatsapp_ops")
    assert not any("crm" in name and any(word in name for word in ("write", "append", "update", "delete")) for name in names)
