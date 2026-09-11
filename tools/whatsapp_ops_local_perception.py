"""Bounded local-only audio transcription for the Hunter media worker.

The helper deliberately has no downloader, network client, persistence, or fallback.
Inputs must already be normalized mono WAV files; normalization belongs to the caller.
"""
from __future__ import annotations

import math
import multiprocessing
import os
import re
import signal
import time
import wave
from pathlib import Path
from typing import Any

_MAX_TEXT_CHARS = 16_000
_MAX_SEGMENTS = 512
_MAX_AUDIO_BYTES = 64 * 1024 * 1024
_AUDIO_TIMEOUT_SECONDS = 60.0
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{2,8})?$")


def _result(*, ok: bool, text: str = "", language: str = "", duration: float = 0.0,
            truncated: bool = False, error: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "ok": ok,
        "text": text[:_MAX_TEXT_CHARS] if ok else "",
        "language": language if ok else "",
        "duration_seconds": round(duration, 3) if ok else 0.0,
        "truncated": bool(truncated) if ok else False,
    }
    if not ok:
        value["error"] = error or "stt_failed"
    return value


def _local_directory(value: Any) -> Path | None:
    if not isinstance(value, str) or not value or value != value.strip() or "://" in value:
        return None
    try:
        path = Path(value)
        return path if path.is_dir() and not path.is_symlink() else None
    except (OSError, ValueError):
        return None


def _normalized_wav(value: Any) -> tuple[Path, float] | None:
    if not isinstance(value, str) or not value or value != value.strip() or "://" in value:
        return None
    try:
        path = Path(value)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_AUDIO_BYTES:
            return None
        with wave.open(str(path), "rb") as handle:
            if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getcomptype()) != (1, 2, 16_000, "NONE"):
                return None
            duration = handle.getnframes() / 16_000
        return path, duration if math.isfinite(duration) and duration >= 0 else 0.0
    except (EOFError, OSError, ValueError, wave.Error):
        return None


def _safe_language(value: Any) -> str:
    text = str(value or "").strip()
    return text if _LANGUAGE_RE.fullmatch(text) else ""


def _worker(model_path: str, audio_path: str, threads: int, connection: Any) -> None:
    """One finite child owns the native model, so its deadline is enforceable."""
    try:
        os.environ.update({
            "OMP_NUM_THREADS": str(threads), "OPENBLAS_NUM_THREADS": str(threads),
            "MKL_NUM_THREADS": str(threads), "CT2_VERBOSE": "0",
        })
        from faster_whisper import WhisperModel
        model = WhisperModel(model_path, device="cpu", compute_type="int8", cpu_threads=threads,
                             num_workers=1, local_files_only=True)
        segments, info = model.transcribe(audio_path, vad_filter=True)
        chunks: list[str] = []
        truncated = False
        for index, segment in enumerate(segments):
            if index >= _MAX_SEGMENTS:
                truncated = True
                break
            text = getattr(segment, "text", "")
            if not isinstance(text, str):
                continue
            remaining = _MAX_TEXT_CHARS - sum(len(chunk) for chunk in chunks)
            if remaining <= 0:
                truncated = True
                break
            if len(text) > remaining:
                chunks.append(text[:remaining])
                truncated = True
                break
            chunks.append(text)
        connection.send({"ok": True, "text": "".join(chunks).strip(),
                         "language": _safe_language(getattr(info, "language", "")),
                         "truncated": truncated})
    except Exception:
        connection.send({"ok": False})
    finally:
        connection.close()


class LocalMediaPerception:
    """Local audio perception only; image perception is intentionally not implemented here."""

    _AUDIO_TIMEOUT_SECONDS = _AUDIO_TIMEOUT_SECONDS

    def __init__(self, config: dict[str, Any]):
        self._config = config if isinstance(config, dict) else {}
        requested = self._config.get("max_threads", 1)
        try:
            self._threads = min(2, max(1, int(requested)))
        except (TypeError, ValueError):
            self._threads = 1

    def transcribe_audio(self, path: str) -> dict[str, Any]:
        model_path = _local_directory(self._config.get("stt_model_path"))
        if model_path is None:
            return _result(ok=False, error="not_configured")
        candidate = _normalized_wav(path)
        if candidate is None:
            return _result(ok=False, error="invalid_audio_input")
        audio_path, duration = candidate
        receive, send = multiprocessing.Pipe(duplex=False)
        process = multiprocessing.get_context("fork").Process(
            target=_worker, args=(str(model_path), str(audio_path), self._threads, send), daemon=True)
        process.start()
        send.close()
        deadline = time.monotonic() + self._AUDIO_TIMEOUT_SECONDS
        message: dict[str, Any] | None = None
        try:
            remaining = max(0.0, deadline - time.monotonic())
            if receive.poll(remaining):
                possible = receive.recv()
                message = possible if isinstance(possible, dict) else None
        except (EOFError, OSError):
            message = None
        finally:
            receive.close()
            if process.is_alive():
                process.terminate()
                process.join(2)
            if process.is_alive() and process.pid is not None:
                os.kill(process.pid, signal.SIGKILL)
                process.join(1)
        if message is None:
            return _result(ok=False, error="stt_timeout")
        if not message.get("ok"):
            return _result(ok=False, error="stt_failed")
        return _result(ok=True, text=str(message.get("text", "")),
                       language=_safe_language(message.get("language")), duration=duration,
                       truncated=bool(message.get("truncated")))
