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
    assert approval["approval_token"] not in rows[0][0]
    assert len(rows[0][0]) == 64
