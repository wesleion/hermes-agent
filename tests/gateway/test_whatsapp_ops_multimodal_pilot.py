"""Real ingest/ledger/worker/dispatcher contracts. Only perception/provider are fakes."""

from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import whatsapp_ops_batch as batch
from tools import whatsapp_ops_store as store
from tools import whatsapp_ops_tool as tool
from gateway.whatsapp_ops_batch_approval import issue_authenticated_friends_authority
from gateway.whatsapp_ops_batch_dispatch import FriendsBatchDispatcher
from gateway.whatsapp_ops_media_worker import WhatsAppOpsMediaWorker


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    token = set_hermes_home_override(tmp_path)
    now = [datetime.now(timezone.utc)]
    monkeypatch.setattr(batch, "_now", lambda: now[0])
    monkeypatch.setattr(store, "utc_now", lambda: now[0].isoformat())
    refs = [f"5511888800{i}@s.whatsapp.net" for i in range(4)]
    peers = []
    for i, ref in enumerate(refs):
        contact = store.register_contact_local(
            alias=f"actor-{i}", raw_ref=ref, allow_send=True
        )
        peers.append({
            "contact_id": contact["contact_id"],
            "channel_id": store.list_contact_channels(contact["contact_id"])[0][
                "channel_id"
            ],
        })
    preview = batch.prepare_friends_envelope(
        campaign_id="multimodal-synthetic",
        contacts=peers[:3],
        offer={"id": "agents"},
        playbook={"id": "v1"},
        issuer="telegram:7",
        starts_at=now[0].isoformat(),
        expires_at=(now[0] + timedelta(hours=2)).isoformat(),
        actions=["qualify", "present", "brief", "refer", "escalate", "stop"],
    )
    pending = batch.persist_friends_pending(
        preview,
        profile_id=str(tmp_path.resolve()),
        chat_id="42",
        thread_id="",
        operator_id="7",
    )
    authority = issue_authenticated_friends_authority(
        profile_id=str(tmp_path.resolve()),
        chat_id="42",
        thread_id="",
        operator_id="7",
        pending_id=pending["pending_id"],
        envelope_digest=pending["envelope_digest"],
    )
    grant = batch.activate_friends_pending(
        pending["pending_id"], decision="approved", authority=authority
    )
    assert grant["ok"]
    cfg = {
        "model": {"provider": "openai-codex", "default": "gpt-5.5"},
        "friends_pilot": {"enabled": True},
        "auxiliary": {
            "vision": {
                "provider": "openrouter",
                "model": "google/gemini-2.5-flash-lite",
                "timeout": 5,
            }
        },
        "whatsapp_ops": {
            "send_enabled": True,
            "kill_switch": False,
            "quepasa": {"send_enabled": True},
            "media_perception": {
                "enabled": True,
                "worker_timeout_seconds": 180,
                "cache_ttl_seconds": 86400,
                "local": {"max_threads": 2},
            },
        },
    }
    monkeypatch.setattr(tool, "load_config", lambda: cfg)
    monkeypatch.setattr(
        tool, "_notify_operator_of_recognized_inbound", lambda *args: None
    )
    calls, inputs, downloads = [], [], []

    class Generator:
        def generate(self, **kwargs):
            inputs.append(kwargs)
            return {
                "stage": "discovery",
                "action": "qualify",
                "qualification": {},
                "blocks": ["Como posso ajudar no atendimento?"],
                "next_step": "ouvir",
                "escalation": False,
            }

    def sender(payload, _cfg):
        calls.append(payload)
        return {"ok": True, "message_id_hash": f"receipt-{len(calls)}"}

    dispatcher = FriendsBatchDispatcher(
        generator=Generator(),
        send_client=sender,
        clock=lambda: now[0],
        enabled=lambda: True,
    )
    assert dispatcher.run_once() == 3
    calls.clear()
    inputs.clear()

    def payload(kind="audio", index=0, event="provider-private-id", caption=""):
        p = {
            "id": event,
            "chat": {"id": refs[index]},
            "participant": {"id": refs[index]},
        }
        types = {
            "audio": ("audioMessage", "audio/ogg"),
            "image": ("imageMessage", "image/png"),
            "sticker": ("stickerMessage", "image/webp"),
            "document": ("documentMessage", "application/pdf"),
        }
        if kind == "text":
            p["text"] = caption
        else:
            name, mime = types[kind]
            p["message"] = {name: {"mimetype": mime, "caption": caption}}
        return p

    def ingest(kind="audio", index=0, event="provider-private-id", caption=""):
        r = json.loads(
            tool.wpp_ingest_inbound_event(payload(kind, index, event, caption))
        )
        assert r["ok"]
        return r

    def downloader(handle, cap):
        downloads.append(handle)
        return b"fake-audio-transport", "audio/ogg"

    class Perception:
        def transcribe_audio(self, path):
            return {
                "ok": True,
                "text": "Tenho uma agenda com muitos pedidos.",
                "language": "pt",
                "duration_seconds": 1,
                "truncated": False,
            }

    def decoder(data, kind, mime, config, output_dir):
        p = Path(output_dir) / "normalized.wav"
        p.write_bytes(b"normalized-test-boundary")
        return {
            "ok": True,
            "kind": kind,
            "audio_path": str(p),
            "duration_seconds": 1,
            "mime": "audio/wav",
        }

    def worker(**kwargs):
        return WhatsAppOpsMediaWorker(
            config_loader=lambda: cfg,
            downloader=kwargs.pop("downloader", downloader),
            perception=kwargs.pop("perception", Perception()),
            decoder=kwargs.pop("decoder", decoder),
            clock=lambda: now[0],
            **kwargs,
        )

    try:
        yield SimpleNamespace(
            home=tmp_path,
            now=now,
            refs=refs,
            peers=peers,
            cfg=cfg,
            grant=grant,
            ingest=ingest,
            payload=payload,
            worker=worker,
            dispatcher=dispatcher,
            calls=calls,
            inputs=inputs,
            downloads=downloads,
        )
    finally:
        reset_hermes_home_override(token)


