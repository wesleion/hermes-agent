"""Local-only WhatsApp Ops STT pipeline.

Acquisition, FFmpeg normalization, transcription, and persistence are separate
steps. Public results never contain media bytes, provider message ids, paths, or
transcript text. The only transcript read surface is the exact-event store API.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hermes_constants import get_hermes_home

_MAX_TRANSCRIPT_CHARS = 4_000
_MAX_SEGMENTS = 128
_MAX_MEDIA_BYTES = 25 * 1024 * 1024
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{2,8})?$")

ModelFactory = Callable[..., Any]
Downloader = Callable[[str, int], tuple[bytes, str]]


@dataclass(frozen=True)
class LocalSttRequest:
    media_path: str
    model_path: str
    language: str = ""


@dataclass(frozen=True)
class LocalSttResult:
    ok: bool
    status: str
    transcript_text: str = field(default="", repr=False)
    transcript_sha256: str = ""
    transcript_truncated: bool = False
    detected_language: str = ""
    duration_seconds: float = 0.0
    error_class: str = ""


def _default_model_factory(model_path: str, **kwargs: Any) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(model_path, **kwargs)


def _safe_local_path(value: Any, *, directory: bool) -> Path | None:
    raw = str(value or "")
    if not raw or raw != raw.strip() or "://" in raw or raw.startswith(("//", "\\\\")):
        return None
    try:
        path = Path(raw)
        if path.is_symlink():
            return None
        if directory and not path.is_dir():
            return None
        if not directory and not path.is_file():
            return None
        return path
    except (OSError, ValueError):
        return None


def _safe_language(value: Any) -> str:
    text = str(value or "").strip()
    return text if _LANGUAGE_RE.fullmatch(text) else ""


def _safe_duration(value: Any) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return 0.0
    return duration if math.isfinite(duration) and duration >= 0 else 0.0


def _bounded_transcript(segments: Any) -> tuple[str, bool]:
    transcript = ""
    saw_text = False
    truncated = False
    for index, segment in enumerate(segments):
        if index >= _MAX_SEGMENTS:
            truncated = True
            break
        text = getattr(segment, "text", None)
        if not isinstance(text, str):
            continue
        chunk = (" " if saw_text else "") + text
        saw_text = True
        remaining = _MAX_TRANSCRIPT_CHARS - len(transcript)
        if len(chunk) > remaining:
            transcript += chunk[:remaining]
            truncated = True
            break
        transcript += chunk
    return transcript, truncated


def transcribe_local_audio(
    request: LocalSttRequest,
    *,
    enabled: bool = False,
    model_factory: ModelFactory | None = None,
) -> LocalSttResult:
    """Run faster-whisper strictly from an already materialized local model."""
    if not enabled:
        return LocalSttResult(ok=False, status="disabled")
    model_path = _safe_local_path(request.model_path, directory=True)
    if model_path is None:
        return LocalSttResult(ok=False, status="model_not_cached")
    media_path = _safe_local_path(request.media_path, directory=False)
    if media_path is None:
        return LocalSttResult(ok=False, status="media_not_available")

    factory = model_factory or _default_model_factory
    try:
        model = factory(
            str(model_path),
            device="cpu",
            compute_type="int8",
            local_files_only=True,
        )
        options: dict[str, Any] = {"vad_filter": True}
        language = _safe_language(request.language)
        if language:
            options["language"] = language
        segments, info = model.transcribe(str(media_path), **options)
        transcript, truncated = _bounded_transcript(segments)
        return LocalSttResult(
            ok=True,
            status="completed",
            transcript_text=transcript,
            transcript_sha256=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
            transcript_truncated=truncated,
            detected_language=_safe_language(getattr(info, "language", "")),
            duration_seconds=_safe_duration(getattr(info, "duration", 0.0)),
        )
    except Exception as exc:
        return LocalSttResult(
            ok=False,
            status="error",
            error_class=type(exc).__name__[:64],
        )


def normalize_audio_ffmpeg(source: Path | str, destination: Path | str) -> dict[str, Any]:
    """Normalize a local file to PCM WAV, 16 kHz, mono, without a shell."""
    source_path = _safe_local_path(source, directory=False)
    destination_path = Path(destination)
    ffmpeg = shutil.which("ffmpeg")
    if source_path is None:
        return {"ok": False, "status": "media_not_available"}
    if not ffmpeg:
        return {"ok": False, "status": "ffmpeg_unavailable"}
    try:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(destination_path),
            ],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "ffmpeg_timeout"}
    except (OSError, subprocess.CalledProcessError) as exc:
        return {
            "ok": False,
            "status": "ffmpeg_error",
            "error_class": type(exc).__name__[:64],
        }
    return {"ok": True, "status": "normalized"}


def _media_suffix(mime_type: str, data: bytes) -> str:
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"ID3") or data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return ".mp3"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4"
    by_mime = {
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/opus": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".mp4",
        "video/mp4": ".mp4",
    }
    return by_mime.get(mime, "")


def _default_downloader(provider_message_id: str, max_bytes: int) -> tuple[bytes, str]:
    from tools.whatsapp_ops_quepasa import download_media_via_quepasa

    return download_media_via_quepasa(provider_message_id, max_bytes=max_bytes)


def provider_message_id_from_payload(payload: dict[str, Any]) -> str:
    """Extract the provider id for immediate in-memory use only."""
    if not isinstance(payload, dict):
        return ""
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    body_data = body.get("data") if isinstance(body.get("data"), dict) else {}
    top_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data = body_data or top_data
    key = data.get("key") if isinstance(data.get("key"), dict) else {}
    return str(
        payload.get("id")
        or payload.get("message_id")
        or payload.get("msgid")
        or key.get("id")
        or data.get("id")
        or ""
    ).strip()


def _public_pipeline_result(
    *,
    ok: bool,
    status: str,
    download_performed: bool = False,
    transcription_performed: bool = False,
    transcript_persisted: bool = False,
    transcript_sha256: str = "",
    transcript_truncated: bool = False,
    error_class: str = "",
) -> dict[str, Any]:
    result = {
        "ok": bool(ok),
        "status": str(status)[:64],
        "download_performed": bool(download_performed),
        "transcription_performed": bool(transcription_performed),
        "stt_provider_called": bool(transcription_performed),
        "transcript_persisted": bool(transcript_persisted),
        "send_performed": False,
        "crm_write_performed": False,
        "provider_history_used": False,
        "llm_used": False,
        "raw_media_exposed": False,
        "transcript_available": bool(transcript_persisted),
        "transcript_sha256": str(transcript_sha256)[:64],
        "transcript_truncated": bool(transcript_truncated),
    }
    if error_class:
        result["error_class"] = str(error_class)[:64]
    return result


def run_inbound_stt_pipeline(
    *,
    event_id: str,
    provider_message_id: str,
    enabled: bool,
    model_path: str,
    model_name: str = "small",
    language: str = "pt",
    downloader: Downloader | None = None,
    model_factory: ModelFactory | None = None,
    temp_root: Path | str | None = None,
    max_bytes: int = _MAX_MEDIA_BYTES,
) -> dict[str, Any]:
    """Acquire, normalize, transcribe, persist, and clean one inbound voice note."""
    if not enabled:
        return _public_pipeline_result(ok=False, status="disabled")
    if not re.fullmatch(r"inbound_[A-Za-z0-9_-]{1,80}", str(event_id or "")):
        return _public_pipeline_result(ok=False, status="invalid_event_id")
    if not str(provider_message_id or "").strip():
        return _public_pipeline_result(ok=False, status="provider_message_id_missing")
    if _safe_local_path(model_path, directory=True) is None:
        return _public_pipeline_result(ok=False, status="model_not_cached")

    from tools.whatsapp_ops_store import get_inbound_transcription_eligibility

    eligibility = get_inbound_transcription_eligibility(event_id)
    if not eligibility.get("eligible"):
        return _public_pipeline_result(
            ok=False,
            status=str(eligibility.get("status") or "no_transcribable_media"),
        )

    bounded_max = max(1, min(int(max_bytes or _MAX_MEDIA_BYTES), _MAX_MEDIA_BYTES))
    root = Path(temp_root) if temp_root is not None else get_hermes_home() / "cache" / "whatsapp_ops_stt"
    root.mkdir(parents=True, exist_ok=True)
    acquire = downloader or _default_downloader
    download_performed = False
    with tempfile.TemporaryDirectory(prefix="inbound-", dir=root) as work_dir:
        work = Path(work_dir)
        try:
            data, mime_type = acquire(str(provider_message_id), bounded_max)
            download_performed = True
        except Exception as exc:
            return _public_pipeline_result(
                ok=False,
                status="download_error",
                error_class=type(exc).__name__,
            )
        if not isinstance(data, bytes) or not data or len(data) > bounded_max:
            return _public_pipeline_result(
                ok=False,
                status="media_size_invalid",
                download_performed=download_performed,
            )
        suffix = _media_suffix(mime_type, data)
        if not suffix:
            return _public_pipeline_result(
                ok=False,
                status="media_type_invalid",
                download_performed=download_performed,
            )
        source = work / ("source" + suffix)
        normalized = work / "normalized.wav"
        source.write_bytes(data)
        normalized_result = normalize_audio_ffmpeg(source, normalized)
        if not normalized_result.get("ok"):
            return _public_pipeline_result(
                ok=False,
                status=str(normalized_result.get("status") or "ffmpeg_error"),
                download_performed=download_performed,
                error_class=str(normalized_result.get("error_class") or ""),
            )
        stt_result = transcribe_local_audio(
            LocalSttRequest(
                media_path=str(normalized),
                model_path=str(model_path),
                language=language,
            ),
            enabled=True,
            model_factory=model_factory,
        )
        if not stt_result.ok:
            return _public_pipeline_result(
                ok=False,
                status=stt_result.status,
                download_performed=download_performed,
                transcription_performed=stt_result.status == "error",
                error_class=stt_result.error_class,
            )
        try:
            from tools.whatsapp_ops_store import persist_media_transcription

            persisted = persist_media_transcription(
                event_id=event_id,
                transcript_text=stt_result.transcript_text,
                transcript_sha256=stt_result.transcript_sha256,
                transcript_truncated=stt_result.transcript_truncated,
                detected_language=stt_result.detected_language,
                duration_seconds=stt_result.duration_seconds,
                provider="local_faster_whisper",
                model=str(model_name or "small"),
            )
        except Exception as exc:
            return _public_pipeline_result(
                ok=False,
                status="persistence_error",
                download_performed=download_performed,
                transcription_performed=True,
                error_class=type(exc).__name__,
            )
        return _public_pipeline_result(
            ok=bool(persisted.get("ok")),
            status="completed" if persisted.get("ok") else "persistence_error",
            download_performed=download_performed,
            transcription_performed=True,
            transcript_persisted=bool(persisted.get("ok")),
            transcript_sha256=stt_result.transcript_sha256,
            transcript_truncated=stt_result.transcript_truncated,
        )
