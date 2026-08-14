"""Fail-closed append-only CRM sink for human-approved WhatsApp sends.

The sink is an internal post-send effect, not a model-callable write tool. Google
libraries are imported lazily so minimal Hermes runtimes can still import the
WhatsApp toolset.

The sink writes the existing commercial ``Interacoes`` schema only. Every cell
comes from explicit CRM context bound into the approved draft; message content,
raw WhatsApp refs, spreadsheet coordinates, credentials, and provider responses
are never copied into the row or returned.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from tools.whatsapp_ops_store import (
    get_contact_crm_binding,
    hash_text,
    init_db,
    mark_crm_append_result,
    record_crm_audit,
    reserve_crm_append,
    utc_now,
)

CRM_ROW_HEADER = (
    "ID Interacao",
    "ID Lead",
    "ID Contato",
    "Tipo Interacao",
    "Data",
    "Resumo",
    "Proximo Passo",
)

_SPREADSHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_CREDENTIAL_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
_WILDCARD_RE = re.compile(r"[*?]")
_LONG_DIGITS_RE = re.compile(r"\d{7,}")
_CRM_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,39}$")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_WA_REF_RE = re.compile(r"(?i)\b[\w.+:-]+@(?:lid|g\.us|s\.whatsapp\.net|c\.us)\b")
_SECRET_RE = re.compile(
    r"(?i)\b(?:token|secret|api[_ -]?key|authorization|private[_ -]?key|password)\s*[:=]"
)
_INTERACTION_RANGE_RE = re.compile(
    r"^(?P<sheet>'(?:[^']|'')+'|[A-Za-z0-9_. -]+)!A:G$"
)
_LEAD_LOOKUP_RANGE_RE = re.compile(
    r"^(?:'Leads'|Leads)!A1:Z(?P<last>[1-9][0-9]{0,2})$"
)


def default_crm_config() -> dict[str, Any]:
    """Return an independent fail-closed CRM configuration."""
    return {
        "enabled": False,
        "read_only": True,
        "write_enabled": False,
        "append_after_send_enabled": False,
        "backend": "google_sheets_append",
        "mode": "append_only",
        "allowed_event_types": ["send_completed"],
        "google_sheets": {
            "spreadsheet_id": "",
            "range": "",
            "lead_lookup_range": "",
            "allowed_spreadsheet_ids": [],
            "allowed_ranges": [],
            "allowed_lead_lookup_ranges": [],
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


def _safe_crm_text(value: Any, *, max_length: int, required: bool = True) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return "" if not required else ""
    if len(text) > max_length:
        return ""
    if text.startswith(("=", "+", "-", "@")):
        return ""
    if (
        _URL_RE.search(text)
        or _EMAIL_RE.search(text)
        or _WA_REF_RE.search(text)
        or _LONG_DIGITS_RE.search(text)
        or _SECRET_RE.search(text)
    ):
        return ""
    return text


def _crm_context(draft: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    targets = _parse_targets(draft)
    if len(targets) != 1 or str(targets[0].get("type") or "contact").strip().lower() != "contact":
        return None, "crm_contact_target_required"
    local_contact_id = str(targets[0].get("contact_id") or "").strip()
    trusted_binding = get_contact_crm_binding(local_contact_id)
    if trusted_binding is None:
        return None, "crm_contact_binding_missing"
    raw = targets[0].get("crm")
    if not isinstance(raw, dict) or not raw:
        return None, "crm_context_missing"
    lead_id = str(raw.get("lead_id") or "").strip()
    contact_id = str(raw.get("contact_id") or "").strip()
    interaction_type = _safe_crm_text(raw.get("interaction_type"), max_length=40)
    summary = _safe_crm_text(raw.get("summary"), max_length=240)
    next_step = _safe_crm_text(raw.get("next_step"), max_length=180, required=False)
    if (
        not _CRM_ID_RE.fullmatch(lead_id)
        or not _CRM_ID_RE.fullmatch(contact_id)
        or not interaction_type
        or not summary
        or (raw.get("next_step") not in (None, "") and not next_step)
    ):
        return None, "crm_context_invalid"
    if (
        lead_id != str(trusted_binding.get("lead_id") or "").strip()
        or contact_id != str(trusted_binding.get("contact_id") or "").strip()
    ):
        return None, "crm_contact_binding_mismatch"
    return {
        "lead_id": lead_id,
        "contact_id": contact_id,
        "interaction_type": interaction_type,
        "summary": summary,
        "next_step": next_step,
    }, None


def approval_crm_preview(draft: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    """Return only sanitized, trusted fields that the human will approve."""
    return _crm_context(draft)


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
) -> dict[str, Any]:
    event_id, _ = _event_identity(draft, approval)
    context, reason = _crm_context(draft)
    if context is None:
        raise ValueError(reason or "crm_context_invalid")
    occurred_at = _safe_occurred_at(draft, approval)
    row = [
        "INT-" + hash_text("interaction\n" + event_id)[:12],
        context["lead_id"],
        context["contact_id"],
        context["interaction_type"],
        occurred_at[:10],
        context["summary"],
        context["next_step"],
    ]
    return {"header": list(CRM_ROW_HEADER), "row": row}


def _valid_timeout_seconds(sheets: dict[str, Any]) -> int | None:
    raw = sheets.get("timeout_seconds")
    if type(raw) is not int or not 1 <= raw <= 60:
        return None
    return raw


def _interaction_header_range(target_range: str) -> str | None:
    match = _INTERACTION_RANGE_RE.fullmatch(str(target_range or "").strip())
    return match.group("sheet") + "!A1:G1" if match else None


def _lead_lookup_range_is_bounded(value: str) -> bool:
    match = _LEAD_LOOKUP_RANGE_RE.fullmatch(str(value or "").strip())
    return bool(match and 2 <= int(match.group("last")) <= 501)


def _preflight_reason(
    crm_config: dict[str, Any],
    draft: dict[str, Any],
    approval: dict[str, Any],
    send_result: dict[str, Any],
) -> str | None:
    if crm_config.get("enabled") is not True:
        return "crm_disabled"
    if crm_config.get("read_only") is not False:
        return "crm_read_only"
    if crm_config.get("write_enabled") is not True:
        return "crm_write_disabled"
    if crm_config.get("append_after_send_enabled") is not True:
        return "crm_append_after_send_disabled"
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
    if approval.get("draft_idempotency_key") != draft.get("idempotency_key"):
        return "approval_draft_mismatch"
    if send_result.get("ok") is not True:
        return "send_not_completed"

    sheets = crm_config.get("google_sheets")
    if not isinstance(sheets, dict):
        return "crm_google_sheets_config_invalid"
    spreadsheet_id = str(sheets.get("spreadsheet_id") or "").strip()
    target_range = str(sheets.get("range") or "").strip()
    lead_lookup_range = str(sheets.get("lead_lookup_range") or "").strip()
    if not spreadsheet_id:
        return "crm_spreadsheet_missing"
    if not target_range:
        return "crm_range_missing"
    if not lead_lookup_range:
        return "crm_lead_lookup_range_missing"
    if any(
        _WILDCARD_RE.search(value)
        for value in (spreadsheet_id, target_range, lead_lookup_range)
    ):
        return "crm_wildcard_forbidden"
    if _interaction_header_range(target_range) is None:
        return "crm_range_invalid"
    if not _lead_lookup_range_is_bounded(lead_lookup_range):
        return "crm_lead_lookup_range_invalid"
    if _valid_timeout_seconds(sheets) is None:
        return "crm_timeout_invalid"
    allowed_ids = sheets.get("allowed_spreadsheet_ids")
    allowed_ranges = sheets.get("allowed_ranges")
    allowed_lead_ranges = sheets.get("allowed_lead_lookup_ranges")
    if not isinstance(allowed_ids, list) or spreadsheet_id not in allowed_ids:
        return "crm_spreadsheet_not_allowed"
    if not isinstance(allowed_ranges, list) or target_range not in allowed_ranges:
        return "crm_range_not_allowed"
    if not isinstance(allowed_lead_ranges, list) or lead_lookup_range not in allowed_lead_ranges:
        return "crm_lead_lookup_range_not_allowed"
    configured_values = [
        spreadsheet_id,
        target_range,
        lead_lookup_range,
        *allowed_ids,
        *allowed_ranges,
        *allowed_lead_ranges,
    ]
    if any(_WILDCARD_RE.search(str(value or "")) for value in configured_values):
        return "crm_wildcard_forbidden"
    credentials_env = str(sheets.get("credentials_env") or "")
    if not _CREDENTIAL_ENV_RE.fullmatch(credentials_env):
        return "crm_credentials_env_invalid"
    return None


def crm_send_preflight(
    *, draft: dict[str, Any], approval: dict[str, Any], config: dict[str, Any]
) -> str | None:
    """Validate every CRM gate before a contact send reaches its provider."""
    crm_config = _configured_crm(config)
    reason = _preflight_reason(crm_config, draft, approval, {"ok": True})
    if reason:
        return reason
    _, context_reason = _crm_context(draft)
    return context_reason


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
    updated_range = str(updates.get("updatedRange") or "").strip()
    read_back = response.get("read_back")
    return bool(updated_range) and isinstance(read_back, dict) and read_back.get("values") == [row]


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
    private_key = str(credentials_info.get("private_key") or "")
    if "\\n" in private_key:
        credentials_info = dict(credentials_info)
        credentials_info["private_key"] = private_key.replace("\\n", "\n")

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
        values_api = service.spreadsheets().values()
        spreadsheet_id = str(sheets["spreadsheet_id"])
        header_range = _interaction_header_range(str(sheets["range"]))
        if header_range is None:
            return {"ok": False, "reason": "crm_range_invalid"}
        header_response = values_api.get(
            spreadsheetId=spreadsheet_id,
            range=header_range,
            majorDimension="ROWS",
        ).execute()
        header_rows = header_response.get("values") if isinstance(header_response, dict) else None
        if header_rows != [list(CRM_ROW_HEADER)]:
            return {"ok": False, "reason": "crm_interacoes_schema_mismatch"}

        lead_response = values_api.get(
            spreadsheetId=spreadsheet_id,
            range=str(sheets["lead_lookup_range"]),
            majorDimension="ROWS",
        ).execute()
        lead_rows = lead_response.get("values") if isinstance(lead_response, dict) else None
        if not isinstance(lead_rows, list) or not lead_rows or not isinstance(lead_rows[0], list):
            return {"ok": False, "reason": "crm_lead_header_mismatch"}
        lead_header = [str(value or "").strip() for value in lead_rows[0]]
        if lead_header.count("ID Lead") != 1:
            return {"ok": False, "reason": "crm_lead_header_mismatch"}
        lead_index = lead_header.index("ID Lead")
        lead_id = row[1]
        lead_matches = 0
        for candidate in lead_rows[1:]:
            if (
                isinstance(candidate, list)
                and lead_index < len(candidate)
                and str(candidate[lead_index] or "").strip() == lead_id
            ):
                lead_matches += 1
        if lead_matches == 0:
            return {"ok": False, "reason": "crm_lead_not_found"}
        if lead_matches > 1:
            return {"ok": False, "reason": "crm_lead_ambiguous"}

        response = (
            values_api
            .append(
                spreadsheetId=spreadsheet_id,
                range=str(sheets["range"]),
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                includeValuesInResponse=True,
                body={"values": [row]},
            )
            .execute()
        )
        updates = response.get("updates") if isinstance(response, dict) else None
        updated_range = str((updates or {}).get("updatedRange") or "").strip()
        if not updated_range:
            return {"ok": False, "reason": "append_unverified"}
        read_back = (
            values_api
            .get(
                spreadsheetId=spreadsheet_id,
                range=updated_range,
                majorDimension="ROWS",
            )
            .execute()
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": "google_sheets_exception",
            "error_class": _safe_error_class(exc),
        }

    if not _verify_append_response({"updates": response.get("updates"), "read_back": read_back}, row):
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
    event_id, idempotency_key = _event_identity(draft, approval)
    enabled = crm_config.get("enabled") is True
    blocked_reason = _preflight_reason(crm_config, draft, approval, send_result)
    _, context_reason = _crm_context(draft)
    blocked_reason = blocked_reason or context_reason
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

    payload = _build_payload(draft, approval, send_result)
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