def test_no_private_handle_or_download_for_fourth_contact(pilot):
    p = pilot
    p.ingest(index=3)
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM media_jobs").fetchone()[0] == 0
    assert p.worker().run_once() is False and p.downloads == []


def test_audio_then_text_waits_for_all_evidence_and_preserves_chronology(pilot):
    p = pilot
    first = p.ingest(caption="audio contexto")
    p.now[0] += timedelta(seconds=1)
    p.ingest("text", event="next-text", caption="Também quero melhorar respostas.")
    p.now[0] += timedelta(seconds=5)
    assert p.dispatcher.run_once() == 0 and not p.calls
    assert p.worker().run_once()
    assert p.dispatcher.run_once() == 1
    messages = p.inputs[-1]["messages"]
    inbounds = [m["text"] for m in messages if m["role"] == "user"]
    assert "agenda" in inbounds[-2] and "Também" in inbounds[-1]
    assert "provider-private-id" not in json.dumps(messages)
    with store._connect() as conn:
        assert (
            conn.execute(
                "SELECT provider_handle FROM media_jobs WHERE event_id=?",
                (first["event_id"],),
            ).fetchone()[0]
            == ""
        )


def test_replay_and_two_peers_do_not_duplicate_or_cross_context(pilot):
    p = pilot
    p.ingest(index=0, event="peer0")
    again = p.ingest(index=0, event="peer0")
    assert again["deduped"]
    p.ingest("text", index=1, event="peer1", caption="Só quero uma explicação.")
    p.now[0] += timedelta(seconds=5)
    assert p.dispatcher.run_once() == 1
    assert p.worker().run_once() and not p.worker().run_once()
    assert p.dispatcher.run_once() == 1 and p.downloads == ["peer0"]
    other = [x for x in p.inputs if x["contact_id"] == p.peers[1]["contact_id"]][0]
    assert "agenda" not in json.dumps(other["messages"])


def test_stop_during_inference_cancels_and_late_result_never_reopens(pilot):
    p = pilot
    r = p.ingest()

    class Slow:
        def transcribe_audio(self, path):
            p.ingest("text", event="stop", caption="Pare de enviar mensagens.")
            return {"ok": True, "text": "Estou interessado", "truncated": False}

    p.worker(perception=Slow()).run_once()
    p.now[0] += timedelta(seconds=5)
    assert p.dispatcher.run_once() == 0 and not p.calls
    with store._connect() as conn:
        row = conn.execute(
            "SELECT status,provider_handle FROM media_jobs WHERE event_id=?",
            (r["event_id"],),
        ).fetchone()
        assert row["status"] == "cancelled" and row["provider_handle"] == ""
        assert conn.execute("SELECT count(*) FROM media_evidence").fetchone()[0] == 0


