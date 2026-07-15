"""Fail-closed append-only CRM sink for human-approved WhatsApp sends.

The sink is an internal post-send effect, not a model-callable write tool. Google
libraries are imported lazily so minimal Hermes runtimes can still import the
WhatsApp toolset.

Google Sheets row header (stable order):
``event_id, idempotency_key, occurred_at, event_type, draft_id,
target_safe_id, target_hash, message_hash, send_transport, actor_mode, status``.
Every cell is a safe string. Message content, raw WhatsApp refs, spreadsheet
coordinates, credentials, and provider responses are never returned or audited.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from tools.whatsapp_ops_store import (
    hash_text,
    init_db,
    mark_crm_append_result,
    record_crm_audit,
    reserve_crm_append,
    utc_now,
)

CRM_ROW_HEADER = (
    "event_id",
    "idempotency_key",
    "occurred_at",
    "event_type",
    "draft_id",
    "target_safe_id",
    "target_hash",
    "message_hash",
    "send_transport",
    "actor_mode",
    "status",
)

_SPREADSHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_CREDENTIAL_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
_WILDCARD_RE = re.compile(r"[*?]")
_LONG_DIGITS_RE = re.compile(r"\d{7,}")


def default_crm_config() -> dict[str, Any]:
    """Return an independent fail-closed CRM configuration."""
    return {
        "enabled": False,
        "write_enabled": False,
        "backend": "google_sheets_append",
        "mode": "append_only",
        "allowed_event_types": ["send_completed"],
        "google_sheets": {
            "spreadsheet_id": "",
            "range": "",
            "allowed_spreadsheet_ids": [],
            "allowed_ranges": [],
            "credentials_env": "GOOGLE_SERVICE_ACCOUNT_JSON",
            "timeout_seconds": 10,
        },
    }


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _configured_crm(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get("crm")
    if not isinstance(raw, dict):
        return default_crm_config()
    return _merge_dict(default_crm_config(), raw)


def _safe_token(value: Any, *, fallback: str, max_length: int = 80) -> str:
    text = str(value or "").strip()
    if _SAFE_TOKEN_RE.fullmatch(text) and not _LONG_DIGITS_RE.search(text) and "@" not in text:
        return text[:max_length]
    return fallback


def _safe_error_class(exc: BaseException) -> str:
    name = type(exc).__name__
    return re.sub(r"[^A-Za-z0-9_]+", "", name)[:80] or "Error"


def _event_identity(draft: dict[str, Any], approval: dict[str, Any]) -> tuple[str, str]:
    seed = "\n".join(
        (
            str(draft.get("id") or ""),
            str(draft.get("idempotency_key") or ""),
            str(draft.get("message_hash") or ""),
            str(approval.get("id") or ""),
            "send_completed",
        )
    )
    digest = hash_text(seed)
    return "crm_evt_" + digest[:24], hash_text("crm_append\n" + digest)


def _parse_targets(draft: dict[str, Any]) -> list[dict[str, Any]]:
    raw = draft.get("targets_json")
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _target_identity(draft: dict[str, Any]) -> tuple[str, str]:
    targets = _parse_targets(draft)
    canonical = json.dumps(targets, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    target_hash = hash_text(canonical or "unknown_target")
    if len(targets) != 1:
        return "multi_target_" + target_hash[:16], target_hash

    target = targets[0]
    target_type = str(target.get("type") or "contact").strip().lower()
    if target_type == "group_create":
        return "group_create_" + target_hash[:16], target_hash
    candidate = target.get("contact_id") or target.get("list_id") or target.get("group_id")
    safe_candidate = _safe_token(candidate, fallback="")
    if safe_candidate:
        return safe_candidate, target_hash
    return "target_" + target_hash[:16], target_hash


def _safe_occurred_at(draft: dict[str, Any], approval: dict[str, Any]) -> str:
    candidate = approval.get("resolved_at") or draft.get("updated_at") or draft.get("created_at")
    text = str(candidate or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return utc_now()


def _build_payload(
    draft: dict[str, Any], approval: dict[str, Any], send_result: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    event_id, idempotency_key = _event_identity(draft, approval)
    target_safe_id, target_hash = _target_identity(draft)
    draft_id = _safe_token(
        draft.get("id"), fallback="draft_" + hash_text(str(draft.get("id") or ""))[:16]
    )
    message_hash_raw = str(draft.get("message_hash") or "")
    message_hash = (
        message_hash_raw.lower()
        if re.fullmatch(r"[A-Fa-f0-9]{64}", message_hash_raw)
        else hash_text(message_hash_raw)
    )
    transport = _safe_token(send_result.get("transport"), fallback="unknown_transport")
    row = [
        event_id,
        idempotency_key,
        _safe_occurred_at(draft, approval),
        "send_completed",
        draft_id,
        target_safe_id,
        target_hash,
        message_hash,
        transport,
        "human_approved",
        "sent",
    ]
    return {"header": list(CRM_ROW_HEADER), "row": row}, idempotency_key


def _valid_timeout_seconds(sheets: dict[str, Any]) -> int | None:
    raw = sheets.get("timeout_seconds")
    if type(raw) is not int or not 1 <= raw <= 60:
        return None
    return raw


def _preflight_reason(
    crm_config: dict[str, Any],
    draft: dict[str, Any],
    approval: dict[str, Any],
    send_result: dict[str, Any],
) -> str | None:
    if crm_config.get("enabled") is not True:
        return "crm_disabled"
    if crm_config.get("write_enabled") is not True:
        return "crm_write_disabled"
    if crm_config.get("backend") != "google_sheets_append":
        return "crm_backend_invalid"
    if crm_config.get("mode") != "append_only":
        return "crm_mode_invalid"
    allowed_events = crm_config.get("allowed_event_types")
    if not isinstance(allowed_events, list) or "send_completed" not in allowed_events:
        return "crm_event_type_not_allowed"
    if approval.get("status") != "approved":
        return "approval_not_approved"
    if not str(approval.get("approver_ref_hash") or "").strip():
        return "approval_human_actor_missing"
    if not str(approval.get("resolved_at") or "").strip():
        return "approval_resolution_missing"
    if approval.get("message_hash") != draft.get("message_hash"):
        return "approval_message_mismatch"
    if send_result.get("ok") is not True:
        return "send_not_completed"

    sheets = crm_config.get("google_sheets")
    if not isinstance(sheets, dict):
        return "crm_google_sheets_config_invalid"
    spreadsheet_id = str(sheets.get("spreadsheet_id") or "").strip()
    target_range = str(sheets.get("range") or "").strip()
    if not spreadsheet_id:
        return "crm_spreadsheet_missing"
    if not target_range:
        return "crm_range_missing"
    if _valid_timeout_seconds(sheets) is None:
        return "crm_timeout_invalid"
    allowed_ids = sheets.get("allowed_spreadsheet_ids")
    allowed_ranges = sheets.get("allowed_ranges")
    if not isinstance(allowed_ids, list) or spreadsheet_id not in allowed_ids:
        return "crm_spreadsheet_not_allowed"
    if not isinstance(allowed_ranges, list) or target_range not in allowed_ranges:
        return "crm_range_not_allowed"
    configured_values = [spreadsheet_id, target_range, *allowed_ids, *allowed_ranges]
    if any(_WILDCARD_RE.search(str(value or "")) for value in configured_values):
        return "crm_wildcard_forbidden"
    credentials_env = str(sheets.get("credentials_env") or "")
    if not _CREDENTIAL_ENV_RE.fullmatch(credentials_env):
        return "crm_credentials_env_invalid"
    return None


def _verify_append_response(response: Any, row: list[str]) -> bool:
    if not isinstance(response, dict):
        return False
    updates = response.get("updates")
    if not isinstance(updates, dict):
        return False
    rows_confirmed = updates.get("updatedRows") == 1
    cells_confirmed = updates.get("updatedCells") == len(row)
    if not (rows_confirmed or cells_confirmed):
        return False
    updated_data = updates.get("updatedData")
    return isinstance(updated_data, dict) and updated_data.get("values") == [row]


def _google_dependencies() -> tuple[Any, Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
    """Import Google dependencies only on the enabled default-client path."""
    from google.oauth2.service_account import Credentials
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    from httplib2 import Http

    return Credentials, build, AuthorizedHttp, Http


def append_google_sheets_row(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Append exactly one RAW row and require an exact echoed confirmation."""
    row = payload.get("row")
    header = payload.get("header")
    if (
        header != list(CRM_ROW_HEADER)
        or not isinstance(row, list)
        or len(row) != len(CRM_ROW_HEADER)
        or not all(isinstance(value, str) for value in row)
    ):
        return {"ok": False, "reason": "crm_payload_invalid"}

    sheets = config.get("google_sheets") if isinstance(config, dict) else None
    if not isinstance(sheets, dict):
        return {"ok": False, "reason": "crm_google_sheets_config_invalid"}
    credentials_env = str(sheets.get("credentials_env") or "")
    if not _CREDENTIAL_ENV_RE.fullmatch(credentials_env):
        return {"ok": False, "reason": "crm_credentials_env_invalid"}
    timeout_seconds = _valid_timeout_seconds(sheets)
    if timeout_seconds is None:
        return {"ok": False, "reason": "crm_timeout_invalid"}
    raw_credentials = os.environ.get(credentials_env)
    if not raw_credentials:
        return {"ok": False, "reason": "crm_credentials_missing"}
    try:
        credentials_info = json.loads(raw_credentials)
    except json.JSONDecodeError:
        return {"ok": False, "reason": "crm_credentials_invalid_json"}
    if not isinstance(credentials_info, dict):
        return {"ok": False, "reason": "crm_credentials_invalid_shape"}

    try:
        credentials_class, build, authorized_http_class, http_class = _google_dependencies()
        credentials = credentials_class.from_service_account_info(
            credentials_info, scopes=[_SPREADSHEETS_SCOPE]
        )
        transport = authorized_http_class(
            credentials,
            http=http_class(timeout=timeout_seconds),
        )
        service = build(
            "sheets", "v4", http=transport, cache_discovery=False
        )
        response = (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=str(sheets["spreadsheet_id"]),
                range=str(sheets["range"]),
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                includeValuesInResponse=True,
                body={"values": [row]},
            )
            .execute()
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": "google_sheets_exception",
            "error_class": _safe_error_class(exc),
        }

    if not _verify_append_response(response, row):
        return {"ok": False, "reason": "append_unverified"}
    return {"ok": True, "reason": "append_confirmed"}


