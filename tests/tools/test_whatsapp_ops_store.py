import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

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
        "mission_envelopes",
        "autonomy_runs",
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
            "SELECT approval_token_hash, draft_idempotency_key FROM approvals WHERE draft_id=?",
            (draft["draft_id"],),
        ).fetchall()

    assert len(rows) == 1
    assert "approval_token" not in approval
    assert len(rows[0][0]) == 64
    assert rows[0][1] == draft["idempotency_key"]


def test_init_db_adds_draft_signature_to_legacy_approvals_table(tmp_path):
    from tools.whatsapp_ops_store import init_db

    token = set_hermes_home_override(tmp_path)
    try:
        db_path = tmp_path / "wpp_ops.sqlite"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE approvals (
                    id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL,
                    approval_token_hash TEXT NOT NULL UNIQUE,
                    approver_ref_hash TEXT,
                    message_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                )
                """
            )
        init_db()
        with sqlite3.connect(db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(approvals)")}
    finally:
        reset_hermes_home_override(token)

    assert "draft_idempotency_key" in columns


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
    assert "+551****0000" in serialized
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


def test_crm_binding_requires_explicit_binder_and_survives_contact_sync(tmp_path):
    from tools.whatsapp_ops_store import (
        bind_contact_crm_identity,
        get_contact_crm_binding,
        init_db,
        upsert_contact,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        upsert_contact(
            contact_id="contact_safe_01",
            display_name="Contato Seguro",
            metadata={
                "crm_lead_id": "L-INJECTED",
                "crm_contact_id": "C-INJECTED",
                "source": "allowlist",
            },
        )
        before_bind = get_contact_crm_binding("contact_safe_01")
        bound = bind_contact_crm_identity(
            contact_id="contact_safe_01",
            lead_id="L-001",
            crm_contact_id="C-001",
        )
        upsert_contact(
            contact_id="contact_safe_01",
            display_name="Contato Atualizado",
            metadata={
                "crm_lead_id": "L-ATTACK",
                "crm_contact_id": "C-ATTACK",
                "source": "runtime_config",
            },
        )
        after_sync = get_contact_crm_binding("contact_safe_01")
    finally:
        reset_hermes_home_override(token)

    assert before_bind is None
    assert bound == {"lead_id": "L-001", "contact_id": "C-001"}
    assert after_sync == bound


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


def test_staging_lid_and_group_refs_never_generate_fake_phone_mask(tmp_path):
    from tools.whatsapp_ops_store import init_db, peek_staging, stage_raw_ref

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        stage_raw_ref(
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            display_name="Weslei W.",
            kind="contact",
            safe_hint={
                "participant_name": "Weslei W.",
                "source_group_name": "H-Ops",
                "last_message_type": "text",
                # Legacy unsafe fields from old rows must be ignored when no
                # trusted phone-bearing source accompanies them.
                "phone_masked": "+17***5034",
                "last4": "5034",
            },
        )
        stage_raw_ref(
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            display_name="H-Ops",
            kind="group",
            safe_hint={"group_name": "H-Ops", "last_message_type": "text"},
        )
        staged = peek_staging()
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(staged, ensure_ascii=False)
    contact = next(item for item in staged if item["kind"] == "contact")
    group = next(item for item in staged if item["kind"] == "group")
    assert contact["display_name"] == "Weslei W."
    assert contact["phone_status"] == "unresolved"
    assert contact["identity_note"] == "número não resolvido"
    assert "phone_masked" not in contact
    assert "last4" not in contact
    assert "phone_masked" not in group
    assert "last4" not in group
    assert "+17***5034" not in serialized
    assert "172185238905034" not in serialized
    assert "120363375521827492" not in serialized
    assert "@lid" not in serialized
    assert "@g.us" not in serialized


def test_staging_lid_contact_can_show_safe_mask_from_matching_local_context(tmp_path):
    from tools.whatsapp_ops_store import init_db, peek_staging, record_inbound_event, stage_raw_ref

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        record_inbound_event(
            source_event_id="ctx-phone-001",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={
                "id": "ctx-phone-001",
                "chat": {"id": "120363375521827492@g.us", "title": "H-Ops"},
                "participant": {"id": "172185238905034@lid", "phone": "553199993111", "title": "Weslei W."},
                "text": "Teste",
                "type": "text",
            },
        )
        stage_raw_ref(
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            display_name="Weslei W.",
            kind="contact",
            safe_hint={"participant_name": "Weslei W.", "source_group_name": "H-Ops", "last_message_type": "text"},
        )
        staged = peek_staging()
    finally:
        reset_hermes_home_override(token)

    contact = next(item for item in staged if item["kind"] == "contact")
    serialized = json.dumps(staged, ensure_ascii=False)
    assert contact["phone_masked"] == "+55***3111"
    assert contact["last4"] == "3111"
    assert "phone_status" not in contact
    assert "553199993111" not in serialized
    assert "172185238905034" not in serialized
    assert "120363375521827492" not in serialized
    assert "@lid" not in serialized
    assert "@g.us" not in serialized


def test_staging_phone_bearing_refs_still_mask_contacts(tmp_path):
    from tools.whatsapp_ops_store import init_db, peek_staging, stage_raw_ref

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        stage_raw_ref(
            contact_ref="553199998765@s.whatsapp.net",
            thread_ref="120363375521827492@g.us",
            display_name="João Cliente",
            kind="contact",
            safe_hint={"participant_name": "João Cliente", "last_message_type": "text"},
        )
        staged = peek_staging()
    finally:
        reset_hermes_home_override(token)

    contact = next(item for item in staged if item["kind"] == "contact")
    serialized = json.dumps(staged, ensure_ascii=False)
    assert contact["phone_masked"] == "+55***8765"
    assert contact["last4"] == "8765"
    assert "phone_status" not in contact
    assert "553199998765" not in serialized
    assert "@s.whatsapp.net" not in serialized


def test_resolve_conversation_target_by_registered_group_and_queue_item_without_raw_refs(tmp_path):
    from tools.whatsapp_ops_store import (
        init_db,
        record_inbound_event,
        register_group_local,
        resolve_conversation_target,
        stage_raw_ref,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        raw_group = "120363375521827492@g.us"
        raw_contact = "172185238905034@lid"
        register_group_local(alias="H-Ops", raw_ref=raw_group, allow_send=False)
        record_inbound_event(
            source_event_id="resolve-target-001",
            contact_ref=raw_contact,
            thread_ref=raw_group,
            payload={"id": "resolve-target-001", "type": "text", "text": "Teste H-Ops"},
        )
        registered = resolve_conversation_target(query="H-Ops")
        private_registered = resolve_conversation_target(query="H-Ops", include_transport=True)

        stage_raw_ref(
            contact_ref="551199998888@s.whatsapp.net",
            thread_ref="120363000000000000@g.us",
            display_name="Novo Grupo",
            kind="group",
            safe_hint={"group_name": "Novo Grupo", "last_message_type": "text"},
        )
        queued = resolve_conversation_target(item_index=1)
        private_queued = resolve_conversation_target(item_index=1, include_transport=True)
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps([registered, queued], ensure_ascii=False)
    assert registered["ok"] is True
    assert registered["ambiguous"] is False
    assert registered["target_kind"] == "group"
    assert registered["target_label"] == "H-Ops"
    assert registered["thread_filter_set"] is True
    assert private_registered["_thread_ref"] == raw_group
    assert queued["ok"] is True
    assert queued["target_kind"] == "group"
    assert queued["source"] == "queue"
    assert queued["target_label"] == "Novo Grupo"
    assert private_queued["_thread_ref"] == "120363000000000000@g.us"
    assert "@g.us" not in serialized
    assert "@lid" not in serialized
    assert "120363375521827492" not in serialized
    assert "172185238905034" not in serialized
    assert "551199998888" not in serialized


def test_resolve_conversation_target_item_can_reference_recent_context_without_raw_refs(tmp_path):
    from tools.whatsapp_ops_store import init_db, record_inbound_event, resolve_conversation_target

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        record_inbound_event(
            source_event_id="resolve-context-item-001",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={"id": "resolve-context-item-001", "type": "text", "text": "Teste contexto"},
        )
        result = resolve_conversation_target(item_index=1)
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is True
    assert result["target_kind"] == "context"
    assert result["source"] == "queue_context"
    assert result["thread_filter_set"] is False
    assert result["contact_filter_set"] is False
    assert "@g.us" not in serialized
    assert "@lid" not in serialized
    assert "120363375521827492" not in serialized
    assert "172185238905034" not in serialized


def test_resolve_conversation_target_reports_ambiguity_without_raw_refs(tmp_path):
    from tools.whatsapp_ops_store import init_db, register_group_local, resolve_conversation_target

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        register_group_local(alias="Duplicado A", raw_ref="120363000000000001@g.us", allow_send=False)
        register_group_local(alias="Duplicado B", raw_ref="120363000000000002@g.us", allow_send=False)
        result = resolve_conversation_target(query="Duplicado")
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is True
    assert result["ambiguous"] is True
    assert len(result["matches"]) == 2
    assert "@g.us" not in serialized
    assert "120363000000000001" not in serialized
    assert "120363000000000002" not in serialized


def test_staging_hides_refs_already_registered_locally(tmp_path):
    from tools.whatsapp_ops_store import (
        init_db,
        peek_staging,
        register_contact_local,
        register_group_local,
        registration_staging_diagnostics,
        stage_raw_ref,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        contact_ref = "551199998888@s.whatsapp.net"
        group_ref = "120363375521827492@g.us"
        register_contact_local(alias="Lead já cadastrado", raw_ref=contact_ref, allow_send=True)
        register_group_local(alias="Grupo já cadastrado", raw_ref=group_ref, allow_send=False)
        stage_raw_ref(
            contact_ref=contact_ref,
            thread_ref=group_ref,
            display_name="Lead já cadastrado",
            kind="contact",
            safe_hint={"display_name": "Lead já cadastrado", "last_message_type": "text"},
        )
        stage_raw_ref(
            contact_ref=group_ref,
            thread_ref=group_ref,
            display_name="Grupo já cadastrado",
            kind="group",
            safe_hint={"group_name": "Grupo já cadastrado", "last_message_type": "text"},
        )
        visible_staging = peek_staging()
        diagnostics = registration_staging_diagnostics()
    finally:
        reset_hermes_home_override(token)

    assert visible_staging == []
    assert diagnostics["staged_count"] == 0


def test_staging_keeps_same_name_when_provider_ref_is_different(tmp_path):
    from tools.whatsapp_ops_store import init_db, peek_staging, register_group_local, stage_raw_ref

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        register_group_local(
            alias="H-Ops",
            raw_ref="120363111111111111@g.us",
            allow_send=False,
        )
        stage_raw_ref(
            contact_ref="120363222222222222@g.us",
            thread_ref="120363222222222222@g.us",
            display_name="H-Ops",
            kind="group",
            safe_hint={"group_name": "H-Ops", "last_message_type": "text"},
        )
        staged = peek_staging()
    finally:
        reset_hermes_home_override(token)

    assert len(staged) == 1
    assert staged[0]["kind"] == "group"
    assert staged[0]["display_name"] == "H-Ops"


def test_actionable_queue_combines_staging_approvals_and_context_without_raw_refs(tmp_path):
    from tools.whatsapp_ops_store import (
        create_approval,
        create_draft,
        get_actionable_queue,
        ignore_staging_item,
        init_db,
        peek_staging,
        record_inbound_event,
        registration_staging_diagnostics,
        stage_raw_ref,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "contact_safe", "display_name": "Safe"}],
            message="Mensagem que não deve aparecer na fila",
        )
        approval = create_approval(draft["draft_id"])
        inbound = record_inbound_event(
            source_event_id="queue-real-001",
            contact_ref="551199998888@s.whatsapp.net",
            thread_ref="120363375521827492@g.us",
            payload={
                "id": "queue-real-001",
                "type": "audio",
                "text": "Contato mandou áudio com telefone 551199998888 e https://cdn.example.invalid/a.ogg?token=secret",
                "message": {"audioMessage": {"mimetype": "audio/ogg", "seconds": 6}},
            },
        )
        stage_raw_ref(
            contact_ref="551199998888@s.whatsapp.net",
            thread_ref="120363375521827492@g.us",
            display_name="Lead Teste",
            kind="contact",
            safe_hint={"display_name": "Lead Teste", "last_message_type": "audio"},
        )
        system_event = record_inbound_event(
            source_event_id="queue-system-001",
            contact_ref="system@s.whatsapp.net",
            thread_ref="system@g.us",
            payload={"type": "system", "text": "Internal System Message"},
        )
        stage_raw_ref(
            contact_ref="system@s.whatsapp.net",
            thread_ref="system@g.us",
            display_name="Internal System Message",
            kind="contact",
            safe_hint={"display_name": "Internal System Message", "last_message_type": "system"},
        )
        visible_staging = peek_staging()
        diagnostics = registration_staging_diagnostics()
        queue = get_actionable_queue(limit=5)
        ignored = ignore_staging_item(item_index=1)
        queue_after_ignore = get_actionable_queue(limit=5)
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(queue, ensure_ascii=False)
    kinds = {item["kind"] for item in queue["items"]}
    assert queue["ok"] is True
    assert queue["read_only"] is True
    assert queue["send_performed"] is False
    assert queue["source"] == "local_inbound_store"
    assert "approval" in kinds
    assert "registration" in kinds
    assert "context" in kinds
    assert [item["kind"] for item in visible_staging] == ["contact"]
    assert visible_staging[0]["display_name"] == "Lead Teste"
    assert diagnostics["staged_count"] == 1
    assert diagnostics["latest_inbound_created_at"] != system_event.get("created_at")
    assert ignored["ok"] is True
    assert ignored["display_name"] == "Lead Teste"
    assert queue["counts"]["total"] == len(queue["items"])
    assert queue["counts"]["active_staging"] == 1
    assert queue_after_ignore["counts"]["active_staging"] == 0
    assert queue["operator_summary"]["latest_inbound_created_at"]
    assert queue["operator_summary"]["latest_inbound_created_at"] != system_event.get("created_at")
    assert any(item.get("approval_id") == approval["approval_id"] for item in queue["items"])
    assert any(item.get("safe_event_id") == inbound["event_id"] for item in queue["items"])
    assert not any(item.get("message_type") == "system" for item in queue["items"])
    assert any("/addct" in " ".join(item.get("actions", [])) for item in queue["items"])
    assert queue.get("operator_summary", {}).get("headline")
    assert queue.get("operator_summary", {}).get("best_next_action")
    actionable = [item for item in queue["items"] if item.get("kind") in {"approval", "registration", "context"}]
    assert all(item.get("operator_state") for item in actionable)
    assert all(item.get("operator_title") for item in actionable)
    assert all(item.get("primary_action") for item in actionable)
    registration = next(item for item in queue["items"] if item.get("kind") == "registration")
    assert registration["operator_state"] == "ACTION_REQUIRED"
    assert registration["primary_action"].startswith("/addct Lead Teste --item")
    assert registration["safe_origin"]
    assert "follow-up comercial" in registration["why_it_matters"]
    context = next(item for item in queue["items"] if item.get("kind") == "context")
    assert context["safe_preview"]
    assert context["primary_action"] == "/ctxwpp"
    assert "@s.whatsapp.net" not in serialized
    assert "@g.us" not in serialized
    assert "551199998888" not in serialized
    assert "120363375521827492" not in serialized
    assert "queue-real-001" not in serialized
    assert "queue-system-001" not in serialized
    assert "Internal System Message" not in serialized
    assert "cdn.example.invalid" not in serialized
    assert "secret" not in serialized
    assert "Mensagem que não deve aparecer" not in serialized


def test_actionable_queue_hides_stale_context_and_expired_approvals(tmp_path):
    from tools.whatsapp_ops_store import (
        create_approval,
        create_draft,
        get_actionable_queue,
        get_db_path,
        init_db,
        record_inbound_event,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        draft = create_draft(
            targets=[{"type": "contact", "contact_id": "contact_safe", "display_name": "Safe"}],
            message="Mensagem antiga",
        )
        create_approval(draft["draft_id"], timeout_minutes=1)
        record_inbound_event(
            source_event_id="stale-personal-001",
            contact_ref="551199998888@s.whatsapp.net",
            thread_ref="551199998888@s.whatsapp.net",
            payload={"id": "stale-personal-001", "type": "text", "text": "conversa pessoal antiga"},
        )
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        with sqlite3.connect(get_db_path()) as conn:
            conn.execute("UPDATE inbound_events SET created_at=?", (old,))
            conn.execute("UPDATE approvals SET expires_at=?", (old,))
        queue = get_actionable_queue(limit=5)
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(queue, ensure_ascii=False)
    assert queue["ok"] is True
    assert queue["counts"]["pending_approvals"] == 0
    assert queue["counts"]["expired_pending_approvals"] == 1
    assert queue["counts"]["context_items"] == 0
    assert "stale_local_inbound_store" in queue["warnings"]
    assert "expired_pending_approvals_hidden" in queue["warnings"]
    assert "conversa pessoal antiga" not in serialized
    assert "stale-personal-001" not in serialized


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


def test_conversation_summary_hides_system_events_from_operational_counts(tmp_path):
    from tools.whatsapp_ops_store import get_conversation_summary, init_db, record_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        record_inbound_event(
            source_event_id="summary-text-001",
            contact_ref="172185238905034@lid",
            thread_ref="120363375521827492@g.us",
            payload={"id": "summary-text-001", "type": "text", "text": "Mensagem comercial útil"},
        )
        record_inbound_event(
            source_event_id="summary-system-001",
            contact_ref="system@s.whatsapp.net",
            thread_ref="120363375521827492@g.us",
            payload={"id": "summary-system-001", "type": "system", "message": "Internal System Message"},
        )
        summary = get_conversation_summary(
            thread="120363375521827492@g.us",
            mode="brief",
            limit=10,
            max_text_chars=120,
        )
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(summary, ensure_ascii=False)
    assert summary["ok"] is True
    assert summary["message_count"] == 1
    assert summary["hidden_system_events"] == 1
    assert "system_events_hidden" in summary["warnings"]
    assert summary["type_counts"] == {"text": 1}
    assert "system" not in summary["type_counts"]
    assert "Internal System Message" not in serialized
    assert "summary-system-001" not in serialized
    assert "@g.us" not in serialized
    assert "@lid" not in serialized
    assert "120363375521827492" not in serialized
    assert "172185238905034" not in serialized


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


def test_sanitize_payload_redacts_data_blob_base64_and_media_tokens(tmp_path):
    """Verify _sanitize_payload redacts data:/blob: URLs, long base64,
    jpegThumbnail, thumbnail fields, mediaKey/directPath/fileSha256
    fields, and base64/blob field names from serialized JSON."""
    from tools.whatsapp_ops_store import _sanitize_payload, init_db, record_inbound_event

    payload = {
        "text": "Mensagem normal",
        "dataUrl": "data:audio/ogg;base64,T3J0aVNwZWNpZmljVGVzdERhdGFVUkw=",
        "blobUrl": "blob:https://example.com/a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "jpegThumbnail": "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAg...base64ThumbnailData",
        "thumbnail": {"url": "https://cdn.example.invalid/thumb.jpg", "width": 100},
        "mediaKey": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "directPath": "/v/media/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "fileSha256": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "fileEncSha256": "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "base64": "T3J0aVNwZWNpZmljVGVzdEJhc2U2NA==",
        "blob": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "longBase64String": "T3J0aVNwZWNpZmljVGVzdExvbmdCYXNlNjRTdHJpbmc=",
        "nested": {
            "mediaKey": "nest_media_key_value",
            "directPath": "/nest/media/path",
            "jpegThumbnail": "nest_thumb_data_base64",
        },
        "innocentField": "Olá mundo",
    }

    safe = _sanitize_payload(payload)
    serialized = json.dumps(safe, ensure_ascii=False)

    # Normal text must survive
    assert "Mensagem normal" in serialized
    assert "Olá mundo" in serialized

    # data: and blob: URLs must be redacted (values replaced)
    assert "data:audio/ogg;base64" not in serialized
    assert "blob:https://example.com" not in serialized
    assert "T3J0aVNwZWNpZmljVGVzdERhdGFVUkw=" not in serialized
    assert "a1b2c3d4-e5f6-7890-abcd-ef1234567890" not in serialized

    # jpegThumbnail value must be redacted (the key name persists in JSON)
    assert "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAg" not in serialized
    assert "base64ThumbnailData" not in serialized

    # thumbnail object values must be redacted (thumbnail key value is replaced)
    assert "cdn.example.invalid" not in serialized

    # mediaKey / directPath / fileSha256 / fileEncSha256 values must be redacted
    assert "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4" not in serialized
    assert "/v/media/" not in serialized
    assert "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4" not in serialized

    # base64 and blob field values must be redacted
    assert "T3J0aVNwZWNpZmljVGVzdEJhc2U2NA==" not in serialized
    assert '"a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"' not in serialized  # blob value as JSON string

    # Long bare base64 string value must be redacted
    assert "T3J0aVNwZWNpZmljVGVzdExvbmdCYXNlNjRTdHJpbmc=" not in serialized

    # Nested mediaKey value must be redacted
    assert "nest_media_key_value" not in serialized
    assert "/nest/media/path" not in serialized
    assert "nest_thumb_data_base64" not in serialized

    # Verify redacted placeholders appear
    assert "<redacted>" in serialized
    assert "<redacted-url>" in serialized
    assert "<redacted-base64>" in serialized

    # Verify it works end-to-end through record_inbound_event + lookup
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        result = record_inbound_event(
            source_event_id="sanitize-e2e-001",
            contact_ref="sanitize_test@lid",
            thread_ref="sanitize_thread@g.us",
            payload=payload,
        )
        from tools.whatsapp_ops_store import lookup_inbound_events
        stored = lookup_inbound_events(contact="sanitize_test@lid", limit=5)
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    stored_json = json.dumps(stored, ensure_ascii=False)
    # The sensitive values must not leak into the stored/retrieved JSON
    assert "data:audio/ogg;base64" not in stored_json
    assert "blob:https://example.com" not in stored_json
    assert "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAg" not in stored_json
    assert "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4" not in stored_json
    assert "/v/media/" not in stored_json
    assert "nest_media_key_value" not in stored_json
    assert "T3J0aVNwZWNpZmljVGVzdExvbmdCYXNlNjRTdHJpbmc=" not in stored_json


def test_inbound_burst_status_groups_recent_messages_without_raw_refs(tmp_path):
    from tools.whatsapp_ops_store import (
        get_inbound_burst_status,
        init_db,
        record_inbound_event,
    )

    raw_group = "burst_thread_synthetic"
    raw_contact = "burst_contact_synthetic"
    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        for idx, text in enumerate(("oi", "tenho uma dúvida", "pode ajudar?"), 1):
            assert record_inbound_event(
                source_event_id=f"BURST_REAL_{idx}",
                contact_ref=raw_contact,
                thread_ref=raw_group,
                payload={"id": f"BURST_REAL_{idx}", "type": "text", "text": text},
            )["ok"] is True
        # System events must not become operator-facing burst evidence.
        assert record_inbound_event(
            source_event_id="BURST_SYSTEM_001",
            contact_ref=raw_contact,
            thread_ref=raw_group,
            payload={"id": "BURST_SYSTEM_001", "type": "system", "text": "internal"},
        )["ok"] is True
        status = get_inbound_burst_status(
            thread=raw_group,
            limit=20,
            window_seconds=60,
            quorum=2,
            max_text_chars=80,
        )
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(status, ensure_ascii=False)
    assert status["ok"] is True
    assert status["read_only"] is True
    assert status["draft_created"] is False
    assert status["send_performed"] is False
    assert status["provider_history_used"] is False
    assert status["coalescing"]["window_seconds"] == 60
    assert status["coalescing"]["quorum"] == 2
    assert status["counts"]["events_considered"] == 3
    assert status["counts"]["quorum_bursts"] == 1
    burst = status["bursts"][0]
    assert burst["state"] == "quorum"
    assert burst["message_count"] == 3
    assert burst["primary_action"] == "review_coalesced_context"
    assert [event["text_preview"] for event in burst["events"]] == [
        "oi",
        "tenho uma dúvida",
        "pode ajudar?",
    ]
    assert "system" not in serialized
    assert "BURST_REAL" not in serialized
    assert raw_group not in serialized
    assert raw_contact not in serialized


def test_inbound_burst_status_collecting_single_message_without_draft(tmp_path):
    from tools.whatsapp_ops_store import get_inbound_burst_status, init_db, record_inbound_event

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        assert record_inbound_event(
            source_event_id="BURST_SINGLE_001",
            contact_ref="lead_single_ref",
            thread_ref="thread_single_ref",
            payload={"id": "BURST_SINGLE_001", "type": "text", "text": "mensagem única"},
        )["ok"] is True
        status = get_inbound_burst_status(limit=10, window_seconds=60, quorum=3)
    finally:
        reset_hermes_home_override(token)

    assert status["ok"] is True
    assert status["counts"]["collecting_bursts"] == 1
    assert status["counts"]["quorum_bursts"] == 0
    assert status["bursts"][0]["state"] == "collecting"
    assert status["bursts"][0]["primary_action"] == "wait_for_more_inbound"
    assert status["draft_created"] is False


def test_mission_ledger_migration_is_idempotent_atomic_and_preserves_outer_transaction():
    from tools.whatsapp_ops_store import _migrate_mission_ledger

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE caller_state (value TEXT)")
        conn.execute("INSERT INTO caller_state VALUES ('pending')")
        assert conn.in_transaction is True
        _migrate_mission_ledger(conn)
        _migrate_mission_ledger(conn)
        assert conn.in_transaction is True
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"mission_envelopes", "autonomy_runs"}.issubset(tables)
        assert not any("commercial" in name for name in tables)
    finally:
        conn.close()

    class FailingConnection(sqlite3.Connection):
        failed = False

        def execute(self, sql, parameters=()):
            if "CREATE TABLE IF NOT EXISTS autonomy_runs" in sql and not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("synthetic migration failure")
            return super().execute(sql, parameters)

    failing = sqlite3.connect(":memory:", factory=FailingConnection)
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic migration failure"):
            _migrate_mission_ledger(failing)
        tables_after_failure = {
            row[0]
            for row in failing.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "mission_envelopes" not in tables_after_failure
        assert "autonomy_runs" not in tables_after_failure
        _migrate_mission_ledger(failing)
        tables_after_retry = {
            row[0]
            for row in failing.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"mission_envelopes", "autonomy_runs"}.issubset(tables_after_retry)
    finally:
        failing.close()


def test_mission_envelope_run_idempotency_fence_and_safe_completion(tmp_path):
    from tools.whatsapp_ops_store import (
        complete_autonomy_run,
        register_mission_envelope,
        reserve_autonomy_run,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        envelope = register_mission_envelope(
            project_ref="project_demo_01",
            sales_pack_digest="a" * 64,
            mode="safe_auto",
            max_items=3,
            max_local_writes=1,
            window_ref="window_demo_01",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        first = reserve_autonomy_run(
            envelope["envelope_digest"],
            "run_demo_01",
            requires_local_write=True,
        )
        duplicate = reserve_autonomy_run(
            envelope["envelope_digest"],
            "run_demo_01",
            requires_local_write=True,
        )
        stale = complete_autonomy_run(
            "run_demo_01",
            "wrong_fence_01",
            counters={"items": 2, "local_writes": 1},
            result_class="success",
        )
        over_caps = complete_autonomy_run(
            "run_demo_01",
            first["fence"],
            counters={"items": 4, "local_writes": 1},
            result_class="success",
        )
        completed = complete_autonomy_run(
            "run_demo_01",
            first["fence"],
            counters={"items": 2, "local_writes": 1},
            result_class="success",
        )
        with sqlite3.connect(tmp_path / "wpp_ops.sqlite") as conn:
            run_count = conn.execute(
                "SELECT COUNT(*) FROM autonomy_runs WHERE run_key='run_demo_01'"
            ).fetchone()[0]
            stored_payload = conn.execute(
                "SELECT payload_json FROM mission_envelopes WHERE envelope_digest=?",
                (envelope["envelope_digest"],),
            ).fetchone()[0]
            stored_fence_hash = conn.execute(
                "SELECT fence_hash FROM autonomy_runs WHERE run_key='run_demo_01'"
            ).fetchone()[0]
    finally:
        reset_hermes_home_override(token)

    assert envelope["ok"] is True
    assert first["ok"] is True
    assert first["status"] == "started"
    assert first["fence"]
    assert duplicate["ok"] is True
    assert duplicate["deduped"] is True
    assert "fence" not in duplicate
    assert stale == {"ok": False, "error": "autonomy_run_fence_stale"}
    assert over_caps == {"ok": False, "error": "autonomy_run_caps_exceeded"}
    assert completed["ok"] is True
    assert completed["status"] == "completed"
    assert completed["counters"] == {"items": 2, "local_writes": 1}
    assert completed["result_class"] == "success"
    assert run_count == 1
    assert first["fence"] not in stored_fence_hash
    assert len(stored_fence_hash) == 64
    assert set(json.loads(stored_payload)) == {
        "active",
        "caps",
        "expires_at",
        "kill_switch",
        "mode",
        "project_ref",
        "sales_pack_digest",
        "window_ref",
    }


def test_mission_envelope_caps_are_bounded_to_dossier_and_local_queue_scale(tmp_path):
    from tools.whatsapp_ops_store import register_mission_envelope

    token = set_hermes_home_override(tmp_path)
    try:
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        boundary = register_mission_envelope(
            project_ref="project_caps_01",
            sales_pack_digest="f" * 64,
            mode="safe_auto",
            max_items=50,
            max_local_writes=5,
            window_ref="window_caps_01",
            expires_at=expires_at,
        )
        too_many_items = register_mission_envelope(
            project_ref="project_caps_02",
            sales_pack_digest="1" * 64,
            mode="safe_auto",
            max_items=51,
            max_local_writes=5,
            window_ref="window_caps_02",
            expires_at=expires_at,
        )
        too_many_writes = register_mission_envelope(
            project_ref="project_caps_03",
            sales_pack_digest="2" * 64,
            mode="safe_auto",
            max_items=50,
            max_local_writes=6,
            window_ref="window_caps_03",
            expires_at=expires_at,
        )
    finally:
        reset_hermes_home_override(token)

    assert boundary["ok"] is True
    assert boundary["max_items"] == 50
    assert boundary["max_local_writes"] == 5
    assert too_many_items == {"ok": False, "error": "mission_envelope_caps_invalid"}
    assert too_many_writes == {"ok": False, "error": "mission_envelope_caps_invalid"}


def test_mission_envelope_kill_expiry_changed_digest_and_lease_fail_closed(tmp_path, monkeypatch):
    import tools.whatsapp_ops_store as store

    token = set_hermes_home_override(tmp_path)
    try:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        killed_envelope = store.register_mission_envelope(
            project_ref="project_kill_01",
            sales_pack_digest="b" * 64,
            mode="safe_auto",
            max_items=2,
            max_local_writes=1,
            window_ref="window_kill_01",
            expires_at=expires_at.isoformat(),
        )
        active_run = store.reserve_autonomy_run(
            killed_envelope["envelope_digest"], "run_kill_01"
        )
        killed = store.kill_mission_envelope(killed_envelope["envelope_digest"])
        blocked_completion = store.complete_autonomy_run(
            "run_kill_01",
            active_run["fence"],
            counters={"items": 0, "local_writes": 0},
            result_class="policy_blocked",
        )

        expiring_envelope = store.register_mission_envelope(
            project_ref="project_expiry_01",
            sales_pack_digest="c" * 64,
            mode="assist",
            max_items=1,
            max_local_writes=0,
            window_ref="window_expiry_01",
            expires_at=expires_at.isoformat(),
        )
        local_write_denied = store.reserve_autonomy_run(
            expiring_envelope["envelope_digest"],
            "run_local_write_denied_01",
            requires_local_write=True,
        )
        monkeypatch.setattr(
            store,
            "utc_now",
            lambda: (expires_at + timedelta(seconds=1)).isoformat(),
        )
        expired = store.reserve_autonomy_run(
            expiring_envelope["envelope_digest"], "run_expiry_01"
        )
        monkeypatch.undo()

        changed_expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        changed_envelope = store.register_mission_envelope(
            project_ref="project_changed_01",
            sales_pack_digest="d" * 64,
            mode="assist",
            max_items=1,
            max_local_writes=0,
            window_ref="window_changed_01",
            expires_at=changed_expires_at,
        )
        with sqlite3.connect(tmp_path / "wpp_ops.sqlite") as conn:
            conn.execute(
                "UPDATE mission_envelopes SET project_ref='project_tampered_01' "
                "WHERE envelope_digest=?",
                (changed_envelope["envelope_digest"],),
            )
        changed = store.reserve_autonomy_run(
            changed_envelope["envelope_digest"], "run_changed_01"
        )
        changed_readback = store.register_mission_envelope(
            project_ref="project_changed_01",
            sales_pack_digest="d" * 64,
            mode="assist",
            max_items=1,
            max_local_writes=0,
            window_ref="window_changed_01",
            expires_at=changed_expires_at,
        )
        unknown = store.reserve_autonomy_run("0" * 64, "run_unknown_01")

        lease_envelope = store.register_mission_envelope(
            project_ref="project_lease_01",
            sales_pack_digest="e" * 64,
            mode="assist",
            max_items=1,
            max_local_writes=0,
            window_ref="window_lease_01",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        leased = store.reserve_autonomy_run(
            lease_envelope["envelope_digest"], "run_lease_01"
        )
        with sqlite3.connect(tmp_path / "wpp_ops.sqlite") as conn:
            conn.execute(
                "UPDATE autonomy_runs SET lease_expires_at=? WHERE run_key='run_lease_01'",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )
        expired_duplicate = store.reserve_autonomy_run(
            lease_envelope["envelope_digest"], "run_lease_01"
        )
        renewed = store.reserve_autonomy_run(
            lease_envelope["envelope_digest"],
            "run_lease_01",
            renew_expired=True,
        )
        old_fence = store.complete_autonomy_run(
            "run_lease_01",
            leased["fence"],
            counters={"items": 0, "local_writes": 0},
            result_class="no_op",
        )
    finally:
        reset_hermes_home_override(token)

    assert killed["ok"] is True
    assert killed["killed"] is True
    assert blocked_completion == {"ok": False, "error": "mission_envelope_killed"}
    assert local_write_denied == {
        "ok": False,
        "error": "mission_envelope_local_write_denied",
    }
    assert expired == {"ok": False, "error": "mission_envelope_expired"}
    assert changed == {"ok": False, "error": "mission_envelope_changed"}
    assert changed_readback == {"ok": False, "error": "mission_envelope_changed"}
    assert unknown == {"ok": False, "error": "mission_envelope_not_found"}
    assert expired_duplicate["ok"] is True
    assert expired_duplicate["deduped"] is True
    assert expired_duplicate["lease_expired"] is True
    assert "fence" not in expired_duplicate
    assert renewed["ok"] is True
    assert renewed["renewed"] is True
    assert renewed["fence"] != leased["fence"]
    assert old_fence == {"ok": False, "error": "autonomy_run_fence_stale"}


def test_inspect_mission_envelope_is_exact_sanitized_and_read_only(tmp_path):
    from tools.whatsapp_ops_store import (
        get_db_path,
        inspect_mission_envelope,
        register_mission_envelope,
    )

    token = set_hermes_home_override(tmp_path)
    try:
        envelope = register_mission_envelope(
            project_ref="project_inspect_01",
            sales_pack_digest="9" * 64,
            mode="safe_auto",
            max_items=4,
            max_local_writes=2,
            window_ref="window_inspect_01",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        db = get_db_path()
        def snapshot():
            with sqlite3.connect(db) as conn:
                schema = conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()
                tables = [row[1] for row in schema if row[0] == "table"]
                row_counts = {}
                for table in tables:
                    quoted = '"' + table.replace('"', '""') + '"'
                    row_counts[table] = conn.execute(
                        f"SELECT COUNT(*) FROM {quoted}"
                    ).fetchone()[0]
            return {
                "file_hash": hashlib.sha256(db.read_bytes()).hexdigest(),
                "schema_hash": hashlib.sha256(
                    json.dumps(schema, ensure_ascii=False).encode("utf-8")
                ).hexdigest(),
                "tables": tables,
                "row_counts": row_counts,
            }

        before = snapshot()
        inspected = inspect_mission_envelope(envelope["envelope_digest"])
        after = snapshot()
    finally:
        reset_hermes_home_override(token)

    assert inspected == envelope
    assert before == after
    assert set(inspected) == {
        "ok",
        "envelope_digest",
        "project_ref",
        "sales_pack_digest",
        "mode",
        "max_items",
        "max_local_writes",
        "window_ref",
        "expires_at",
        "active",
        "killed",
    }


def test_inspect_mission_envelope_missing_store_is_fail_closed_without_creating_db(tmp_path):
    from tools.whatsapp_ops_store import get_db_path, inspect_mission_envelope

    token = set_hermes_home_override(tmp_path)
    try:
        db = get_db_path()
        assert not db.exists()
        inspected = inspect_mission_envelope("a" * 64)
        still_absent = not db.exists()
    finally:
        reset_hermes_home_override(token)

    assert inspected == {
        "ok": False,
        "error": "mission_envelope_store_unavailable",
    }
    assert still_absent is True


def test_inspect_mission_envelope_legacy_store_is_fail_closed_and_byte_unchanged(tmp_path):
    from tools.whatsapp_ops_store import get_db_path, inspect_mission_envelope

    token = set_hermes_home_override(tmp_path)
    try:
        db = get_db_path()
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE legacy_rows (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO legacy_rows (value) VALUES (?)", ("legacy-value",))
        before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
        with sqlite3.connect(db) as conn:
            before_schema = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            before_rows = conn.execute("SELECT id, value FROM legacy_rows").fetchall()

        inspected = inspect_mission_envelope("b" * 64)

        after_hash = hashlib.sha256(db.read_bytes()).hexdigest()
        with sqlite3.connect(db) as conn:
            after_schema = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            after_rows = conn.execute("SELECT id, value FROM legacy_rows").fetchall()
    finally:
        reset_hermes_home_override(token)

    assert inspected == {
        "ok": False,
        "error": "mission_envelope_store_unavailable",
    }
    assert after_hash == before_hash
    assert after_schema == before_schema
    assert after_rows == before_rows


def test_atomic_autonomy_write_rechecks_lease_after_acquiring_write_lock(tmp_path, monkeypatch):
    import tools.whatsapp_ops_store as store

    token = set_hermes_home_override(tmp_path)
    try:
        base = datetime.now(timezone.utc)
        envelope = store.register_mission_envelope(
            project_ref="project_atomic_lease_01",
            sales_pack_digest="7" * 64,
            mode="safe_auto",
            max_items=1,
            max_local_writes=1,
            window_ref="window_atomic_lease_01",
            expires_at=(base + timedelta(hours=1)).isoformat(),
        )
        run = store.reserve_autonomy_run(
            envelope["envelope_digest"],
            "run_atomic_lease_01",
            requires_local_write=True,
        )
        with sqlite3.connect(store.get_db_path()) as conn:
            conn.execute(
                "UPDATE autonomy_runs SET lease_expires_at=? WHERE run_key=?",
                ((base + timedelta(seconds=1)).isoformat(), "run_atomic_lease_01"),
            )

        original_connect = store._connect
        fake_now = {"value": base}

        class LockAdvancingConnection:
            def __init__(self, conn):
                self.conn = conn

            def __enter__(self):
                self.conn.__enter__()
                return self

            def __exit__(self, *args):
                return self.conn.__exit__(*args)

            def execute(self, sql, parameters=()):
                if str(sql).strip().upper() == "BEGIN IMMEDIATE":
                    fake_now["value"] = base + timedelta(seconds=2)
                return self.conn.execute(sql, parameters)

            def __getattr__(self, name):
                return getattr(self.conn, name)

        monkeypatch.setattr(store, "utc_now", lambda: fake_now["value"].isoformat())
        monkeypatch.setattr(
            store,
            "_connect",
            lambda: LockAdvancingConnection(original_connect()),
        )

        with pytest.raises(ValueError, match="autonomy_run_lease_expired"):
            store.create_autonomy_draft_approval(
                envelope["envelope_digest"],
                "run_atomic_lease_01",
                run["fence"],
                target={"type": "contact", "contact_id": "lead_atomic_01"},
                message="Oi! Podemos alinhar o próximo passo deste projeto?",
            )

        with sqlite3.connect(store.get_db_path()) as conn:
            drafts = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
            approvals = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
            local_writes = conn.execute(
                "SELECT local_write_count FROM autonomy_runs WHERE run_key=?",
                ("run_atomic_lease_01",),
            ).fetchone()[0]
    finally:
        reset_hermes_home_override(token)

    assert drafts == 0
    assert approvals == 0
    assert local_writes == 0
