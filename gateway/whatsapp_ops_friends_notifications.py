"""One-shot exception delivery to the existing, approved operator destination."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from hermes_cli.whatsapp_ops_commands import _safe_text
from tools.whatsapp_ops_batch import _conn


def drain_friends_exceptions(
    profile_id: str, *, client=None, token: str | None = None
) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "") if token is None else token
    if not token:
        return 0
    delivered = 0
    with _conn() as conn:
        rows = conn.execute(
            "SELECT q.*,p.profile_id,p.chat_id,p.thread_id,c.display_name FROM friends_qualifications q JOIN friends_grants g ON g.grant_id=q.grant_id JOIN friends_pending_envelopes p ON p.envelope_digest=g.envelope_digest JOIN contacts c ON c.id=q.contact_id WHERE p.profile_id=? AND q.outcome IN ('escalate','stop') ORDER BY q.created_at LIMIT 30",
            (profile_id,),
        ).fetchall()
    for row in rows:
        key = hashlib.sha256(
            "|".join(
                str(row[x])
                for x in (
                    "grant_id",
                    "contact_id",
                    "channel_id",
                    "created_at",
                    "outcome",
                )
            ).encode()
        ).hexdigest()
        stamp = datetime.now(timezone.utc).isoformat()
        with _conn() as conn:
            claimed = conn.execute(
                "INSERT OR IGNORE INTO friends_notifications(event_key,status,created_at) VALUES (?,'claimed',?)",
                (key, stamp),
            ).rowcount
        if not claimed:
            continue
        label = _safe_text(row["display_name"], max_len=80)
        detail = json.loads(row["detail_json"] or "{}")
        reason = detail.get("reason", "")
        hints = {
            "opt_out": "Pediu para parar; mensagens bloqueadas.",
            "ambiguous_opt_out": "Possível pedido de parada; mensagens bloqueadas.",
            "commercial_exception": "Falta uma condição comercial aprovada; precisa de você.",
            "generation_invalid_or_timeout": "Não foi possível concluir a resposta com segurança.",
            "send_failed_or_uncertain": "Entrega sem confirmação; não reenviar antes de reconciliar.",
        }
        message = hints.get(
            reason, "Conversa pausada; confira o status do piloto antes de continuar."
        )
        payload = {
            "chat_id": row["chat_id"],
            "text": f"Piloto — {label}\n{message}\nUse /piloto status. As demais conversas seguem seus limites.",
            "disable_web_page_preview": True,
        }
        if row["thread_id"]:
            payload["message_thread_id"] = row["thread_id"]
        status, receipt = "uncertain", None
        try:
            request = Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps(payload, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with (client or urlopen)(request, timeout=10) as response:
                ack = json.loads(response.read().decode())
            if (
                ack.get("ok") is True
                and isinstance(ack.get("result"), dict)
                and ack["result"].get("message_id") is not None
            ):
                status = "sent"
                receipt = hashlib.sha256(
                    str(ack["result"]["message_id"]).encode()
                ).hexdigest()
                delivered += 1
        except Exception:
            # Claimed/uncertain events are never automatically posted twice.
            pass
        with _conn() as conn:
            conn.execute(
                "UPDATE friends_notifications SET status=?,receipt_hash=? WHERE event_key=?",
                (status, receipt, key),
            )
    return delivered


def drain_friends_approval_cards(
    profile_id: str, *, client=None, token: str | None = None
) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "") if token is None else token
    if not token:
        return 0
    stamp = datetime.now(timezone.utc).isoformat()
    delivered = 0
    with _conn() as conn:
        pending = conn.execute(
            "SELECT * FROM friends_pending_envelopes WHERE profile_id=? AND status='pending' AND expires_at>? ORDER BY created_at LIMIT 10",
            (profile_id, stamp),
        ).fetchall()
    for row in pending:
        key = "approval:" + row["pending_id"]
        with _conn() as conn:
            claimed = conn.execute(
                "INSERT OR IGNORE INTO friends_notifications(event_key,status,created_at) VALUES (?,'claimed',?)",
                (key, stamp),
            ).rowcount
            labels = []
            envelope = json.loads(row["envelope_json"])
            for bound in envelope["contacts"]:
                contact = conn.execute(
                    "SELECT display_name FROM contacts WHERE id=?",
                    (bound["contact_id"],),
                ).fetchone()
                labels.append(
                    _safe_text(contact[0], max_len=80)
                    if contact
                    else "contato indisponível"
                )
        if not claimed:
            continue
        caps = envelope["caps"]
        text = (
            "Piloto consentido — autorização única\n" + ", ".join(labels) + "\n"
            "Agentes e infraestrutura agêntica; qualificar e encaminhar briefing. Sem preço ou prazo definido.\n"
            "Ao aprovar, Hunter inicia e responde sozinho aos três contatos. Exceções chamam você.\n"
            f"Janela: {envelope['starts_at']} até {envelope['expires_at']}\n"
            f"Limites: {caps['max_turns']} turnos, {caps['max_messages']} mensagens/contato, {caps['max_blocks']} blocos/resposta; {caps['global_cap']} no total.\n"
            "Um follow-up após 30 min. Parada por opt-out, /piloto encerrar ou expiração. Nenhum terceiro ou cobrança."
        )
        payload = {
            "chat_id": row["chat_id"],
            "text": text,
            "disable_web_page_preview": True,
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "Autorizar lote",
                            "callback_data": "wppf:a:" + row["pending_id"],
                        },
                        {
                            "text": "Negar",
                            "callback_data": "wppf:d:" + row["pending_id"],
                        },
                    ]
                ]
            },
        }
        if row["thread_id"]:
            payload["message_thread_id"] = row["thread_id"]
        status, receipt = "uncertain", None
        try:
            request = Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps(payload, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with (client or urlopen)(request, timeout=10) as response:
                ack = json.loads(response.read().decode())
            if (
                ack.get("ok") is True
                and isinstance(ack.get("result"), dict)
                and ack["result"].get("message_id") is not None
            ):
                status = "sent"
                receipt = hashlib.sha256(
                    str(ack["result"]["message_id"]).encode()
                ).hexdigest()
                delivered += 1
        except Exception:
            pass
        with _conn() as conn:
            conn.execute(
                "UPDATE friends_notifications SET status=?,receipt_hash=? WHERE event_key=?",
                (status, receipt, key),
            )
    return delivered


def drain_friends_operator_events(profile_id: str) -> None:
    drain_friends_approval_cards(profile_id)
    drain_friends_exceptions(profile_id)
