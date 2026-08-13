"""Gate B contracts for person/channel identity and read-only CRM preview."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


PUBLIC_CHANNEL_KEYS = {
    "channel_id",
    "contact_id",
    "channel_type",
    "address_masked",
    "is_active",
    "is_primary",
    "validation_status",
    "allow_send",
    "authorized_at",
    "revoked_at",
}


def _store_api():
    from tools import whatsapp_ops_store as store

    names = (
        "upsert_contact",
        "upsert_channel",
        "authorize_channel",
        "revoke_channel",
        "get_contact_channel",
        "list_contact_channels",
        "get_transport_contact_ref",
        "preview_crm_identity_sync",
    )
    funcs = tuple(getattr(store, name, None) for name in names)
    assert all(callable(func) for func in funcs), "Gate B contact-channel API is missing"
    return funcs


def _raw_channel(db_path, channel_id):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM contact_channels WHERE id=?", (channel_id,)
        ).fetchone()
        return dict(row) if row else None


def test_legacy_migration_is_additive_idempotent_and_preserves_authority(tmp_path):
    from tools.whatsapp_ops_store import get_db_path, init_db

    raw_ref = "12025550101@s.whatsapp.net"
    token = set_hermes_home_override(tmp_path)
    try:
        db_path = get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                "CREATE TABLE contacts (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
                "phone_e164_hash TEXT, phone_e164_enc TEXT, whitelisted INTEGER NOT NULL DEFAULT 0, "
                "policy_group TEXT, metadata_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            )
            conn.execute(
                "INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "contact_legacy",
                    "Legacy Synthetic",
                    "legacy-hash",
                    raw_ref,
                    1,
                    "lead",
                    "{}",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        init_db()
        first = sqlite3.connect(db_path).execute(
            "SELECT contact_id, is_active, is_primary, validation_status, allow_send, "
            "authorized_at, revoked_at, source FROM contact_channels"
        ).fetchall()
        init_db()
        second = sqlite3.connect(db_path).execute(
            "SELECT contact_id, is_active, is_primary, validation_status, allow_send, "
            "authorized_at, revoked_at, source FROM contact_channels"
        ).fetchall()
    finally:
        reset_hermes_home_override(token)

    assert first == second
    assert first == [
        (
            "contact_legacy",
            1,
            1,
            "validated",
            1,
            "2026-01-01T00:00:00+00:00",
            None,
            "legacy_contacts",
        )
    ]


def test_contact_and_channel_upserts_never_grant_or_reactivate_send(tmp_path):
    (
        upsert_contact,
        upsert_channel,
        authorize_channel,
        revoke_channel,
        get_channel,
        _,
        transport_ref,
        _,
    ) = _store_api()
    raw_ref = "12025550102@s.whatsapp.net"
    token = set_hermes_home_override(tmp_path)
    try:
        contact = upsert_contact(
            contact_id="contact_safe",
            display_name="Safe Person",
            phone_e164=raw_ref,
            whitelisted=True,
        )
        channel = upsert_channel(
            contact_id="contact_safe",
            address=raw_ref,
            is_primary=True,
            validation_status="validated",
            source="crm_preview",
        )
        assert contact["whitelisted"] is False
        assert channel["allow_send"] is False
        assert transport_ref("contact_safe") == ""

        authorized = authorize_channel(channel["channel_id"])
        assert authorized["allow_send"] is True
        assert authorized["authorized_at"]
        assert transport_ref("contact_safe") == raw_ref

        revoked = revoke_channel(channel["channel_id"])
        assert revoked["allow_send"] is False
        assert revoked["is_active"] is False
        assert revoked["revoked_at"]
        assert transport_ref("contact_safe") == ""

        upsert_contact(
            contact_id="contact_safe",
            display_name="Safe Person Updated",
            phone_e164=raw_ref,
            whitelisted=True,
        )
        repeated = upsert_channel(
            contact_id="contact_safe",
            address=raw_ref,
            is_active=True,
            is_primary=True,
            validation_status="validated",
            source="crm_update",
        )
        stored = get_channel(channel["channel_id"])
    finally:
        reset_hermes_home_override(token)

    assert repeated["channel_id"] == channel["channel_id"]
    assert stored["allow_send"] is False
    assert stored["is_active"] is False
    assert stored["revoked_at"] == revoked["revoked_at"]


def test_authorize_requires_active_validated_channel_and_is_explicit(tmp_path):
    upsert_contact, upsert_channel, authorize, revoke, _, _, _, _ = _store_api()
    token = set_hermes_home_override(tmp_path)
    try:
        upsert_contact(contact_id="contact_guard", display_name="Guard")
        unvalidated = upsert_channel(
            contact_id="contact_guard",
            address="12025550103@s.whatsapp.net",
            is_primary=True,
        )
        with pytest.raises(ValueError, match="validated"):
            authorize(unvalidated["channel_id"])
        validated = upsert_channel(
            contact_id="contact_guard",
            address="12025550103@s.whatsapp.net",
            is_primary=True,
            validation_status="validated",
        )
        allowed = authorize(validated["channel_id"])
        revoke(allowed["channel_id"])
        with pytest.raises(ValueError, match="inactive|revoked"):
            authorize(allowed["channel_id"])
    finally:
        reset_hermes_home_override(token)


def test_transport_resolution_requires_one_exact_sendable_channel(tmp_path):
    upsert_contact, upsert_channel, authorize, _, _, _, resolve_ref, _ = _store_api()
    ref_a = "12025550104@s.whatsapp.net"
    ref_b = "12025550105@s.whatsapp.net"
    token = set_hermes_home_override(tmp_path)
    try:
        upsert_contact(contact_id="contact_multi", display_name="Multi", aliases=["multi"])
        a = upsert_channel(
            contact_id="contact_multi",
            address=ref_a,
            is_primary=True,
            validation_status="validated",
        )
        b = upsert_channel(
            contact_id="contact_multi",
            address=ref_b,
            is_primary=False,
            validation_status="validated",
        )
        authorize(a["channel_id"])
        assert resolve_ref("multi") == ref_a
        authorize(b["channel_id"])
        assert resolve_ref("multi") == ""
        assert resolve_ref("multi", channel_id=a["channel_id"]) == ref_a
        assert resolve_ref("multi", channel_id=b["channel_id"]) == ref_b
    finally:
        reset_hermes_home_override(token)


def test_public_channel_shapes_never_expose_raw_address_or_context(tmp_path):
    upsert_contact, upsert_channel, _, _, get_channel, list_channels, _, _ = _store_api()
    raw_ref = "12025550106@s.whatsapp.net"
    token = set_hermes_home_override(tmp_path)
    try:
        upsert_contact(contact_id="contact_private", display_name="Private")
        created = upsert_channel(
            contact_id="contact_private",
            address=raw_ref,
            context_key="private-thread-key",
        )
        fetched = get_channel(created["channel_id"])
        listed = list_channels("contact_private")
    finally:
        reset_hermes_home_override(token)

    public = json.dumps({"created": created, "fetched": fetched, "listed": listed})
    assert set(created) == PUBLIC_CHANNEL_KEYS
    assert set(fetched) == PUBLIC_CHANNEL_KEYS
    assert set(listed[0]) == PUBLIC_CHANNEL_KEYS
    assert raw_ref not in public
    assert "12025550106" not in public
    assert "private-thread-key" not in public
    assert "address_hash" not in public


def test_crm_identity_preview_is_read_only_bounded_and_forces_allow_send_false(tmp_path):
    *_, preview = _store_api()
    token = set_hermes_home_override(tmp_path)
    try:
        before_exists = (tmp_path / "wpp_ops.sqlite").exists()
        result = preview(
            contact_rows=[
                {
                    "contact_id": "person_a",
                    "display_name": "Person A",
                    "phone": "+1 202 555 0107",
                    "allow_send": True,
                },
                {
                    "contact_id": "person_b",
                    "display_name": "Person B",
                    "phone": "invalid-phone",
                },
            ],
            channel_rows=[
                {
                    "contact_id": "person_a",
                    "address": "12025550107@s.whatsapp.net",
                    "is_primary": True,
                    "allow_send": True,
                },
                {
                    "contact_id": "person_a",
                    "address": "+1 202 555 0108",
                    "is_primary": True,
                },
                {
                    "contact_id": "person_a",
                    "address": "+1 202 555 0108",
                    "is_primary": False,
                },
            ],
            limit=20,
        )
        after_exists = (tmp_path / "wpp_ops.sqlite").exists()
    finally:
        reset_hermes_home_override(token)

    serialized = json.dumps(result, sort_keys=True)
    assert before_exists is False and after_exists is False
    assert result["ok"] is True
    assert result["crm_write_performed"] is False
    assert result["local_write_performed"] is False
    assert result["allow_send_changes"] == 0
    assert result["invalid_count"] >= 1
    assert result["duplicate_count"] >= 1
    assert result["ambiguous_count"] >= 1
    assert result["truncated"] is False
    assert all(p["allow_send"] is False for p in result["channel_proposals"])
    assert "12025550107" not in serialized
    assert "12025550108" not in serialized


def test_public_crm_sync_preview_tool_is_registered_and_does_not_change_sqlite(tmp_path):
    from tools.registry import registry
    from tools.whatsapp_ops_store import get_db_path, init_db
    from tools.whatsapp_ops_tool import wpp_crm_sync_preview

    token = set_hermes_home_override(tmp_path)
    try:
        init_db()
        db_path = get_db_path()
        before = db_path.read_bytes()
        result = json.loads(
            wpp_crm_sync_preview(
                contacts=[
                    {
                        "contact_id": "crm_public",
                        "display_name": "Public Preview",
                        "phone": "+12025550123",
                    }
                ]
            )
        )
        after = db_path.read_bytes()
        entry = registry._tools.get("wpp_crm_sync_preview")
    finally:
        reset_hermes_home_override(token)

    assert result["ok"] is True
    assert result["crm_write_performed"] is False
    assert result["local_write_performed"] is False
    assert result["allow_send_changes"] == 0
    assert before == after
    assert entry is not None
    assert "read-only" in entry.schema["description"].lower()