def test_pending_caption_stop_cancels_before_any_download(pilot):
    p = pilot
    p.ingest("image", caption="Não me envie mais mensagens.")
    assert not p.worker().run_once() and p.downloads == []
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM media_jobs").fetchone()[0] == 0


def test_unsupported_document_is_explicit_without_download(pilot):
    p = pilot
    p.ingest("document")
    p.now[0] += timedelta(seconds=5)
    p.worker().run_once()
    assert not p.downloads
    assert p.dispatcher.run_once() == 1
    text = json.dumps(p.inputs[-1]["messages"], ensure_ascii=False)
    assert "documento" in text.lower() and "alternativa" in text.lower()


def test_corrupt_media_keeps_error_explicit_and_unblocks_conversation(pilot):
    p = pilot
    p.ingest()
    p.worker(decoder=lambda *args: {"ok": False, "error": "media_corrupt"}).run_once()
    p.now[0] += timedelta(seconds=5)
    assert p.dispatcher.run_once() == 1
    assert "media_corrupt" in json.dumps(p.inputs[-1]["messages"])


def test_expired_grant_prevents_claim_and_clears_handle(pilot):
    p = pilot
    p.ingest()
    p.now[0] += timedelta(hours=3)
    assert p.worker().run_once() is False and not p.downloads
    with store._connect() as conn:
        row = conn.execute("SELECT status,provider_handle FROM media_jobs").fetchone()
        assert row["status"] == "cancelled" and row["provider_handle"] == ""


def test_retry_is_bounded_and_old_fence_cannot_finish(pilot):
    p = pilot
    p.ingest()
    worker = p.worker()
    first = worker._claim()
    assert first
    p.now[0] += timedelta(seconds=181)
    # The whole-event deadline has elapsed: fail explicitly, do not resurrect
    # the job or rerun perception after the caller's wait budget.
    assert not p.worker().run_once()
    worker._finish(
        first[0]["event_id"], first[1], kind="audio", text="stale", status="completed"
    )
    with store._connect() as conn:
        row = conn.execute(
            "SELECT attempt,provider_handle,status FROM media_jobs"
        ).fetchone()
        assert row["attempt"] <= 2 and row["provider_handle"] == ""
        assert "stale" not in str(
            conn.execute("SELECT text FROM media_evidence").fetchall()
        )


def test_disable_during_download_does_not_use_model_or_commit_evidence(pilot):
    p = pilot
    p.ingest()

    def download(handle, cap):
        p.cfg["whatsapp_ops"]["media_perception"]["enabled"] = False
        return b"fixture", "audio/wav"

    class Forbidden:
        def transcribe_audio(self, path):
            raise AssertionError("disabled model called")

    p.worker(downloader=download, perception=Forbidden()).run_once()
    with store._connect() as conn:
        assert not conn.execute("SELECT 1 FROM media_evidence").fetchone()


def test_admission_rollback_covers_job_insert_failure(pilot, monkeypatch):
    p = pilot
    import tools.whatsapp_ops_media_store as media_store

    def fail(*args, **kwargs):
        raise RuntimeError("fixture-failure")

    monkeypatch.setattr(media_store, "admit_media", fail)
    result = json.loads(tool.wpp_ingest_inbound_event(p.payload()))
    assert not result["ok"]
    with store._connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM inbound_events WHERE source_event_id_hash=?",
            (store.hash_text("provider-private-id"),),
        ).fetchone()


