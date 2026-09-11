"""Narrow Groq audio transcription adapter for the Hunter media worker.

This module deliberately owns no credentials, retries, fallback, media download, or send path.
"""

from __future__ import annotations

import json
import math
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from tools.whatsapp_ops_local_perception import _normalized_wav

_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
_MODEL = "whisper-large-v3-turbo"
_MAX_AUDIO_BYTES = 20 * 1024 * 1024
_MAX_DURATION_SECONDS = 300.0
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_TEXT_CHARS = 16_000
_DEFAULT_TIMEOUT_SECONDS = 30


def _result(
    *,
    ok: bool,
    text: str = "",
    language: str = "",
    duration: float = 0.0,
    truncated: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "ok": ok,
        "text": text[:_MAX_TEXT_CHARS] if ok else "",
        "language": language if ok else "",
        "duration_seconds": round(duration, 3) if ok else 0.0,
        "truncated": bool(truncated) if ok else False,
    }
    if not ok:
        value["error"] = error or "groq_transcription_failed"
    return value


class _DenyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def _default_opener() -> Callable[..., Any]:
    # Explicitly ignore proxy environment variables; this adapter has one fixed route.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _DenyRedirect()
    ).open


def _multipart(audio: bytes, *, language: str) -> tuple[bytes, str]:
    boundary = "----hunter-groq-audio-boundary"
    fields = [("model", _MODEL), ("response_format", "verbose_json")]
    if language == "pt":
        fields.append(("language", language))
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend((
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("ascii"),
            b"\r\n",
        ))
    chunks.extend((
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n',
        b"Content-Type: audio/wav\r\n\r\n",
        audio,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class GroqMediaPerception:
    """Explicit Groq-only transcription; every invalid condition is fail-closed."""

    def __init__(self, config: Any, *, opener: Callable[..., Any] | None = None):
        self._config = config if isinstance(config, dict) else None
        self._opener = opener if callable(opener) else _default_opener()

    def _settings(self) -> tuple[int, str] | None:
        config = self._config
        if not isinstance(config, dict):
            return None
        if config.get("provider") != "groq" or config.get("model") != _MODEL:
            return None
        timeout = config.get("timeout", _DEFAULT_TIMEOUT_SECONDS)
        if type(timeout) is not int or not 1 <= timeout <= 60:
            return None
        language = config.get("language", "")
        if language not in ("", "pt", "auto"):
            return None
        return timeout, language

    @staticmethod
    def _read_audio(path: Path) -> bytes | None:
        try:
            before = path.stat()
            if before.st_size > _MAX_AUDIO_BYTES:
                return None
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size != before.st_size:
                    return None
                audio = bytearray()
                while len(audio) <= _MAX_AUDIO_BYTES:
                    chunk = os.read(
                        fd, min(64 * 1024, _MAX_AUDIO_BYTES + 1 - len(audio))
                    )
                    if not chunk:
                        break
                    audio.extend(chunk)
                return bytes(audio) if len(audio) <= _MAX_AUDIO_BYTES else None
            finally:
                os.close(fd)
        except OSError:
            return None

    def transcribe_audio(self, path: str) -> dict[str, Any]:
        settings = self._settings()
        if settings is None:
            return _result(ok=False, error="groq_configuration_invalid")
        key = os.environ.get("GROQ_API_KEY", "")
        if not isinstance(key, str) or not key.strip():
            return _result(ok=False, error="groq_not_configured")
        try:
            candidate = _normalized_wav(path)
        except (OSError, ValueError, TypeError):
            candidate = None
        if candidate is None:
            return _result(ok=False, error="invalid_audio_input")
        audio_path, duration = candidate
        if (
            not math.isfinite(duration)
            or duration < 0
            or duration > _MAX_DURATION_SECONDS
        ):
            return _result(ok=False, error="audio_duration_limit")
        audio = self._read_audio(audio_path)
        if audio is None:
            return _result(ok=False, error="invalid_audio_input")
        timeout, language = settings
        body, content_type = _multipart(audio, language=language)
        request = urllib.request.Request(
            _ENDPOINT,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if not isinstance(status, int) or not 200 <= status < 300:
                    return _result(ok=False, error="groq_transcription_failed")
                data = response.read(_MAX_RESPONSE_BYTES + 1)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
        ):
            return _result(ok=False, error="groq_transcription_failed")
        if not isinstance(data, bytes) or len(data) > _MAX_RESPONSE_BYTES:
            return _result(ok=False, error="groq_invalid_response")
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _result(ok=False, error="groq_invalid_response")
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            return _result(ok=False, error="groq_invalid_response")
        response_language = payload.get("language", "")
        response_language = (
            response_language if isinstance(response_language, str) else ""
        )
        return _result(
            ok=True,
            text=payload["text"],
            language=response_language,
            duration=duration,
            truncated=len(payload["text"]) > _MAX_TEXT_CHARS,
        )
