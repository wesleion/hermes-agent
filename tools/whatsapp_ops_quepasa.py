"""Fail-closed QuePasa/n8n client shim for WhatsApp Ops.

The real transport endpoint is intentionally optional.  If it is missing or
send flags are false, this module returns a structured refusal and performs no
network I/O.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def _redact_error(text: str) -> str:
    redacted = text or ""
    for key in ("token", "api_key", "apikey", "authorization", "secret"):
        redacted = redacted.replace(key, "[redacted]")
    return redacted[:500]


def send_via_quepasa(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    quepasa_raw = config.get("quepasa")
    quepasa = quepasa_raw if isinstance(quepasa_raw, dict) else {}
    if not bool(quepasa.get("send_enabled", False)):
        return {"ok": False, "error": "quepasa_send_disabled"}

    url = quepasa.get("send_url") or os.getenv("WHATSAPP_OPS_QUEPASA_SEND_URL", "")
    if not url:
        return {"ok": False, "error": "quepasa_send_url_missing"}

    api_key = os.getenv("WHATSAPP_OPS_QUEPASA_API_KEY", "")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read(2048).decode("utf-8", errors="replace")
        return {"ok": True, "status": getattr(resp, "status", 200), "body": body[:500]}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "http_error", "status": exc.code}
    except Exception as exc:  # pragma: no cover - defensive against transport stack
        return {"ok": False, "error": _redact_error(str(exc))}