@pytest.mark.asyncio
async def test_real_http_ack_is_independent_of_slow_media_and_stop_is_immediate(
    pilot, monkeypatch
):
    import hashlib, hmac
    from aiohttp import ClientSession
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    import gateway.whatsapp_ops_media_worker as workers
    import gateway.whatsapp_ops_batch_dispatch as dispatchers

    p = pilot
    entered = threading.Event()
    release = threading.Event()

    class Slow:
        def transcribe_audio(self, path):
            entered.set()
            release.wait(3)
            return {"ok": True, "text": "Estou interessado", "truncated": False}

    worker = p.worker(perception=Slow())
    monkeypatch.setattr(workers, "WhatsAppOpsMediaWorker", lambda: worker)
    monkeypatch.setattr(dispatchers, "FriendsBatchDispatcher", lambda: p.dispatcher)
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "media": {
                        "kind": "quepasa_inbound",
                        "secret": "fixture-hmac",
                        "stt": {"enabled": True},
                    }
                },
            },
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._direct_deliver = AsyncMock()

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy inline STT was reached")

    monkeypatch.setattr("tools.whatsapp_ops_stt.run_inbound_stt_pipeline", forbidden)
    assert await adapter.connect()
    task = adapter._media_worker_task
    try:
        port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
        async with ClientSession() as client:

            async def post(payload):
                body = json.dumps(payload).encode()
                signature = hmac.new(b"fixture-hmac", body, hashlib.sha256).hexdigest()
                async with client.post(
                    f"http://127.0.0.1:{port}/webhooks/media",
                    data=body,
                    headers={"X-Webhook-Signature": signature},
                ) as response:
                    return response.status, await response.json()

            status, result = await asyncio.wait_for(
                post(p.payload(event="slow-http")), 2
            )
            assert status == 200
            assert await asyncio.to_thread(entered.wait, 2)
            assert not release.is_set()
            status, _ = await asyncio.wait_for(
                post(p.payload("text", event="stop-http", caption="pare")), 2
            )
            assert status == 200
            with store._connect() as conn:
                assert conn.execute(
                    "SELECT 1 FROM friends_suppressions WHERE contact_id=?",
                    (p.peers[0]["contact_id"],),
                ).fetchone()
        release.set()
    finally:
        release.set()
        await adapter.disconnect()
    assert task.done() and adapter._media_worker_task is None
    assert not p.calls
    adapter.handle_message.assert_not_called()
    adapter._direct_deliver.assert_not_called()


def test_legacy_media_switch_off_creates_no_media_job_and_leaves_text_running(pilot):
    p = pilot
    p.cfg["whatsapp_ops"]["media_perception"]["enabled"] = False
    p.ingest("audio")
    p.ingest("text", event="text-still", caption="Vamos conversar")
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM media_jobs").fetchone()[0] == 0
    p.now[0] += timedelta(seconds=5)
    assert p.dispatcher.run_once() == 1


@pytest.mark.parametrize(
    "fmt,kind",
    [("PNG", "image"), ("JPEG", "image"), ("WEBP", "sticker"), ("GIF", "image")],
)
def test_candidate_package_config_decodes_real_pixels_before_vision(pilot, fmt, kind):
    import yaml

    p = pilot
    # Same schema as the package, with only the allowed pilot enabled in tempDB.
    template = Path(
        "/home/won-agent/worktrees/nyx-hunter-multimodal-20260910/profiles/hunter-won-wpp-ops/config.template.yaml"
    )
    cfg = yaml.safe_load(template.read_text())
    p.cfg["whatsapp_ops"]["media_perception"] = cfg["whatsapp_ops"]["media_perception"]
    m = p.cfg["whatsapp_ops"]["media_perception"]
    m["enabled"] = True
    m["local"].update(
        ffmpeg_binary="/home/won-agent/.cache/hunter-multimodal/ffmpeg-static/bin/ffmpeg",
        ffprobe_binary="/home/won-agent/.cache/hunter-multimodal/ffmpeg-static/bin/ffprobe",
    )
    out = io.BytesIO()
    frames = [Image.new("RGB", (32, 24), color) for color in ("red", "blue")]
    if fmt in ("WEBP", "GIF"):
        frames[0].save(
            out,
            format=fmt,
            save_all=True,
            append_images=frames[1:],
            duration=100,
            lossless=True,
        )
    else:
        frames[0].save(out, format=fmt)
    requests = []

    class Vision:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kw):
            requests.append(kw)
            assert kw["messages"][0]["content"][1]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"ocr_text":"","description":"imagem sintética colorida","uncertainty":"figurinha não prova consentimento"}'
                        )
                    )
                ]
            )

    p.ingest(kind=kind, event="real-" + fmt)
    worker = p.worker(
        decoder=None,
        downloader=lambda *_: (out.getvalue(), "application/octet-stream"),
        vision_client=Vision(),
    )
    assert worker.run_once()
    p.now[0] += timedelta(seconds=5)
    assert p.dispatcher.run_once() == 1
    assert len(requests) == 1
    with store._connect() as conn:
        assert (
            conn.execute("SELECT status FROM media_jobs").fetchone()[0] == "completed"
        )
        assert (
            "imagem sintética"
            in conn.execute("SELECT text FROM media_evidence").fetchone()[0]
        )


