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


def test_sync_allowlist_from_env_upserts_aliases_without_raw_target_leak(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import (
        init_db,
        list_contacts,
        resolve_contact,
        sync_allowlist_from_env,
    )

    monkeypatch.delenv("CONTACTS_JSON", raising=False)
    monkeypatch.delenv("GROUPS_JSON", raising=False)
    monkeypatch.delenv("ALIAS_MAP_JSON", raising=False)
    raw_target = "551199998888@s.whatsapp.net"
    monkeypatch.setenv(
        "WHATSAPP_OPS_ALLOWLIST_CONTACTS_JSON",
        json.dumps(
            [
                {
                    "alias": "weslei_ctt_teste",
                    "target_ref": raw_target,
                    "kind": "contact",
                    "display_name": "Weslei Teste",
                    "allow_send": False,
                    "allow_receive": True,
                    "policy_group": "dm_test",
                }
            ]
        ),
    )
    monkeypatch.setenv("WHATSAPP_OPS_ALLOWLIST_GROUPS_JSON", "[]")
    monkeypatch.setenv("WHATSAPP_OPS_ALIAS_MAP_JSON", "{}")

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        synced = sync_allowlist_from_env()
        resolved = resolve_contact("weslei_ctt_teste")
        listed = list_contacts("weslei")
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps({"synced": synced, "resolved": resolved, "listed": listed}, ensure_ascii=False)
    assert synced == {"ok": True, "source": "env", "contacts_synced": 1, "groups_synced": 0}
    assert resolved["ok"] is True
    assert resolved["ambiguous"] is False
    assert resolved["match"]["contact_id"].startswith("contact_")
    assert resolved["match"]["whitelisted"] is False
    assert resolved["match"]["policy_group"] == "dm_test"
    assert listed[0]["display_name"] == "Weslei Teste"
    assert raw_target not in serialized
    assert "551199998888" not in serialized


def test_list_contacts_masks_legacy_raw_contact_ids(tmp_path):
    from tools.whatsapp_ops_store import init_db, list_contacts, resolve_contact, upsert_contact

    raw_contact_id = "172185238905034@lid"
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        upsert_contact(
            contact_id=raw_contact_id,
            display_name="Weslei Legacy",
            aliases=["weslei_ctt_teste"],
            whitelisted=False,
            policy_group="legacy_cache",
        )
        resolved = resolve_contact("weslei_ctt_teste")
        listed = list_contacts("weslei")
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps({"resolved": resolved, "listed": listed}, ensure_ascii=False)
    assert resolved["ok"] is True
    assert resolved["match"]["contact_id"].startswith("contact_")
    assert raw_contact_id not in serialized
    assert "@lid" not in serialized


def test_sync_allowlist_from_env_prefers_path_scoped_generic_keys(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import init_db, resolve_contact, sync_allowlist_from_env

    raw_target = "551177776666@s.whatsapp.net"
    monkeypatch.setenv(
        "CONTACTS_JSON",
        json.dumps(
            [
                {
                    "alias": "weslei_ctt_teste",
                    "target_ref": raw_target,
                    "display_name": "Weslei Teste Generic",
                    "allow_send": False,
                    "allow_receive": True,
                    "policy_group": "dm_test",
                }
            ]
        ),
    )
    monkeypatch.setenv("GROUPS_JSON", "[]")
    monkeypatch.setenv("ALIAS_MAP_JSON", "{}")
    # Legacy/global key exists but must not win over the path-scoped key.
    monkeypatch.setenv(
        "WHATSAPP_OPS_ALLOWLIST_CONTACTS_JSON",
        json.dumps(
            [
                {
                    "alias": "legacy_alias",
                    "target_ref": "551100000000@s.whatsapp.net",
                    "display_name": "Legacy Should Not Win",
                    "allow_send": True,
                }
            ]
        ),
    )

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        synced = sync_allowlist_from_env()
        resolved = resolve_contact("weslei_ctt_teste")
        legacy = resolve_contact("legacy_alias")
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps({"synced": synced, "resolved": resolved, "legacy": legacy}, ensure_ascii=False)
    assert synced == {"ok": True, "source": "env", "contacts_synced": 1, "groups_synced": 0}
    assert resolved["ambiguous"] is False
    assert resolved["match"]["display_name"] == "Weslei Teste Generic"
    assert legacy["ambiguous"] is True
    assert legacy["matches"] == []
    assert raw_target not in serialized
    assert "551177776666" not in serialized



def test_cockpit_overview_returns_sanitized_admin_queue(tmp_path):
    from tools.whatsapp_ops_store import (
        create_approval,
        create_draft,
        get_cockpit_overview,
        init_db,
        record_inbound_event,
        upsert_contact,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        upsert_contact(
            contact_id="172185238905034@lid",
            display_name="Weslei Legacy",
            aliases=["weslei_ctt_teste"],
            whitelisted=False,
            policy_group="dm_test",
        )
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "contact_safe", "display_name": "Safe"}],
            message="Mensagem cockpit",
        )
        approval = create_approval(draft["draft_id"])
        inbound = record_inbound_event(
            source_event_id="evt-real-123",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={"id": "evt-real-123", "from": "172185238905034@lid", "body": "Olá cockpit"},
        )
        overview = get_cockpit_overview(limit=5)
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(overview, ensure_ascii=False)
    assert overview["ok"] is True
    assert overview["counts"]["contacts"] == 1
    assert overview["counts"]["drafts_by_status"]["pending_approval"] == 1
    assert overview["counts"]["approvals_by_status"]["pending"] == 1
    assert overview["counts"]["inbound_events"] == 1
    assert overview["pending_approvals"][0]["approval_id"] == approval["approval_id"]
    assert overview["recent_inbound"][0]["event_id"] == inbound["event_id"]
    assert "@lid" not in serialized
    assert "@g.us" not in serialized
    assert "172185238905034" not in serialized
    assert "120363375521827492" not in serialized
    assert "evt-real-123" not in serialized