def _public_result(
    *,
    enabled: bool,
    attempted: bool,
    result: str,
    reason: str,
    write_performed: bool,
    error_class: str = "",
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "enabled": enabled,
        "attempted": attempted,
        "result": result,
        "reason": reason,
        "write_performed": write_performed,
    }
    if error_class:
        response["error_class"] = error_class
    return response


def append_approved_send_event(
    *,
    draft: dict[str, Any],
    approval: dict[str, Any],
    send_result: dict[str, Any],
    config: dict[str, Any],
    client: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append one approved ``send_completed`` event after all gates pass.

    No automatic retry is performed. Once reserved, any exception or unverifiable
    response becomes ``failed_unknown`` and subsequent calls fail closed.
    """
    init_db()
    crm_config = _configured_crm(config)
    payload, idempotency_key = _build_payload(draft, approval, send_result)
    event_id = payload["row"][0]
    enabled = crm_config.get("enabled") is True
    blocked_reason = _preflight_reason(crm_config, draft, approval, send_result)
    if blocked_reason:
        record_crm_audit(
            "crm_append_blocked",
            event_id=event_id,
            reason=blocked_reason,
            status="blocked",
        )
        return _public_result(
            enabled=enabled,
            attempted=False,
            result="blocked",
            reason=blocked_reason,
            write_performed=False,
        )

    reservation = reserve_crm_append(idempotency_key, event_id)
    if not reservation["reserved"]:
        existing_status = reservation["status"]
        if existing_status == "appended":
            reason = "already_appended"
            result = "idempotent_replay"
        else:
            reason = "idempotency_uncertain"
            result = "blocked"
        record_crm_audit(
            "crm_append_blocked",
            event_id=event_id,
            reason=reason,
            status=existing_status,
        )
        return _public_result(
            enabled=True,
            attempted=False,
            result=result,
            reason=reason,
            write_performed=False,
        )

    try:
        append_result = (
            client(payload, crm_config)
            if client is not None
            else append_google_sheets_row(payload, crm_config)
        )
    except Exception as exc:
        error_class = _safe_error_class(exc)
        mark_crm_append_result(
            idempotency_key, status="failed_unknown", reason="crm_client_exception"
        )
        record_crm_audit(
            "crm_append_failed",
            event_id=event_id,
            reason="crm_client_exception",
            status="failed_unknown",
            error_class=error_class,
        )
        return _public_result(
            enabled=True,
            attempted=True,
            result="failed_unknown",
            reason="crm_client_exception",
            write_performed=False,
            error_class=error_class,
        )

    if client is not None:
        verified = _verify_append_response(append_result, payload["row"])
        safe_reason = "append_confirmed" if verified else "append_unverified"
        error_class = ""
    else:
        verified = bool(append_result.get("ok")) and append_result.get("reason") == "append_confirmed"
        safe_reason = str(append_result.get("reason") or "append_unverified")
        error_class = str(append_result.get("error_class") or "")

    if not verified:
        mark_crm_append_result(
            idempotency_key, status="failed_unknown", reason=safe_reason
        )
        record_crm_audit(
            "crm_append_failed",
            event_id=event_id,
            reason=safe_reason,
            status="failed_unknown",
            error_class=error_class,
        )
        return _public_result(
            enabled=True,
            attempted=True,
            result="failed_unknown",
            reason=safe_reason,
            write_performed=False,
            error_class=error_class,
        )

    mark_crm_append_result(
        idempotency_key, status="appended", reason="append_confirmed"
    )
    record_crm_audit(
        "crm_append_succeeded",
        event_id=event_id,
        reason="append_confirmed",
        status="appended",
    )
    return _public_result(
        enabled=True,
        attempted=True,
        result="appended",
        reason="append_confirmed",
        write_performed=True,
    )
