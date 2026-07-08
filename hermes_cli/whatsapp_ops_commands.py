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

_RAW_WA_REF_RE = re.compile(r"(?i)\b[\w.-]+@(?:g\.us|lid|s\.whatsapp\.net|c\.us)\b")
_URL_RE = re.compile(r"(?i)\bhttps?://\S+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_DATA_B64_RE = re.compile(r"(?i)data:[^\s,]*?;base64,[A-Za-z0-9+/=]{16,}")
_LONG_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b")
_MEDIA_WORD_RE = re.compile(r"(?i)\b(?:base64|blob)\b")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|api[_-]?key|authorization|password|secret)\s*[=:]\s*\S+"
)
_RAW_THREAD_SUFFIXES = ("@g.us",)
_RAW_CONTACT_SUFFIXES = ("@lid", "@s.whatsapp.net", "@c.us")


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


def _token_is_raw_thread_ref(token: str) -> bool:
    lowered = str(token or "").strip().lower()
    return lowered.endswith(_RAW_THREAD_SUFFIXES)


def _token_is_raw_contact_ref(token: str) -> bool:
    lowered = str(token or "").strip().lower()
    return lowered.endswith(_RAW_CONTACT_SUFFIXES)


def _parse_targeted_args(arg: str, *, default_limit: int, max_limit: int) -> dict[str, Any]:
    """Parse `/ctxwpp|sumwpp [target|item N|limit]` safely."""
    tokens = [part.strip() for part in str(arg or "").split() if part.strip()]
    parsed: dict[str, Any] = {
        "thread": "",
        "contact": "",
        "target": "",
        "item": 0,
        "limit": default_limit,
        "help": False,
        "error": "",
        "technical_filter": False,
    }
    target_parts: list[str] = []
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        token_lower = token.lower()
        if token_lower in {"help", "ajuda", "?", "uso"}:
            parsed["help"] = True
            idx += 1
            continue
        if token_lower in {"item", "fila", "--item"}:
            if idx + 1 < len(tokens) and tokens[idx + 1].isdigit():
                parsed["item"] = int(tokens[idx + 1])
                idx += 2
                continue
            parsed["error"] = "item_without_number"
            idx += 1
            continue
        if token_lower.startswith("--item=") and token_lower.split("=", 1)[1].isdigit():
            parsed["item"] = int(token_lower.split("=", 1)[1])
            idx += 1
            continue
        if token.isdigit():
            parsed["limit"] = max(1, min(int(token), max_limit))
            idx += 1
            continue
        if token_lower in {"thread", "conversa", "grupo"}:
            if idx + 1 < len(tokens) and not tokens[idx + 1].isdigit():
                ref = tokens[idx + 1]
                parsed["thread"] = ref
                parsed["technical_filter"] = True
                idx += 2
                continue
            parsed["error"] = "thread_without_ref"
            idx += 1
            continue
        if token_lower in {"contact", "contato", "dm"}:
            if idx + 1 < len(tokens) and not tokens[idx + 1].isdigit():
                ref = tokens[idx + 1]
                parsed["contact"] = ref
                parsed["technical_filter"] = True
                idx += 2
                continue
            parsed["error"] = "contact_without_ref"
            idx += 1
            continue
        if _token_is_raw_thread_ref(token):
            parsed["thread"] = token
            parsed["technical_filter"] = True
        elif _token_is_raw_contact_ref(token):
            parsed["contact"] = token
            parsed["technical_filter"] = True
        else:
            target_parts.append(token)
        idx += 1
    if target_parts:
        parsed["target"] = " ".join(target_parts)
    return parsed


def _thread_context_usage_lines() -> list[str]:
    return [
        "📲 WhatsApp Ops — contexto local (somente leitura)",
        "Uso operacional:",
        "- /ctxwpp — mostra as últimas 10 mensagens operacionais locais já ingeridas.",
        "- /ctxwpp 20 — mostra até 20 eventos locais; máximo 25.",
        "- /ctxwpp H-Ops 20 — resolve grupo/contato por nome local ou fila recente.",
        "- /ctxwpp item 1 20 — usa o item exatamente como aparece em /fila.",
        "- /ctxwpp thread <ref> 10 ou /ctxwpp contact <ref> 10 — filtro técnico quando uma ref já é conhecida.",
        "",
        "Sem argumento ele NÃO adivinha um grupo por intenção; ele usa o local_inbound_store mais recente.",
        "Não busca histórico do provedor, não envia WhatsApp e não imprime refs/telefones/URLs/mídia bruta.",
    ]


