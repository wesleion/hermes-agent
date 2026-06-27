"""Read-only WhatsApp Ops slash-command UX helpers.

These helpers intentionally expose only local, already-ingested inbound context.
They never send WhatsApp messages, fetch provider history, or print raw WhatsApp
refs/media URLs/operator secrets.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

_RAW_WA_REF_RE = re.compile(r"(?i)\b[\w.-]+@(?:g\.us|lid|s\.whatsapp\.net)\b")
_URL_RE = re.compile(r"(?i)\bhttps?://\S+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_DATA_B64_RE = re.compile(r"(?i)data:[^\s,]*?;base64,[A-Za-z0-9+/=]{16,}")
_LONG_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b")
_MEDIA_WORD_RE = re.compile(r"(?i)\b(?:base64|blob)\b")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|api[_-]?key|authorization|password|secret)\s*[=:]\s*\S+"
)


def _redact_phone_match(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "<telefone-redigido>" if len(digits) >= 10 else match.group(0)


def _safe_text(value: Any, *, max_len: int = 500) -> str:
    """Return display text with transport identifiers/media blobs redacted."""
    text = str(value or "")
    text = _DATA_B64_RE.sub("<midia-redigida>", text)
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1=<redigido>", text)
    text = _URL_RE.sub("<url-redigida>", text)
    text = _RAW_WA_REF_RE.sub("<ref-redigida>", text)
    text = _PHONE_RE.sub(_redact_phone_match, text)
    text = _LONG_B64_RE.sub("<midia-redigida>", text)
    text = _MEDIA_WORD_RE.sub("midia-redigida", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[: max(0, max_len - 1)].rstrip() + "…"
    return text


def _parse_thread_context_args(arg: str) -> dict[str, Any]:
    """Parse `/ctxwpp [thread|contact] [limit]` without echoing raw refs."""
    tokens = [part.strip() for part in str(arg or "").split() if part.strip()]
    ref = ""
    limit = 10
    for token in tokens:
        if token.isdigit():
            limit = max(1, min(int(token), 25))
            continue
        if not ref:
            ref = token
    ref_lower = ref.lower()
    parsed: dict[str, Any] = {"thread": "", "contact": "", "limit": limit}
    if ref_lower.endswith("@lid") or ref_lower.endswith("@s.whatsapp.net"):
        parsed["contact"] = ref
    elif ref:
        parsed["thread"] = ref
    return parsed


def _format_counts(counts: dict[str, Any]) -> str:
    parts = []
    for key, value in sorted((counts or {}).items()):
        try:
            count = int(value)
        except Exception:
            continue
        parts.append(f"{_safe_text(key, max_len=40)}={count}")
    return ", ".join(parts) if parts else "nenhum"


def _format_event(event: dict[str, Any], idx: int) -> list[str]:
    created = _safe_text(event.get("created_at", ""), max_len=32) or "sem horário"
    msg_type = _safe_text(event.get("message_type", "unknown"), max_len=40) or "unknown"
    status = _safe_text(event.get("status", ""), max_len=40) or "sem status"
    has_media = "sim" if event.get("has_media") else "não"
    lines = [f"{idx}. {created} · tipo={msg_type} · status={status} · mídia={has_media}"]

    preview = _safe_text(event.get("text_preview", ""), max_len=220)
    if preview:
        lines.append(f"   prévia: {preview}")

    media = event.get("media") if isinstance(event.get("media"), dict) else {}
    if media:
        safe_media: list[str] = []
        for key, value in sorted(media.items()):
            if key == "has_media":
                continue
            safe_media.append(f"{_safe_text(key, max_len=32)}={_safe_text(value, max_len=80)}")
        if safe_media:
            lines.append("   mídia: " + ", ".join(safe_media[:6]))

    actions = event.get("suggested_actions")
    if isinstance(actions, list) and actions:
        safe_actions = [_safe_text(action, max_len=60) for action in actions[:5]]
        lines.append("   ações sugeridas: " + ", ".join(action for action in safe_actions if action))
    return lines


def render_thread_context_command(
    arg: str = "",
    *,
    context_loader: Callable[..., str] | None = None,
) -> str:
    """Render a PT-BR, operator-safe local thread context summary.

    The default loader is ``tools.whatsapp_ops_tool.wpp_thread_context`` in
    operator mode.  It reads only ``local_inbound_store`` and does not send or
    fetch provider history.
    """
    parsed = _parse_thread_context_args(arg)
    if context_loader is None:
        from tools.whatsapp_ops_tool import wpp_thread_context as default_loader  # type: ignore[import-not-found]

        loader: Callable[..., str] = default_loader
    else:
        loader = context_loader

    try:
        raw = loader(
            thread=parsed["thread"],
            contact=parsed["contact"],
            limit=parsed["limit"],
            mode="operator",
            max_text_chars=180,
        )
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        return "\n".join([
            "📲 WhatsApp Ops — contexto local (somente leitura)",
            f"Falha ao ler local_inbound_store: {_safe_text(exc, max_len=160)}",
            "Nenhum envio foi disparado e nenhum histórico do provedor foi buscado.",
        ])

    if not isinstance(data, dict) or data.get("ok") is not True:
        err = _safe_text((data or {}).get("error") if isinstance(data, dict) else "resposta inválida", max_len=160)
        return "\n".join([
            "📲 WhatsApp Ops — contexto local (somente leitura)",
            f"Não foi possível montar o contexto: {err or 'erro desconhecido'}",
            "Nenhum envio foi disparado e nenhum histórico do provedor foi buscado.",
        ])

    source = _safe_text(data.get("source") or "local_inbound_store", max_len=80)
    count = int(data.get("message_count") or 0)
    filter_bits = []
    if data.get("thread_filter_set"):
        filter_bits.append("conversa")
    if data.get("contact_filter_set"):
        filter_bits.append("contato")
    filtro = "+".join(filter_bits) if filter_bits else "recentes locais"

    lines = [
        "📲 WhatsApp Ops — contexto local (somente leitura)",
        f"Fonte: {source} · modo: operador · filtro: {filtro} · mensagens: {count}",
        f"Tipos: {_format_counts(data.get('type_counts') or {})}",
        f"Mídias: {_format_counts(data.get('media_counts') or {})}",
        "Garantias: não envia, não busca histórico do provedor, não imprime refs/telefones/URLs/mídia bruta.",
    ]

    events = data.get("events") if isinstance(data.get("events"), list) else []
    if not events:
        lines.append("Nenhuma mensagem local encontrada no escopo informado.")
    else:
        lines.append("Eventos locais:")
        for idx, event in enumerate(events[: parsed["limit"]], start=1):
            if isinstance(event, dict):
                lines.extend(_format_event(event, idx))

    return "\n".join(_safe_text(line, max_len=600) for line in lines)