@pytest.mark.asyncio
async def test_http_png_to_default_decoder_evidence_conversation_and_receipt(
    pilot, monkeypatch
):
    import hashlib, hmac, time
    from aiohttp import ClientSession
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    import gateway.whatsapp_ops_media_worker as workers
    import gateway.whatsapp_ops_batch_dispatch as dispatchers

    p = pilot
    p.cfg["whatsapp_ops"]["media_perception"]["local"].update(
        ffmpeg_binary="/home/won-agent/.cache/hunter-multimodal/ffmpeg-static/bin/ffmpeg",
        ffprobe_binary="/home/won-agent/.cache/hunter-multimodal/ffmpeg-static/bin/ffprobe",
    )
    out = io.BytesIO()
    Image.new("RGB", (24, 24), "blue").save(out, format="PNG")
    reached = threading.Event()
    calls = []

    class Vision:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kw):
            calls.append(kw)
            assert kw["messages"][0]["content"][1]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
            reached.set()
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"ocr_text":"","description":"quadro azul de teste","uncertainty":""}'
                        )
                    )
                ]
            )

    worker = p.worker(
        decoder=None,
        downloader=lambda *_: (out.getvalue(), "image/png"),
        vision_client=Vision(),
    )
    monkeypatch.setattr(workers, "WhatsAppOpsMediaWorker", lambda: worker)
    monkeypatch.setattr(dispatchers, "FriendsBatchDispatcher", lambda: p.dispatcher)
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {"media": {"kind": "quepasa_inbound", "secret": "test-hmac"}},
            },
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._direct_deliver = AsyncMock()
    assert await adapter.connect()
    try:
        port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
        data = json.dumps(p.payload(kind="image", event="e2e-png")).encode()
        headers = {
            "X-Webhook-Signature": hmac.new(
                b"test-hmac", data, hashlib.sha256
            ).hexdigest()
        }
        async with ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{port}/webhooks/media", data=data, headers=headers
            ) as response:
                assert response.status == 200
            assert await asyncio.to_thread(reached.wait, 15)
            for _ in range(80):
                with store._connect() as conn:
                    done = conn.execute(
                        "SELECT count(*) FROM media_jobs WHERE status='completed'"
                    ).fetchone()[0]
                if done:
                    break
                await asyncio.sleep(0.05)
            assert done == 1
            p.now[0] += timedelta(seconds=5)
            await asyncio.to_thread(p.dispatcher.run_once)
            assert len(p.calls) == 1
            assert any("quadro azul" in x["text"] for x in p.inputs[-1]["messages"])
            async with client.post(
                f"http://127.0.0.1:{port}/webhooks/media", data=data, headers=headers
            ) as response:
                assert response.status == 200
            assert len(calls) == 1
            with store._connect() as conn:
                assert (
                    conn.execute(
                        "SELECT count(*) FROM friends_inbound_queue"
                    ).fetchone()[0]
                    == 1
                )
    finally:
        await adapter.disconnect()
    adapter.handle_message.assert_not_called()
    adapter._direct_deliver.assert_not_called()


def test_voice_optout_completion_stops_before_any_reply(pilot):
    p = pilot
    p.ingest()

    class Stop:
        def transcribe_audio(self, path):
            return {
                "ok": True,
                "text": "Não me envie mais mensagens.",
                "truncated": False,
            }

    p.worker(perception=Stop()).run_once()
    p.now[0] += timedelta(seconds=5)
    assert p.dispatcher.run_once() == 0 and not p.calls
    with store._connect() as conn:
        assert (
            conn.execute("SELECT reason FROM friends_suppressions").fetchone()[0]
            == "voice_opt_out"
        )


def test_completed_job_cache_cleanup_never_follows_symlink(pilot):
    p = pilot
    p.ingest()
    p.worker().run_once()
    cache = p.home / "cache" / "whatsapp_ops_media"
    cache.mkdir(exist_ok=True, parents=True)
    outside = p.home / "unrelated"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (cache / "media-owned-symlink").symlink_to(outside, target_is_directory=True)
    p.now[0] += timedelta(days=2)
    p.worker().run_once()
    assert (outside / "keep").read_text() == "keep"