def _summary_usage_lines() -> list[str]:
    return [
        "🧾 WhatsApp Ops — resumo local determinístico (somente leitura)",
        "Uso operacional:",
        "- /sumwpp H-Ops 50 — resume até 50 eventos locais do grupo/contato resolvido.",
        "- /sumwpp item 1 50 — resume o alvo do item mostrado em /fila.",
        "- /sumwpp 50 — resumo não filtrado dos eventos locais mais recentes.",
        "Limite padrão 50; máximo 100. Não usa LLM, não busca histórico do provedor, não persiste resumo e não envia WhatsApp.",
    ]


def _format_counts(counts: dict[str, Any]) -> str:
    parts = []
    for key, value in sorted((counts or {}).items()):
        try:
            count = int(value)
        except Exception:
            continue
        if count <= 0:
            continue
        parts.append(f"{_safe_text(key, max_len=40)}={count}")
    return ", ".join(parts) if parts else "nenhum"


def _count_visible_events(events: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    type_counts: dict[str, int] = {}
    media_counts: dict[str, int] = {}
    for event in events:
        msg_type = _safe_text(event.get("message_type") or "unknown", max_len=40) or "unknown"
        type_counts[msg_type] = type_counts.get(msg_type, 0) + 1
        if event.get("has_media"):
            media_counts[msg_type] = media_counts.get(msg_type, 0) + 1
    return type_counts, media_counts


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


def _resolve_target_for_command(
    parsed: dict[str, Any],
    *,
    resolver: Callable[..., dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str, str, list[str]]:
    """Return (target_public, thread, contact, error_lines)."""
    if parsed.get("thread") or parsed.get("contact"):
        return None, str(parsed.get("thread") or ""), str(parsed.get("contact") or ""), []
    target = str(parsed.get("target") or "").strip()
    item = int(parsed.get("item") or 0)
    if not target and item <= 0:
        return None, "", "", []
    if resolver is None:
        from tools.whatsapp_ops_store import resolve_conversation_target as default_resolver  # type: ignore[import-not-found]

        resolver = default_resolver
    assert resolver is not None
    resolved = resolver(query=target, item_index=item, include_transport=True)
    if not isinstance(resolved, dict) or resolved.get("ok") is not True:
        err = _safe_text((resolved or {}).get("error") if isinstance(resolved, dict) else "resolver_invalid", max_len=120)
        hint = _safe_text((resolved or {}).get("hint") if isinstance(resolved, dict) else "", max_len=240)
        return None, "", "", [
            f"Não consegui resolver o alvo: {err or 'erro desconhecido'}.",
            hint or "Use /fila para ver itens recentes ou informe um nome mais específico.",
        ]
    if resolved.get("ambiguous"):
        lines = ["Alvo ambíguo. Use /ctxwpp item N pela fila ou refine o nome."]
        matches_obj = resolved.get("matches")
        matches: list[Any] = matches_obj if isinstance(matches_obj, list) else []
        for idx, match in enumerate(matches[:6], start=1):
            if isinstance(match, dict):
                lines.append(
                    f"{idx}. {_safe_text(match.get('target_kind'), max_len=30)} · "
                    f"{_safe_text(match.get('target_label'), max_len=80)} · "
                    f"{_safe_text(match.get('source'), max_len=40)}"
                )
        return None, "", "", lines
    return resolved, str(resolved.get("_thread_ref") or ""), str(resolved.get("_contact_ref") or ""), []


def render_thread_context_command(
    arg: str = "",
    *,
    context_loader: Callable[..., str] | None = None,
    target_resolver: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Render a PT-BR, operator-safe local thread context summary."""
    parsed = _parse_targeted_args(arg, default_limit=10, max_limit=25)
    if parsed.get("help"):
        return "\n".join(_safe_text(line, max_len=600) for line in _thread_context_usage_lines())
    if parsed.get("error"):
        lines = _thread_context_usage_lines()
        lines.insert(1, "Filtro incompleto: use /ctxwpp item N, /ctxwpp <nome> 10, ou /ctxwpp thread <ref> 10.")
        return "\n".join(_safe_text(line, max_len=600) for line in lines)

    target_public, thread_ref, contact_ref, target_errors = _resolve_target_for_command(parsed, resolver=target_resolver)
    if target_errors:
        return "\n".join([
            "📲 WhatsApp Ops — contexto local (somente leitura)",
            *(_safe_text(line, max_len=600) for line in target_errors),
            "Nenhum envio foi disparado e nenhum histórico do provedor foi buscado.",
        ])

    if context_loader is None:
        from tools.whatsapp_ops_tool import wpp_thread_context as default_loader  # type: ignore[import-not-found]

        loader: Callable[..., str] = default_loader
    else:
        loader = context_loader

    try:
        raw = loader(
            thread=thread_ref,
            contact=contact_ref,
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
    raw_events = data.get("events")
    all_events = raw_events if isinstance(raw_events, list) else []
    events = [
        event for event in all_events
        if isinstance(event, dict) and str(event.get("message_type") or "").strip().lower() != "system"
    ]
    visible_type_counts, visible_media_counts = _count_visible_events(events)
    fetched_count = len(all_events)
    visible_count = len(events)
    system_hidden = max(0, fetched_count - visible_count)
    filter_bits = []
    if data.get("thread_filter_set"):
        filter_bits.append("conversa informada")
    if data.get("contact_filter_set"):
        filter_bits.append("contato informado")
    filtro = "+".join(filter_bits) if filter_bits else "últimos eventos locais ingeridos"
    if target_public:
        scope_hint = (
            "Alvo: "
            f"{_safe_text(target_public.get('target_label'), max_len=100)} "
            f"({_safe_text(target_public.get('target_kind'), max_len=30)} · {_safe_text(target_public.get('source'), max_len=40)})"
        )
    elif filter_bits:
        scope_hint = "Escopo: filtro técnico informado no comando."
    else:
        scope_hint = "Escopo: sem filtro específico; usa o local_inbound_store mais recente."

    lines = [
        "📲 WhatsApp Ops — contexto local (somente leitura)",
        f"Fonte: {source} · modo: operador",
        scope_hint,
        f"Filtro: {filtro}",
        f"Limite pedido: {parsed['limit']} eventos locais (padrão 10; máximo 25) · exibidas: {visible_count}",
    ]
    if system_hidden:
        lines.append(f"Ocultos: {system_hidden} evento(s) system/conexão não operacional.")
    lines.extend([
        f"Tipos exibidos: {_format_counts(visible_type_counts)}",
        f"Mídias exibidas: {_format_counts(visible_media_counts)}",
        "Garantias: não envia, não busca histórico do provedor, não imprime refs/telefones/URLs/mídia bruta.",
    ])

    if not events:
        lines.append("Nenhuma mensagem operacional local encontrada no escopo informado.")
    else:
        lines.append("Eventos locais:")
        for idx, event in enumerate(events[: parsed["limit"]], start=1):
            if isinstance(event, dict):
                lines.extend(_format_event(event, idx))

    return "\n".join(_safe_text(line, max_len=600) for line in lines)


def render_conversation_summary_command(
    arg: str = "",
    *,
    summary_loader: Callable[..., str] | None = None,
    target_resolver: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Render a deterministic local WhatsApp conversation summary."""
    parsed = _parse_targeted_args(arg, default_limit=50, max_limit=100)
    if parsed.get("help"):
        return "\n".join(_safe_text(line, max_len=600) for line in _summary_usage_lines())
    if parsed.get("error"):
        lines = _summary_usage_lines()
        lines.insert(1, "Filtro incompleto: use /sumwpp item N, /sumwpp <nome> 50, ou /sumwpp 50.")
        return "\n".join(_safe_text(line, max_len=600) for line in lines)

    target_public, thread_ref, contact_ref, target_errors = _resolve_target_for_command(parsed, resolver=target_resolver)
    if target_errors:
        return "\n".join([
            "🧾 WhatsApp Ops — resumo local determinístico (somente leitura)",
            *(_safe_text(line, max_len=600) for line in target_errors),
            "Nenhum envio foi disparado, nenhum resumo foi persistido e nenhum histórico do provedor foi buscado.",
        ])

    if summary_loader is None:
        from tools.whatsapp_ops_tool import wpp_conversation_summary as default_loader  # type: ignore[import-not-found]

        loader: Callable[..., str] = default_loader
    else:
        loader = summary_loader
    try:
        raw = loader(
            thread=thread_ref,
            contact=contact_ref,
            limit=parsed["limit"],
            mode="brief",
            max_text_chars=180,
            include_evidence=False,
        )
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        return "\n".join([
            "🧾 WhatsApp Ops — resumo local determinístico (somente leitura)",
            f"Falha ao resumir local_inbound_store: {_safe_text(exc, max_len=160)}",
            "Nenhum envio foi disparado, nenhum resumo foi persistido e nenhum histórico do provedor foi buscado.",
        ])

    if not isinstance(data, dict) or data.get("ok") is not True:
        err = _safe_text((data or {}).get("error") if isinstance(data, dict) else "resposta inválida", max_len=160)
        return "\n".join([
            "🧾 WhatsApp Ops — resumo local determinístico (somente leitura)",
            f"Não foi possível montar o resumo: {err or 'erro desconhecido'}",
            "Nenhum envio foi disparado, nenhum resumo foi persistido e nenhum histórico do provedor foi buscado.",
        ])

    if target_public:
        scope_hint = (
            "Alvo: "
            f"{_safe_text(target_public.get('target_label'), max_len=100)} "
            f"({_safe_text(target_public.get('target_kind'), max_len=30)} · {_safe_text(target_public.get('source'), max_len=40)})"
        )
    elif data.get("thread_filter_set") or data.get("contact_filter_set"):
        scope_hint = "Escopo: filtro técnico informado no comando."
    else:
        scope_hint = "Escopo: sem filtro específico; resumo dos eventos locais mais recentes."

    type_counts_obj = data.get("type_counts")
    media_counts_obj = data.get("media_counts")
    type_counts: dict[str, Any] = type_counts_obj if isinstance(type_counts_obj, dict) else {}
    media_counts: dict[str, Any] = media_counts_obj if isinstance(media_counts_obj, dict) else {}
    lines = [
        "🧾 WhatsApp Ops — resumo local determinístico (somente leitura)",
        scope_hint,
        f"Fonte: {_safe_text(data.get('source'), max_len=80)} · gerador: {_safe_text(data.get('generated_by'), max_len=80)}",
        f"Limite pedido: {parsed['limit']} eventos locais (padrão 50; máximo 100) · analisados: {int(data.get('message_count') or 0)}",
        f"Tipos: {_format_counts(type_counts)}",
        f"Mídias: {_format_counts(media_counts)}",
        "Garantias: llm_used=false · provider_history_used=false · send_performed=false · summary_persisted=false.",
    ]
    headline = _safe_text(data.get("headline"), max_len=300)
    if headline:
        lines.extend(["", f"Resumo: {headline}"])
    bullets = data.get("bullets") if isinstance(data.get("bullets"), list) else []
    if bullets:
        lines.append("Pontos:")
        for bullet in bullets[:8]:
            lines.append(f"- {_safe_text(bullet, max_len=260)}")
    previews = data.get("latest_previews") if isinstance(data.get("latest_previews"), list) else []
    if previews:
        lines.append("Últimas prévias:")
        for preview in previews[:5]:
            if isinstance(preview, dict):
                created = _safe_text(preview.get("created_at"), max_len=32) or "sem horário"
                msg_type = _safe_text(preview.get("message_type"), max_len=40) or "unknown"
                text = _safe_text(preview.get("text_preview"), max_len=180)
                if text:
                    lines.append(f"- {created} · {msg_type}: {text}")
    actions = data.get("suggested_actions") if isinstance(data.get("suggested_actions"), list) else []
    if actions:
        safe_actions = [_safe_text(action, max_len=80) for action in actions[:6]]
        lines.append("Ações sugeridas locais: " + ", ".join(action for action in safe_actions if action))
    warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
    if warnings:
        lines.append("Warnings: " + ", ".join(_safe_text(warning, max_len=60) for warning in warnings[:6]))
    return "\n".join(_safe_text(line, max_len=700) for line in lines)
