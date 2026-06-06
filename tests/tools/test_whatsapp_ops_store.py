import json
import sqlite3

from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def test_init_db_creates_required_tables_idempotently(tmp_path):
    from tools.whatsapp_ops_store import init_db, get_db_path

    token = set_hermes_home_override(tmp_path)
    try:
        db_path = init_db()
        second_path = init_db()
    finally:
        reset_hermes_home_override(token)

    assert db_path == second_path == tmp_path / "wpp_ops.sqlite"
    assert db_path.exists()

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert {
        "contacts",
        "contact_aliases",
        "lists",
        "list_members",
        "drafts",
        "approvals",
        "outbox",
        "inbound_events",
        "audit_log",
    }.issubset(tables)
    assert get_db_path(tmp_path) == tmp_path / "wpp_ops.sqlite"


def test_create_draft_persists_hash_and_idempotency_without_sending(tmp_path):
    from tools.whatsapp_ops_store import create_draft, get_draft, init_db

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Olá teste seguro",
        )
        stored = get_draft(draft["draft_id"])
    finally:
        reset_hermes_home_override(token)

    assert stored is not None
    assert stored["status"] == "draft"
    assert stored["message"] == "Olá teste seguro"
    assert stored["message_hash"]
    assert stored["idempotency_key"]
    assert json.loads(stored["targets_json"])[0]["contact_id"] == "c_1"


def test_approval_token_is_stored_hashed_not_plaintext(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, init_db

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Mensagem aprovada",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        db_path = tmp_path / "wpp_ops.sqlite"
    finally:
        reset_hermes_home_override(token)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT approval_token_hash FROM approvals WHERE draft_id=?",
            (draft["draft_id"],),
        ).fetchall()

    assert len(rows) == 1
    assert "approval_token" not in approval
    assert len(rows[0][0]) == 64


def test_create_approval_starts_pending_and_does_not_return_plaintext_token(tmp_path):
    from tools.whatsapp_ops_store import create_approval, create_draft, get_draft, init_db

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Aprovação humana pendente",
        )
        approval = create_approval(draft["draft_id"], timeout_minutes=60)
        stored_draft = get_draft(draft["draft_id"])
        db_path = tmp_path / "wpp_ops.sqlite"
    finally:
        reset_hermes_home_override(token)

    assert approval["approval_id"].startswith("approval_")
    assert approval["status"] == "pending"
    assert "approval_token" not in approval
    assert stored_draft["status"] == "pending_approval"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, resolved_at FROM approvals WHERE id=?",
            (approval["approval_id"],),
        ).fetchone()
    assert row == ("pending", None)


def test_resolve_approval_approve_and_deny_update_state_without_raw_approver(tmp_path):
    from tools.whatsapp_ops_store import (
        create_approval,
        create_draft,
        get_draft,
        init_db,
        resolve_approval,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        approved_draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Aprovar",
        )
        denied_draft = create_draft(
            targets=[{"type": "contact", "contact_id": "c_1"}],
            message="Negar",
        )
        approval = create_approval(approved_draft["draft_id"])
        denial = create_approval(denied_draft["draft_id"])
        approved = resolve_approval(
            approval["approval_id"], decision="approved", approver_ref="telegram:12345"
        )
        denied = resolve_approval(
            denial["approval_id"], decision="denied", approver_ref="telegram:12345"
        )
        approved_status = get_draft(approved_draft["draft_id"])["status"]
        denied_status = get_draft(denied_draft["draft_id"])["status"]
        db_path = tmp_path / "wpp_ops.sqlite"
    finally:
        reset_hermes_home_override(token)

    assert approved["ok"] is True
    assert approved["status"] == "approved"
    assert denied["ok"] is True
    assert denied["status"] == "denied"
    assert approved_status == "approved"
    assert denied_status == "denied"
    with sqlite3.connect(db_path) as conn:
        hashes = [
            row[0]
            for row in conn.execute(
                "SELECT approver_ref_hash FROM approvals WHERE approver_ref_hash IS NOT NULL"
            ).fetchall()
        ]
    assert hashes and all("telegram:12345" not in value for value in hashes)
    assert all(len(value) == 64 for value in hashes)


def test_record_and_lookup_inbound_event_returns_sanitized_payload(tmp_path):
    from tools.whatsapp_ops_store import init_db, lookup_inbound_events, record_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        first = record_inbound_event(
            source_event_id="evt-real-123",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={
                "text": "Olá inbound real",
                "phone": "+551****0000",
                "token": "secret-token",
                "api_key": "secret-key",
            },
        )
        second = record_inbound_event(
            source_event_id="evt-real-123",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={"text": "duplicado"},
        )
        events = lookup_inbound_events(contact="172185238905034@lid", limit=10)
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(events, ensure_ascii=False)
    assert first["ok"] is True
    assert second["ok"] is True
    assert second["deduped"] is True
    assert len(events) == 1
    assert events[0]["event_id"].startswith("inbound_")
    assert events[0]["status"] == "received"
    assert "Olá inbound real" in serialized
    assert "evt-real-123" not in serialized
    assert "172185238905034@lid" not in serialized
    assert "+551****0000" not in serialized
    assert "secret-token" not in serialized
    assert "secret-key" not in serialized
