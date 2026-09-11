"""Profile-owned post-ACK perception worker. No independent send authority."""

from __future__ import annotations
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
import threading
from typing import Any, Callable

from hermes_constants import get_hermes_home
from tools.whatsapp_ops_batch import friends_profile_id
from tools.whatsapp_ops_media_store import (
    authorized_job,
    bounded_int,
    cancel_job,
    finish_job,
    maintain_jobs,
    media_enabled,
    media_settings,
)
from tools.whatsapp_ops_media_vision import describe_images


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_config():
    from hermes_cli.config import load_config

    return load_config() or {}


class WhatsAppOpsMediaWorker:
    def __init__(
        self,
        *,
        config: dict | None = None,
        config_loader: Callable | None = None,
        downloader: Callable | None = None,
        perception: Any = None,
        vision_client: Any = None,
        decoder: Callable | None = None,
        clock: Callable = _now,
    ):
        self._config_loader = config_loader or (
            (lambda: config) if config is not None else _load_config
        )
        self.downloader, self.perception, self.vision_client, self.decoder = (
            downloader,
            perception,
            vision_client,
            decoder,
        )
        self.clock = clock
        self._stop = threading.Event()
        self._busy = threading.Lock()

    @property
    def config(self):
        try:
            cfg = self._config_loader()
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def enabled(self):
        return not self._stop.is_set() and media_enabled(self.config)

    def _claim(self):
        from tools.whatsapp_ops_store import _connect, init_db

        init_db()
        now = self.clock()
        fence = secrets.token_urlsafe(18)
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            maintain_jobs(conn, now)
            row = conn.execute(
                "SELECT * FROM media_jobs WHERE profile_id=? AND (status='pending' OR (status='processing' AND lease_expires_at<=?)) AND attempt<2 ORDER BY created_at,event_id LIMIT 1",
                (friends_profile_id(), now.isoformat()),
            ).fetchone()
            if not row:
                return None
            changed = conn.execute(
                "UPDATE media_jobs SET status='processing',attempt=attempt+1,fence=?,lease_expires_at=?,updated_at=? WHERE event_id=? AND status IN ('pending','processing')",
                (
                    fence,
                    (now + timedelta(seconds=180)).isoformat(),
                    now.isoformat(),
                    row["event_id"],
                ),
            ).rowcount
            return (dict(row), fence) if changed else None

    def _current(self, row, fence):
        from tools.whatsapp_ops_store import _connect

        with _connect() as conn:
            current = conn.execute(
                "SELECT * FROM media_jobs WHERE event_id=? AND fence=? AND status='processing'",
                (row["event_id"], fence),
            ).fetchone()
            if not current:
                return False
            if not self.enabled() or not authorized_job(conn, current, self.clock()):
                cancel_job(conn, row["event_id"], self.clock().isoformat())
                return False
            from tools.whatsapp_ops_batch import _iso

            if (_iso(current["deadline_at"]) or self.clock()) <= self.clock():
                maintain_jobs(conn, self.clock())
                return False
        return True

    def _finish(
        self,
        event_id,
        fence,
        *,
        kind,
        text,
        status,
        uncertain=False,
        truncated=False,
        error="",
    ):
        from tools.whatsapp_ops_store import _connect

        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM media_jobs WHERE event_id=? AND fence=? AND status='processing'",
                (event_id, fence),
            ).fetchone():
                return False
            if not self.enabled():
                cancel_job(conn, event_id, self.clock().isoformat())
                return False
            return finish_job(
                conn,
                event_id,
                fence,
                now=self.clock(),
                kind=kind,
                text=text,
                status=status,
                error=error,
                uncertain=uncertain,
                truncated=truncated,
            )

    def _cache_root(self):
        home = get_hermes_home()
        root = home / "cache" / "whatsapp_ops_media"
        if home.is_symlink() or (home / "cache").is_symlink() or root.is_symlink():
            raise ValueError("media_cache_invalid")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        root_stat = root.stat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or stat.S_IMODE(root_stat.st_mode) & 0o077
        ):
            raise ValueError("media_cache_invalid")
        return root

    @staticmethod
    def _open_cache_dir(directory):
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        return os.open(directory, flags)

    @contextmanager
    def _cache_root_lock(self, root):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(root / ".media_cache.lock", flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("media_cache_invalid")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def _cache_dir(self):
        root = self._cache_root()
        with self._cache_root_lock(root):
            directory = Path(tempfile.mkdtemp(prefix="media_", dir=root))
            fd = self._open_cache_dir(directory)
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield str(directory)
        finally:
            try:
                shutil.rmtree(directory, ignore_errors=True)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _cleanup(self):
        root = self._cache_root()
        ttl = bounded_int(
            media_settings(self.config).get("cache_ttl_seconds"), 86400, 86400
        )
        cutoff = self.clock().timestamp() - ttl
        with self._cache_root_lock(root):
            for item in root.iterdir():
                if not item.name.startswith("media_") or item.is_symlink():
                    continue
                try:
                    before = item.stat(follow_symlinks=False)
                    if not stat.S_ISDIR(before.st_mode):
                        continue
                    fd = self._open_cache_dir(item)
                except OSError:
                    continue
                try:
                    current = os.fstat(fd)
                    if (current.st_dev, current.st_ino) != (
                        before.st_dev,
                        before.st_ino,
                    ):
                        continue
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    locked = os.fstat(fd)
                    if (locked.st_dev, locked.st_ino) != (before.st_dev, before.st_ino):
                        continue
                    if locked.st_mtime < cutoff:
                        shutil.rmtree(item, ignore_errors=True)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    def _decode(self, data, kind, mime, settings, tmp):
        if self.decoder is not None:
            return self.decoder(data, kind, mime, settings, tmp)
        from tools.whatsapp_ops_media_decode import decode_media

        local = settings.get("local", {})
        local = local if isinstance(local, dict) else {}
        decode_config = {
            **settings,
            **{
                k: local.get(k)
                for k in ("ffmpeg_binary", "ffprobe_binary", "max_threads")
            },
        }
        return decode_media(data, kind, mime, decode_config, tmp)

    def _perceive(self, decoded, kind, settings):
        if kind == "audio":
            perception = self.perception
            if perception is None:
                from tools.whatsapp_ops_local_perception import LocalMediaPerception

                perception = LocalMediaPerception(settings.get("local", {}))
            return perception.transcribe_audio(decoded["audio_path"])
        return describe_images(
            decoded["frames"], self.config, client=self.vision_client
        )

    def _process(self, row, fence):
        settings = media_settings(self.config)
        kind = row["kind"]
        cap = (
            bounded_int(
                settings.get("max_audio_bytes"), 20 * 1024 * 1024, 20 * 1024 * 1024
            )
            if kind == "audio"
            else bounded_int(
                settings.get("max_image_bytes"), 10 * 1024 * 1024, 10 * 1024 * 1024
            )
        )
        if not self._current(row, fence):
            return False
        try:
            if row["size_hint"] > cap:
                self._finish(
                    row["event_id"],
                    fence,
                    kind=kind,
                    text="",
                    status="failed",
                    error="media_size_invalid",
                )
                return True
            downloader = self.downloader
            if downloader is None:
                from tools.whatsapp_ops_quepasa import (
                    download_media_via_quepasa_no_redirect,
                )

                downloader = (
                    lambda handle, limit: download_media_via_quepasa_no_redirect(
                        handle, max_bytes=limit
                    )
                )
            data, mime = downloader(row["provider_handle"], cap)
            if not self._current(row, fence):
                return False
            if not isinstance(data, bytes) or not data or len(data) > cap:
                raise ValueError("media_size_invalid")
            with self._cache_dir() as tmp:
                decoded = self._decode(data, kind, mime, settings, tmp)
                if not self._current(row, fence):
                    return False
                if not isinstance(decoded, dict) or decoded.get("ok") is not True:
                    error = (
                        str(decoded.get("error", "media_decode_failed"))
                        if isinstance(decoded, dict)
                        else "media_decode_failed"
                    )
                    self._finish(
                        row["event_id"],
                        fence,
                        kind=kind,
                        text="",
                        status="failed",
                        error=self._safe_error(error),
                        uncertain=True,
                    )
                    return True
                result = self._perceive(decoded, kind, settings)
            if not self._current(row, fence):
                return False
            if not isinstance(result, dict) or result.get("ok") is not True:
                error = (
                    result.get("error", "media_perception_failed")
                    if isinstance(result, dict)
                    else "media_perception_failed"
                )
                self._finish(
                    row["event_id"],
                    fence,
                    kind=kind,
                    text="",
                    status="failed",
                    error=self._safe_error(error),
                    uncertain=True,
                )
                return True
            text = result.get("text")
            if not isinstance(text, str) or not text.strip():
                self._finish(
                    row["event_id"],
                    fence,
                    kind=kind,
                    text="",
                    status="failed",
                    error="no_content_recognized",
                    uncertain=True,
                )
                return True
            self._finish(
                row["event_id"],
                fence,
                kind=kind,
                text=text[:16000],
                status="completed",
                truncated=result.get("truncated") is True or len(text) > 16000,
                uncertain=kind != "audio" or bool(result.get("uncertainty")),
            )
            return True
        except Exception:
            # Decoder/provider exception strings may contain private handles.
            self._finish(
                row["event_id"],
                fence,
                kind=kind,
                text="",
                status="failed",
                error="media_processing_failed",
                uncertain=True,
            )
            return True

    @staticmethod
    def _safe_error(error):
        allowed = {
            "unsupported_media",
            "output_dir_invalid",
            "audio_bytes_exceeded",
            "image_bytes_exceeded",
            "audio_duration_exceeded",
            "animation_duration_exceeded",
            "image_pixels_exceeded",
            "image_frames_exceeded",
            "audio_stream_missing",
            "video_stream_missing",
            "decode_failed",
            "decoder_unavailable",
            "media_corrupt",
            "media_size_invalid",
            "media_decode_failed",
            "media_type_invalid",
            "audio_duration_limit",
            "animation_duration_limit",
            "image_pixels_invalid",
            "image_frames_invalid",
            "decode_timeout",
            "ffmpeg_unavailable",
            "ffprobe_unavailable",
            "not_configured",
            "stt_timeout",
            "stt_failed",
            "invalid_audio_input",
            "vision_not_configured",
            "vision_call_failed",
            "vision_invalid_output",
            "vision_frames_invalid",
            "vision_timeout",
        }
        return (
            error
            if isinstance(error, str) and error in allowed
            else "media_perception_failed"
        )

    def run_once(self):
        if not self.enabled() or not self._busy.acquire(blocking=False):
            return False
        try:
            self._cleanup()
            claimed = self._claim()
            return self._process(*claimed) if claimed else False
        finally:
            self._busy.release()

    async def serve(self):
        while not self._stop.is_set():
            await asyncio.to_thread(self.run_once)
            await asyncio.to_thread(self._stop.wait, 0.5)

    def stop(self):
        self._stop.set()
