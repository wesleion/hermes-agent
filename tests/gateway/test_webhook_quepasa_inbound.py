"""WebhookAdapter receive-only QuePasa inbound tests.

These tests lock the Gate 1.21 contract: a QuePasa inbound route may ingest a
sanitized event into the WhatsApp Ops store, but it must not dispatch an agent
run, direct-deliver a message, or send WhatsApp transport.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from tools.whatsapp_ops_tool import wpp_inbound_lookup


pytestmark = pytest.mark.asyncio


SECRET = "test-quepasa-secret"
ROUTE = "quepasa-inbound"


def _make_adapter(
    *,
    routes: dict | None = None,
    max_body_bytes: int = 4096,
    use_secret_env: bool = False,
) -> WebhookAdapter:
    default_route = {
        "kind": "quepasa_inbound",
        "events": [],
    }
    if use_secret_env:
        default_route["secret_env"] = "WHATSAPP_OPS_WEBHOOK_SECRET"
    else:
        default_route["secret"] = SECRET
    config = PlatformConfig(
        enabled=True,
        extra={
            "host": "127.0.0.1",
            "port": 0,
            "rate_limit": 10,
            "max_body_bytes": max_body_bytes,
            "routes": routes or {ROUTE: default_route},
        },
    )
    adapter = WebhookAdapter(config)
    adapter.handle_message = AsyncMock()
    adapter._direct_deliver = AsyncMock()
    return adapter


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _signature(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(event_id: str = "msg_synth_001") -> dict:
    return {
        "id": event_id,
        "event_type": "message",
        "chat": {"id": "5511999999999@s.whatsapp.net", "type": "individual"},
        "participant": {"id": "5511999999999@s.whatsapp.net", "name": "Synthetic Lead"},
        "message": {"conversation": "Oi, quero saber mais sobre o projeto."},
        "apikey": "synthetic-secret-that-must-not-leak",
        "server_url": "https://quepasa.example.invalid",
    }


async def _post(adapter: WebhookAdapter, payload: dict, *, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {}
    if signature is not None:
        headers["X-Webhook-Signature"] = signature
    server = TestServer(_create_app(adapter))
    async with TestClient(server) as client:
        resp = await client.post(f"/webhooks/{ROUTE}", data=body, headers=headers)
        try:
            data = await resp.json()
        except Exception:
            data = {"raw": await resp.text()}
        return resp.status, data


async def _post_chunked(adapter: WebhookAdapter, payload: dict, *, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {}
    if signature is not None:
        headers["X-Webhook-Signature"] = signature

    async def chunks():
        yield body[:8]
        yield body[8:]

    server = TestServer(_create_app(adapter))
    async with TestClient(server) as client:
        resp = await client.post(f"/webhooks/{ROUTE}", data=chunks(), headers=headers)
        try:
            data = await resp.json()
        except Exception:
            data = {"raw": await resp.text()}
        return resp.status, data


async def test_quepasa_inbound_route_ingests_without_agent_or_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _make_adapter()
    body_payload = _payload()
    body = json.dumps(body_payload).encode()

    status, data = await _post(adapter, body_payload, signature=_signature(body))

    assert status == 200
    assert data["status"] == "ingested"
    assert data["route"] == ROUTE
    assert data["deduped"] is False
    assert data["event_id"].startswith("inbound_")
    adapter.handle_message.assert_not_called()
    adapter._direct_deliver.assert_not_called()

    lookup = json.loads(wpp_inbound_lookup(limit=5))
    assert lookup["ok"] is True
    assert lookup["events"]
    serialized = json.dumps(lookup, ensure_ascii=False)
    assert "5511999999999" not in serialized
    assert "synthetic-secret-that-must-not-leak" not in serialized
    assert "s.whatsapp.net" not in serialized
    assert "<redacted>" in serialized


async def test_quepasa_inbound_route_dedupes_persistently(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _make_adapter()
    body_payload = _payload("msg_duplicate_001")
    body = json.dumps(body_payload).encode()

    first_status, first_data = await _post(adapter, body_payload, signature=_signature(body))
    second_status, second_data = await _post(adapter, body_payload, signature=_signature(body))

    assert first_status == 200
    assert second_status == 200
    assert first_data["status"] == "ingested"
    assert second_data["status"] == "duplicate"
    assert second_data["deduped"] is True
    assert first_data["event_id"] == second_data["event_id"]
    adapter.handle_message.assert_not_called()
    adapter._direct_deliver.assert_not_called()


async def test_quepasa_inbound_route_fails_closed_without_valid_signature(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _make_adapter()

    no_sig_status, _ = await _post(adapter, _payload("msg_auth_001"), signature=None)
    bad_sig_status, _ = await _post(adapter, _payload("msg_auth_002"), signature="bad")

    assert no_sig_status == 401
    assert bad_sig_status == 401
    adapter.handle_message.assert_not_called()
    adapter._direct_deliver.assert_not_called()
    assert json.loads(wpp_inbound_lookup(limit=5))["events"] == []


async def test_quepasa_inbound_route_accepts_secret_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WHATSAPP_OPS_WEBHOOK_SECRET", SECRET)
    adapter = _make_adapter(use_secret_env=True)
    body_payload = _payload("msg_env_secret_001")
    body = json.dumps(body_payload).encode()

    status, data = await _post(adapter, body_payload, signature=_signature(body))

    assert status == 200
    assert data["status"] == "ingested"
    adapter.handle_message.assert_not_called()
    adapter._direct_deliver.assert_not_called()


async def test_quepasa_inbound_route_secret_env_fails_closed_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("WHATSAPP_OPS_WEBHOOK_SECRET", raising=False)
    adapter = _make_adapter(use_secret_env=True)
    body_payload = _payload("msg_env_missing_001")
    body = json.dumps(body_payload).encode()

    status, data = await _post(adapter, body_payload, signature=_signature(body))

    assert status == 403
    assert data["error"] == "Webhook route is missing an HMAC secret"
    adapter.handle_message.assert_not_called()
    adapter._direct_deliver.assert_not_called()
    assert json.loads(wpp_inbound_lookup(limit=5))["events"] == []


async def test_quepasa_inbound_requires_source_event_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _make_adapter()
    body_payload = _payload("")
    body_payload.pop("id", None)
    body = json.dumps(body_payload).encode()

    status, data = await _post(adapter, body_payload, signature=_signature(body))

    assert status == 400
    assert data["status"] == "error"
    assert data["error"] == "source_event_id_required"
    adapter.handle_message.assert_not_called()
    adapter._direct_deliver.assert_not_called()


async def test_quepasa_inbound_route_respects_body_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _make_adapter(max_body_bytes=16)
    body_payload = _payload("msg_large_001")
    body = json.dumps(body_payload).encode()

    status, _ = await _post(adapter, body_payload, signature=_signature(body))

    assert status == 413
    adapter.handle_message.assert_not_called()
    adapter._direct_deliver.assert_not_called()


async def test_quepasa_inbound_route_respects_body_limit_without_content_length(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _make_adapter(max_body_bytes=16)
    body_payload = _payload("msg_chunked_large_001")
    body = json.dumps(body_payload).encode()

    status, _ = await _post_chunked(adapter, body_payload, signature=_signature(body))

    assert status == 413
    adapter.handle_message.assert_not_called()
    adapter._direct_deliver.assert_not_called()
