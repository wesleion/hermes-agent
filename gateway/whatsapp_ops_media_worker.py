"""Durable post-ACK WhatsApp media worker; no gateway request processing."""
from __future__ import annotations

import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from tools.whatsapp_ops_media import normalize_image, sniff_media
from tools.whatsapp_ops_media_vision import describe_images


def _now() -> datetime: return datetime.now(timezone.utc)

class WhatsAppOpsMediaWorker:
    def __init__(self, *, config: dict[str, Any], downloader: Callable[[str, int], tuple[bytes, str]] | None = None,
                 perception: Any = None, vision_client: Any = None, clock: Callable[[], datetime] = _now) -> None:
        self.config, self.downloader, self.perception, self.vision_client, self.clock = config or {}, downloader, perception, vision_client, clock
        self._stop = False

    def enabled(self) -> bool:
        media = self.config.get("media_perception") if isinstance(self.config, dict) else {}
        return bool(isinstance(media, dict) and media.get("enabled") is True and isinstance(self.config.get("friends_pilot"), dict) and self.config["friends_pilot"].get("enabled") is True)

    def _limits(self) -> tuple[int, int]:
        media = self.config.get("media_perception") or {}
        return (max(1, min(int(media.get("max_audio_bytes", 20 * 1024 * 1024)), 20 * 1024 * 1024)), max(1, min(int(media.get("max_image_bytes", 10 * 1024 * 1024)), 10 * 1024 * 1024)))

    def _claim(self):
        from tools.whatsapp_ops_store import _connect, init_db
        init_db(); now = self.clock(); fence = secrets.token_urlsafe(18)
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM media_jobs WHERE status='pending' OR (status='processing' AND lease_expires_at<=?) ORDER BY created_at LIMIT 1", (now.isoformat(),)).fetchone()
            if not row: return None
            changed = conn.execute("UPDATE media_jobs SET status='processing',attempt=attempt+1,fence=?,lease_expires_at=?,updated_at=? WHERE event_id=? AND status IN ('pending','processing')", (fence, (now + timedelta(seconds=180)).isoformat(), now.isoformat(), row['event_id'])).rowcount
            return (dict(row), fence) if changed else None

    def _finish(self, event_id: str, fence: str, *, kind: str, text: str, status: str, uncertain: bool = False, truncated: bool = False) -> None:
        from tools.whatsapp_ops_store import _connect
        now = self.clock().isoformat(); text = str(text or "")[:16000]
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("UPDATE media_jobs SET status=?,fence=NULL,lease_expires_at=NULL,updated_at=? WHERE event_id=? AND fence=?", (status, now, event_id, fence)).rowcount
            if not changed: return
            conn.execute("INSERT OR REPLACE INTO media_evidence(event_id,kind,source,text,uncertain,truncated,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (event_id, kind, "media_perception", text, int(uncertain), int(truncated), status, now, now))
            # Preserve the inbound slot and only enrich its own event. No raw
            # handle, path, URL, or bytes reaches the conversation history.
            label = text if text else "mídia indisponível; peça alternativa"
            conn.execute("UPDATE friends_inbound_queue SET text=substr(text || '\\n[mídia: ' || ? || ']',1,16000) WHERE event_id=?", (label, event_id))

    def run_once(self) -> bool:
        if self._stop or not self.enabled(): return False
        claimed = self._claim()
        if not claimed: return False
        row, fence = claimed; kind = row['kind']; audio_limit, image_limit = self._limits()
        try:
            if self.downloader is None: raise RuntimeError("media_downloader_unavailable")
            limit = audio_limit if kind == 'audio' else image_limit
            data, declared = self.downloader(str(row['provider_handle']), limit)
            if not isinstance(data, bytes) or len(data) > limit: raise ValueError("media_size_invalid")
            mime = sniff_media(data, declared or row['mime'], kind)
            if not mime: raise ValueError("media_type_invalid")
            if kind == 'audio':
                if self.perception is None: raise RuntimeError("local_perception_unavailable")
                with tempfile.TemporaryDirectory(prefix='wpp-media-') as tmp:
                    path = Path(tmp) / 'audio.wav'; path.write_bytes(data); os.chmod(path, 0o600)
                    result = self.perception.transcribe_audio(str(path))
                if not isinstance(result, dict) or not result.get('ok'): raise RuntimeError(str((result or {}).get('errorstable') or 'transcription_failed'))
                text = str(result.get('text') or '')[:16000]
                self._finish(row['event_id'], fence, kind=kind, text=text, status='completed', truncated=bool(result.get('truncated')))
            else:
                frames = normalize_image(data, mime, max_bytes=image_limit)
                result = describe_images(frames, self.config, client=self.vision_client)
                if not result.get('ok'): raise RuntimeError(str(result.get('error') or 'vision_failed'))
                self._finish(row['event_id'], fence, kind=kind, text=str(result.get('text') or ''), status='completed', uncertain=True)
        except (ValueError, RuntimeError, OSError):
            self._finish(row['event_id'], fence, kind=kind, text='', status='failed', uncertain=True)
        return True

    async def serve(self) -> None:
        import asyncio
        while not self._stop:
            await asyncio.to_thread(self.run_once)
            await asyncio.to_thread(__import__('threading').Event().wait, 0.5)
    def stop(self) -> None: self._stop = True
