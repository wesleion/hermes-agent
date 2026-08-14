from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone


_NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _pack() -> dict:
    return {
        "schema": "op-lp-sales-pack/v1",
        "project_ref": "project_demo_01",
        "refs": {
            "source_snapshot_ref": "snapshot_demo_01",
            "package_ref": "package_demo_01",
            "preview_ref": "preview_demo_01",
            "payment_ref": "payment_demo_01",
            "policy_ref": "policy_demo_01",
        },
        "audience": {
            "segments": ["segment_smb", "segment_local"],
            "lead_classes": ["lead_warm", "lead_cold"],
        },
        "expires_at": (_NOW + timedelta(days=1)).isoformat(),
    }


def test_sales_pack_admission_is_strict_canonical_and_executable():
    from tools.whatsapp_ops_sales import admit_sales_pack

    pack = _pack()
    first = admit_sales_pack(pack, now=_NOW)
    reordered = {
        "expires_at": pack["expires_at"],
        "audience": {
            "lead_classes": list(pack["audience"]["lead_classes"]),
            "segments": list(pack["audience"]["segments"]),
        },
        "refs": dict(reversed(list(pack["refs"].items()))),
        "project_ref": pack["project_ref"],
        "schema": pack["schema"],
    }
    second = admit_sales_pack(reordered, now=_NOW)

    assert first.accepted is True
    assert first.code == "sales_pack_accepted"
    assert first.sales_pack is not None
    assert first.sales_pack.digest == second.sales_pack.digest
    assert len(first.sales_pack.digest) == 64
    assert first.sales_pack.segments == ("segment_smb", "segment_local")
    assert first.sales_pack.lead_classes == ("lead_warm", "lead_cold")
    assert first.as_dict()["sales_pack"]["project_ref"] == "project_demo_01"


def test_sales_pack_rejects_unknown_missing_duplicate_and_malformed_values():
    from tools.whatsapp_ops_sales import admit_sales_pack

    cases: list[tuple[str, object]] = []

    unknown = _pack()
    unknown["copy"] = "must_not_be_here"
    cases.append(("sales_pack_key_unknown", unknown))

    nested_unknown = _pack()
    nested_unknown["audience"]["copy"] = "must_not_be_here"
    cases.append(("sales_pack_key_unknown", nested_unknown))

    missing = _pack()
    del missing["refs"]["policy_ref"]
    cases.append(("sales_pack_key_missing", missing))

    duplicate_value = _pack()
    duplicate_value["audience"]["segments"] = ["segment_smb", "segment_smb"]
    cases.append(("sales_pack_duplicate_value", duplicate_value))

    malformed_ref = _pack()
    malformed_ref["refs"]["preview_ref"] = ""
    cases.append(("sales_pack_ref_invalid", malformed_ref))

    malformed_expiry = _pack()
    malformed_expiry["expires_at"] = "2026-08-15T12:00:00"
    cases.append(("sales_pack_expiry_invalid", malformed_expiry))

    cases.append(("sales_pack_payload_invalid", "not-json"))

    for expected, payload in cases:
        result = admit_sales_pack(payload, now=_NOW)
        assert result.accepted is False, expected
        assert result.code == expected
        assert result.sales_pack is None

    duplicate_key_json = json.dumps(_pack(), separators=(",", ":")).replace(
        '"project_ref":"project_demo_01"',
        '"project_ref":"project_demo_01","project_ref":"project_other_02"',
        1,
    )
    duplicate = admit_sales_pack(duplicate_key_json, now=_NOW)
    assert duplicate.accepted is False
    assert duplicate.code == "sales_pack_duplicate_key"


def test_sales_pack_rejects_expiry_and_sensitive_material_without_echo():
    from tools.whatsapp_ops_sales import admit_sales_pack

    expired = _pack()
    expired["expires_at"] = (_NOW - timedelta(seconds=1)).isoformat()
    assert admit_sales_pack(expired, now=_NOW).code == "sales_pack_expired"

    sentinels = (
        "+551199998888",
        "owner@example.invalid",
        "https://example.invalid/private",
        "551199998888@s.whatsapp.net",
        "wa:551199998888",
        "lead_551199998888",
        "sk-secret-canary-value",
    )
    for sentinel in sentinels:
        payload = copy.deepcopy(_pack())
        payload["refs"]["payment_ref"] = sentinel
        result = admit_sales_pack(payload, now=_NOW)
        serialized = json.dumps(result.as_dict(), ensure_ascii=False)
        assert result.accepted is False
        assert result.code == "sales_pack_sensitive_material"
        assert sentinel not in serialized
