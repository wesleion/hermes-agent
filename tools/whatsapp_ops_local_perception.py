"""Bounded local-only audio transcription for the Hunter media worker.

The helper deliberately has no downloader, network client, persistence, or fallback.
Inputs must already be normalized mono WAV files; normalization belongs to the caller.
"""
from __future__ import annotations

import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

_MAX_TEXT_CHARS = 16_000
_MAX_SEGMENTS = 512
_MAX_AUDIO_BYTES = 64 * 1024 * 1024
_AUDIO_TIMEOUT_SECONDS = 60.0
_CHILD_OUTPUT_BYTES = 32 * 1024
_WORKER_ADDRESS_SPACE_BYTES = 1536 * 1024 * 1024
_WORKER_CPU_SECONDS = 60
_WORKER_NOFILE = 64
_WORKER_FILE_BYTES = 32 * 1024 * 1024
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


def _safe_path(value: Any, *, directory: bool) -> Path | None:
    if not isinstance(value, str) or not value or value != value.strip() or "://" in value:
        return None
    try:
        path = Path(value)
        if not path.is_absolute() or path.is_symlink():
            return None
        resolved = path.resolve(strict=True)
        if resolved != path or (not resolved.is_dir() if directory else not resolved.is_file()):
            return None
        return path
    except (OSError, ValueError):
        return None


def _local_directory(value: Any) -> Path | None:
    return _safe_path(value, directory=True)


def _normalized_wav(value: Any) -> tuple[Path, float] | None:
    path = _safe_path(value, directory=False)
    if path is None:
        return None
    try:
        if path.stat().st_size > _MAX_AUDIO_BYTES:
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


def _set_limit(resource_type: int, value: int) -> None:
    import resource

    _, hard = resource.getrlimit(resource_type)
    ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
    resource.setrlimit(resource_type, (ceiling, hard))


def _apply_worker_limits() -> None:
    """Apply limits before importing native inference code in the isolated child."""
    import resource

    _set_limit(resource.RLIMIT_AS, _WORKER_ADDRESS_SPACE_BYTES)
    _set_limit(resource.RLIMIT_CPU, _WORKER_CPU_SECONDS)
    _set_limit(resource.RLIMIT_NOFILE, _WORKER_NOFILE)
    _set_limit(resource.RLIMIT_FSIZE, _WORKER_FILE_BYTES)


def _worker(model_path: str, audio_path: str, threads: int) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_path, device="cpu", compute_type="int8", cpu_threads=threads,
                             num_workers=1, local_files_only=True)
        segments, info = model.transcribe(audio_path, vad_filter=True)
        chunks: list[str] = []
        truncated = False
        used = 0
        for index, segment in enumerate(segments):
            if index >= _MAX_SEGMENTS:
                truncated = True
                break
            text = getattr(segment, "text", "")
            if not isinstance(text, str):
                continue
            remaining = _MAX_TEXT_CHARS - used
            if remaining <= 0:
                truncated = True
                break
            if len(text) > remaining:
                chunks.append(text[:remaining])
                truncated = True
                break
            chunks.append(text)
            used += len(text)
        return {"ok": True, "text": "".join(chunks).strip(),
                "language": _safe_language(getattr(info, "language", "")), "truncated": truncated}
    except Exception:
        return {"ok": False}


def _child_main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[1] != "--_hunter_stt_child":
        return 2
    model_path = _local_directory(argv[2])
    audio = _normalized_wav(argv[3])
    try:
        threads = int(argv[4])
    except ValueError:
        threads = 0
    message: dict[str, Any] = {"ok": False}
    if model_path is not None and audio is not None and threads in (1, 2):
        try:
            _apply_worker_limits()
            message = _worker(str(model_path), str(audio[0]), threads)
        except (OSError, ValueError):
            message = {"ok": False}
    payload = json.dumps(message, separators=(",", ":"))[:_CHILD_OUTPUT_BYTES]
    try:
        os.write(sys.stdout.fileno(), payload.encode("utf-8") + b"\n")
    except OSError:
        return 1
    return 0


def _child_environment(threads: int, home: str) -> dict[str, str]:
    source = str(Path(__file__).resolve().parent)
    return {
        "HOME": home,
        "HERMES_HOME": str(Path(home) / ".hermes"),
        "PYTHONPATH": source,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "OMP_NUM_THREADS": str(threads),
        "OPENBLAS_NUM_THREADS": str(threads),
        "MKL_NUM_THREADS": str(threads),
        "CT2_VERBOSE": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    pid = process.pid
    if pid is None or pid == os.getpid():
        return
    for signum, timeout in ((signal.SIGTERM, 2), (signal.SIGKILL, 2)):
        try:
            os.killpg(pid, signum)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        if signum == signal.SIGKILL:
            return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


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
        message: dict[str, Any] | None = None
        with tempfile.TemporaryDirectory(prefix="hunter-stt-") as home:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--_hunter_stt_child",
                 str(model_path), str(audio_path), str(self._threads)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, env=_child_environment(self._threads, home), start_new_session=True,
            )
            try:
                assert process.stdout is not None
                ready, _, _ = select.select([process.stdout], [], [], self._AUDIO_TIMEOUT_SECONDS)
                if ready:
                    payload = process.stdout.readline(_CHILD_OUTPUT_BYTES + 1)
                    if len(payload) <= _CHILD_OUTPUT_BYTES:
                        possible = json.loads(payload)
                        message = possible if isinstance(possible, dict) else None
            except (OSError, ValueError, json.JSONDecodeError):
                message = None
            finally:
                _kill_process_group(process)
                if process.stdout is not None:
                    process.stdout.close()
        if message is None:
            return _result(ok=False, error="stt_timeout")
        if not message.get("ok"):
            return _result(ok=False, error="stt_failed")
        return _result(ok=True, text=str(message.get("text", "")),
                       language=_safe_language(message.get("language")), duration=duration,
                       truncated=bool(message.get("truncated")))


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv))