def test_thread_context_operator_mode_summarizes_local_store_without_raw_refs(tmp_path):
    from tools.whatsapp_ops_store import get_thread_context, init_db, record_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        record_inbound_event(
            source_event_id="ctx-real-001",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={
                "id": "ctx-real-001",
                "type": "text",
                "text": "Olá contexto com telefone 551199998888 e ref 172185238905034@lid",
                "mediaUrl": "https://cdn.example.invalid/a.jpg?token=secret",
            },
        )
        record_inbound_event(
            source_event_id="ctx-real-002",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={
                "id": "ctx-real-002",
                "message": {"audioMessage": {"mimetype": "audio/ogg", "seconds": 4, "url": "https://cdn.example.invalid/audio?token=secret"}},
            },
        )
        context = get_thread_context(
            thread="120363375521827492@g.us",
            mode="operator",
            limit=10,
            max_text_chars=80,
        )
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(context, ensure_ascii=False)
    assert context["ok"] is True
    assert context["source"] == "local_inbound_store"
    assert context["mode"] == "operator"
    assert context["message_count"] == 2
    assert context["type_counts"]["text"] == 1
    assert context["type_counts"]["audio"] == 1
    assert context["media_counts"]["audio"] == 1
    assert any(event.get("text_preview") for event in context["events"])
    assert any(event.get("media") for event in context["events"])
    assert "@lid" not in serialized
    assert "@g.us" not in serialized
    assert "172185238905034" not in serialized
    assert "120363375521827492" not in serialized
    assert "551199998888" not in serialized
    assert "ctx-real-001" not in serialized
    assert "cdn.example.invalid" not in serialized
    assert "secret" not in serialized


def test_sync_allowlist_from_env_fails_closed_on_malformed_json(tmp_path, monkeypatch):
    from tools.whatsapp_ops_store import init_db, list_contacts, sync_allowlist_from_env

    monkeypatch.delenv("CONTACTS_JSON", raising=False)
    monkeypatch.delenv("GROUPS_JSON", raising=False)
    monkeypatch.delenv("ALIAS_MAP_JSON", raising=False)
    monkeypatch.setenv("WHATSAPP_OPS_ALLOWLIST_CONTACTS_JSON", "not-json")
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        synced = sync_allowlist_from_env()
        contacts = list_contacts("")
    finally:
        reset_hermes_home_override(token)

    assert synced["ok"] is False
    assert synced["error"] == "allowlist_json_invalid"
    assert contacts == []
