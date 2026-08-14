"""Strict, non-secret admission for versioned WhatsApp Ops sales packs.

This module is an internal data boundary only.  It does not register a model
callable tool, perform I/O, or carry package copy/URLs/credentials.  Accepted
packs contain only opaque references plus configured audience classifications.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SALES_PACK_SCHEMA = "op-lp-sales-pack/v1"

_TOP_LEVEL_KEYS = frozenset(
    {"schema", "project_ref", "refs", "audience", "expires_at"}
)
_REF_KEYS = frozenset(
    {
        "source_snapshot_ref",
        "package_ref",
        "preview_ref",
        "payment_ref",
        "policy_ref",
    }
)
_AUDIENCE_KEYS = frozenset({"segments", "lead_classes"})
_OPAQUE_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{1,127}$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\s().-]{6,}[0-9]$")
_EMBEDDED_PHONE_RE = re.compile(r"(?<![A-Za-z0-9])\+?[0-9]{10,15}(?![0-9])")
_SECRET_MARKERS = (
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "access_token",
    "private_key",
    "bearer",
    "token=",
    "sk-",
    "ghp_",
    "github_pat_",
    "xoxb-",
    "akia",
)


class SalesPackError(ValueError):
    """Fail-closed parse error carrying only a stable, non-secret code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _DuplicateKeyError(ValueError):
    pass


@dataclass(frozen=True)
class SalesPack:
    schema: str
    project_ref: str
    source_snapshot_ref: str
    package_ref: str
    preview_ref: str
    payment_ref: str
    policy_ref: str
    segments: tuple[str, ...]
    lead_classes: tuple[str, ...]
    expires_at: str
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "project_ref": self.project_ref,
            "refs": {
                "source_snapshot_ref": self.source_snapshot_ref,
                "package_ref": self.package_ref,
                "preview_ref": self.preview_ref,
                "payment_ref": self.payment_ref,
                "policy_ref": self.policy_ref,
            },
            "audience": {
                "segments": list(self.segments),
                "lead_classes": list(self.lead_classes),
            },
            "expires_at": self.expires_at,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class SalesPackAdmission:
    accepted: bool
    code: str
    sales_pack: SalesPack | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"accepted": self.accepted, "code": self.code}
        if self.sales_pack is not None:
            result["sales_pack"] = self.sales_pack.as_dict()
        return result


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _load_payload(payload: Any) -> dict[str, Any]:
    if type(payload) is dict:
        return dict(payload)
    if type(payload) is not str or not payload.strip():
        raise SalesPackError("sales_pack_payload_invalid")
    try:
        parsed = json.loads(payload, object_pairs_hook=_pairs_without_duplicates)
    except _DuplicateKeyError as exc:
        raise SalesPackError("sales_pack_duplicate_key") from exc
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SalesPackError("sales_pack_payload_invalid") from exc
    if type(parsed) is not dict:
        raise SalesPackError("sales_pack_payload_invalid")
    return parsed


def _require_exact_keys(value: Any, expected: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict:
        raise SalesPackError("sales_pack_payload_invalid")
    keys = set(value)
    if keys - expected:
        raise SalesPackError("sales_pack_key_unknown")
    if expected - keys:
        raise SalesPackError("sales_pack_key_missing")
    return value


def _contains_sensitive_material(value: Any) -> bool:
    if type(value) is not str:
        return False
    text = value.strip()
    lowered = text.casefold()
    if not text:
        return False
    if "@" in text or re.search(r"(?i)^https?:|\b(?:https?|ftp)://", text):
        return True
    if _PHONE_RE.fullmatch(text) or _EMBEDDED_PHONE_RE.search(text):
        return True
    return any(marker in lowered for marker in _SECRET_MARKERS)


def is_opaque_ref(value: Any) -> bool:
    """Return whether *value* is a bounded opaque identifier, never raw contact data."""

    return (
        type(value) is str
        and bool(value)
        and not _contains_sensitive_material(value)
        and _OPAQUE_REF_RE.fullmatch(value) is not None
    )


def _opaque_ref(value: Any) -> str:
    if _contains_sensitive_material(value):
        raise SalesPackError("sales_pack_sensitive_material")
    if not is_opaque_ref(value):
        raise SalesPackError("sales_pack_ref_invalid")
    return value


def _opaque_values(value: Any) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise SalesPackError("sales_pack_audience_invalid")
    normalized: list[str] = []
    for item in value:
        if _contains_sensitive_material(item):
            raise SalesPackError("sales_pack_sensitive_material")
        if not is_opaque_ref(item):
            raise SalesPackError("sales_pack_audience_invalid")
        normalized.append(item)
    if len(set(normalized)) != len(normalized):
        raise SalesPackError("sales_pack_duplicate_value")
    return tuple(normalized)


def _canonical_expiry(value: Any, *, now: datetime | None) -> str:
    if type(value) is not str or not value.strip():
        raise SalesPackError("sales_pack_expiry_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SalesPackError("sales_pack_expiry_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise SalesPackError("sales_pack_expiry_invalid")
    parsed = parsed.astimezone(timezone.utc)
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise SalesPackError("sales_pack_expiry_invalid")
    current = current.astimezone(timezone.utc)
    if parsed <= current:
        raise SalesPackError("sales_pack_expired")
    return parsed.isoformat()


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def parse_sales_pack(payload: Any, *, now: datetime | None = None) -> SalesPack:
    """Parse and normalize one exact ``op-lp-sales-pack/v1`` document."""

    root = _require_exact_keys(_load_payload(payload), _TOP_LEVEL_KEYS)
    if root["schema"] != SALES_PACK_SCHEMA:
        raise SalesPackError("sales_pack_schema_invalid")

    refs = _require_exact_keys(root["refs"], _REF_KEYS)
    audience = _require_exact_keys(root["audience"], _AUDIENCE_KEYS)
    project_ref = _opaque_ref(root["project_ref"])
    normalized_refs = {key: _opaque_ref(refs[key]) for key in sorted(_REF_KEYS)}
    segments = _opaque_values(audience["segments"])
    lead_classes = _opaque_values(audience["lead_classes"])
    expires_at = _canonical_expiry(root["expires_at"], now=now)

    canonical = {
        "schema": SALES_PACK_SCHEMA,
        "project_ref": project_ref,
        "refs": normalized_refs,
        "audience": {
            "segments": list(segments),
            "lead_classes": list(lead_classes),
        },
        "expires_at": expires_at,
    }
    digest = hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()
    return SalesPack(
        schema=SALES_PACK_SCHEMA,
        project_ref=project_ref,
        source_snapshot_ref=normalized_refs["source_snapshot_ref"],
        package_ref=normalized_refs["package_ref"],
        preview_ref=normalized_refs["preview_ref"],
        payment_ref=normalized_refs["payment_ref"],
        policy_ref=normalized_refs["policy_ref"],
        segments=segments,
        lead_classes=lead_classes,
        expires_at=expires_at,
        digest=digest,
    )


def admit_sales_pack(payload: Any, *, now: datetime | None = None) -> SalesPackAdmission:
    """Return a stable fail-closed admission result without echoing rejected input."""

    try:
        sales_pack = parse_sales_pack(payload, now=now)
    except SalesPackError as exc:
        return SalesPackAdmission(accepted=False, code=exc.code)
    return SalesPackAdmission(
        accepted=True,
        code="sales_pack_accepted",
        sales_pack=sales_pack,
    )
