"""Tests for read-only WhatsApp Ops command UX helpers."""

from __future__ import annotations

import json

from hermes_cli.commands import COMMAND_REGISTRY
from hermes_cli.whatsapp_ops_commands import render_thread_context_command


def test_ctxwpp_command_is_registered_as_gated_whatsapp_ops_command():
    command = next((cmd for cmd in COMMAND_REGISTRY if cmd.name == "ctxwpp"), None)

    assert command is not None
    assert command.category == "WhatsApp Ops"
    assert command.gateway_config_gate == "whatsapp_ops.slash_commands_enabled"
    assert "wpp_thread_context" in command.aliases


def test_render_thread_context_command_redacts_transport_refs_and_media_blobs():
    def fake_loader(**kwargs):
        assert kwargs["mode"] == "operator"
        assert kwargs["max_text_chars"] == 180
        return json.dumps(
            {
                "ok": True,
                "source": "local_inbound_store",
                "message_count": 1,
                "thread_filter_set": True,
                "contact_filter_set": False,
                "type_counts": {"text": 1},
                "media_counts": {"audio": 1},
                "events": [
                    {
                        "created_at": "2026-06-27T12:00:00+00:00",
                        "message_type": "text",
                        "status": "received",
                        "has_media": True,
                        "text_preview": (
                            "cliente 5511999999999 em grupo 120363000000000000@g.us "
                            "com contato user@lid link https://example.invalid/a "
                            "data:audio/ogg;base64,T3J0aVNwZWNpZmljVGVzdERhdGE="
                        ),
                        "media": {
                            "url": "https://cdn.invalid/file.ogg?token=secret",
                            "sha": "T3J0aVNwZWNpZmljVGVzdExvbmdCYXNlNjRTdHJpbmc=",
                        },
                        "suggested_actions": ["wpp_transcribe_media"],
                    }
                ],
            },
            ensure_ascii=False,
        )

    rendered = render_thread_context_command("120363000000000000@g.us 5", context_loader=fake_loader)

    assert "WhatsApp Ops — contexto local" in rendered
    assert "somente leitura" in rendered
    assert "não envia" in rendered
    assert "não busca histórico do provedor" in rendered
    assert "5511999999999" not in rendered
    assert "@g.us" not in rendered
    assert "@lid" not in rendered
    assert "https://" not in rendered
    assert "data:audio" not in rendered
    assert "T3J0aVNwZWNpZmlj" not in rendered
    assert "<telefone-redigido>" in rendered
    assert "<ref-redigida>" in rendered
    assert "<url-redigida>" in rendered


def test_render_thread_context_command_reports_safe_failure():
    def failing_loader(**kwargs):
        raise RuntimeError("token=abc https://secret.invalid user@lid")

    rendered = render_thread_context_command("user@lid", context_loader=failing_loader)

    assert "Falha ao ler local_inbound_store" in rendered
    assert "token=abc" not in rendered
    assert "https://" not in rendered
    assert "@lid" not in rendered
    assert "Nenhum envio foi disparado" in rendered
