"""Bounded local decoder for WhatsApp Ops media.
The public function starts a resource-limited child process; it never contacts a
provider or accepts a path/URL from inbound media.  Only decoded WAV/PNG bytes
are returned to the caller-owned private directory.
"""

from __future__ import annotations

# fmt: off
import io
import json
import math
import os
import stat
import subprocess
import sys
import uuid
import wave
from pathlib import Path
from typing import Any
_MAX_AUDIO_BYTES = 20 * 1024 * 1024
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_PIXELS = 20_000_000
_MAX_TOTAL_PIXELS = 100_000_000
_MAX_FRAMES = 100
_MAX_AUDIO_SECONDS = 300.0
_MAX_ANIMATION_SECONDS = 15.0
_MAX_VISION_FRAMES = 4
_ERRORS = {
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
    "decode_timeout",
    "decoder_unavailable",
}
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"OggS", "audio/ogg", ".ogg"),
    (b"ID3", "audio/mpeg", ".mp3"),
)
def decode_media(data: bytes, kind: str, declared_mime: str, config: dict, output_dir: str) -> dict:
    """Decode known media bytes in a finite isolated child process."""
    del declared_mime
    if not isinstance(data, bytes) or kind not in {"audio", "image", "sticker", "animation"}: return _failure("unsupported_media")
    directory = _safe_directory(output_dir)
    detected = _sniff(data)
    if directory is None: return _failure("output_dir_invalid")
    if detected is None: return _failure("unsupported_media")
    mime, suffix = detected
    accepted = (kind == "audio" and (mime.startswith("audio/") or mime == "video/mp4")) or (kind != "audio" and mime.startswith("image/")) or (kind == "animation" and mime == "video/mp4")
    if not accepted: return _failure("unsupported_media")
    limits = _limits(config)
    if len(data) > limits["max_audio_bytes" if kind == "audio" else "max_image_bytes"]: return _failure("audio_bytes_exceeded" if kind == "audio" else "image_bytes_exceeded")
    binaries = _binaries(config)
    if binaries is None: return _failure("decoder_unavailable")
    token = uuid.uuid4().hex; input_path = directory / f".hunter-decode-{token}{suffix}"
    try:
        with os.fdopen(os.open(input_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600), "wb") as handle: handle.write(data)
        result = _run_child({"input": str(input_path), "output": str(directory), "token": token, "kind": kind, "mime": mime, "ffmpeg": binaries[0], "ffprobe": binaries[1], "limits": limits})
        return _validate_result(result, directory, token, kind) if result.get("ok") else _failure(result.get("error", "decode_failed"))
    except OSError: return _failure("decode_failed")
    finally:
        try: input_path.unlink(missing_ok=True)
        except OSError: pass
def _failure(code: str) -> dict:
    return {"ok": False, "error": code if code in _ERRORS else "decode_failed"}
def _sniff(data: bytes) -> tuple[str, str] | None:
    for marker, mime, suffix in _MAGIC:
        if data.startswith(marker):
            return mime, suffix
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", ".wav"
    if data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "audio/mpeg", ".mp3"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4", ".mp4"
    return None

def _limits(config: dict) -> dict:
    source = config if isinstance(config, dict) else {}
    defaults = {"max_audio_bytes": _MAX_AUDIO_BYTES, "max_image_bytes": _MAX_IMAGE_BYTES, "max_image_pixels": _MAX_IMAGE_PIXELS, "max_audio_seconds": 300, "max_animation_seconds": 15, "max_vision_frames": 4, "max_threads": 2}
    def cap(key, value):
        try: return max(1, min(int(source.get(key, value)), value))
        except (TypeError, ValueError): return value
    return {key: cap(key, value) for key, value in defaults.items()}
def _binaries(config: dict) -> tuple[str, str] | None:
    values = (config.get("ffmpeg_binary"), config.get("ffprobe_binary")) if isinstance(config, dict) else ()
    return values if len(values) == 2 and all(isinstance(value, str) and os.path.isabs(value) and os.path.isfile(value) and os.access(value, os.X_OK) for value in values) else None
def _safe_directory(value: str) -> Path | None:
    if not isinstance(value, str) or not os.path.isabs(value): return None
    try:
        path = Path(value)
        if not path.is_dir() or any(part.is_symlink() for part in (path, *path.parents)): return None
        return path.resolve(strict=True)
    except OSError: return None

def _run_child(spec: dict) -> dict:
    root = str(Path(__file__).resolve().parent.parent)
    try:
        done = subprocess.run([sys.executable, "-m", "tools.whatsapp_ops_media_decode", "--child"], input=json.dumps(spec), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, start_new_session=True, cwd=root, env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "PYTHONPATH": root}, check=False)
        return json.loads(done.stdout) if done.returncode == 0 and len(done.stdout) <= 8192 else _failure("decode_failed")
    except subprocess.TimeoutExpired: return _failure("decode_timeout")
    except (OSError, ValueError, TypeError): return _failure("decode_failed")
def _validate_result(result: dict, directory: Path, token: str, kind: str) -> dict:
    def owned(value, suffix):
        try:
            path = Path(value)
            info = path.lstat()
            return path if path.parent == directory and path.name.startswith(f".hunter-decode-{token}-") and path.suffix == suffix and stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) else None
        except (TypeError, OSError): return None
    if kind == "audio":
        path = owned(result.get("audio_path"), ".wav")
        try:
            with wave.open(str(path), "rb") as decoded: valid = decoded.getnchannels() == 1 and decoded.getframerate() == 16_000 and decoded.getsampwidth() == 2
        except (OSError, wave.Error): valid = False
        return {"ok": True, "kind": "audio", "audio_path": str(path), "duration_seconds": float(result["duration_seconds"]), "mime": str(result["mime"])} if path and valid else _failure("decode_failed")
    paths = [owned(item, ".png") for item in result.get("frames", [])] if isinstance(result.get("frames"), list) else []
    if not 1 <= len(paths) <= _MAX_VISION_FRAMES or any(path is None or path.stat().st_size > 20 * 1024 * 1024 for path in paths): return _failure("decode_failed")
    return {"ok": True, "kind": kind, "frames": [path.read_bytes() for path in paths], "duration_seconds": float(result["duration_seconds"]), "frame_count": len(paths), "sampled": bool(result["sampled"]), "mime": str(result["mime"])}

def _child_main() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        spec = json.loads(sys.stdin.read(8192))
        result = _child_decode(spec)
    except Exception:
        result = _failure("decode_failed")
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
def _child_decode(spec: Any) -> dict:
    if not isinstance(spec, dict): return _failure("decode_failed")
    input_path, output, token = (Path(str(spec.get(key, ""))) for key in ("input", "output", "token"))
    if not input_path.is_file() or output != input_path.parent or len(token.name) != 32: return _failure("decode_failed")
    kind, mime, limits = spec.get("kind"), spec.get("mime"), spec.get("limits")
    if kind == "audio": return _decode_audio(input_path, output, token.name, mime, limits, spec)
    if kind in {"image", "sticker", "animation"}: return _decode_mp4(input_path, output, token.name, kind, mime, limits, spec) if mime == "video/mp4" else _decode_pillow(input_path, output, token.name, kind, mime, limits)
    return _failure("decode_failed")
def _probe(path: Path, spec: dict) -> dict | None:
    command = [
        spec["ffprobe"],
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        done = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
def _probe_duration(probe: dict) -> float | None:
    values = [(probe.get("format") or {}).get("duration")] + [
        stream.get("duration")
        for stream in probe.get("streams", [])
        if isinstance(stream, dict)
    ]
    for value in values:
        try:
            duration = float(value)
            if math.isfinite(duration) and duration >= 0:
                return duration
        except (TypeError, ValueError):
            pass
    return None
def _decode_audio(
    input_path: Path, output: Path, token: str, mime: str, limits: dict, spec: dict
) -> dict:
    probe = _probe(input_path, spec)
    if probe is None:
        return _failure("decode_failed")
    audio = next(
        (
            stream
            for stream in probe.get("streams", [])
            if stream.get("codec_type") == "audio"
        ),
        None,
    )
    if audio is None:
        return _failure("audio_stream_missing")
    duration = _probe_duration(probe)
    if duration is None:
        return _failure("decode_failed")
    if duration > limits["max_audio_seconds"]:
        return _failure("audio_duration_exceeded")
    target = output / f".hunter-decode-{token}-audio.wav"
    command = [
        spec["ffmpeg"],
        "-nostdin",
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(input_path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        "-threads",
        str(limits["max_threads"]),
        "-f",
        "wav",
        str(target),
    ]
    if not _command(command):
        return _failure("decode_failed")
    try:
        with wave.open(str(target), "rb") as decoded:
            actual = decoded.getnframes() / decoded.getframerate()
    except (OSError, wave.Error, ZeroDivisionError):
        return _failure("decode_failed")
    if actual > limits["max_audio_seconds"]:
        target.unlink(missing_ok=True)
        return _failure("audio_duration_exceeded")
    return {
        "ok": True,
        "audio_path": str(target),
        "duration_seconds": actual,
        "mime": mime,
    }

def _decode_pillow(
    input_path: Path, output: Path, token: str, kind: str, mime: str, limits: dict
) -> dict:
    try:
        from PIL import Image, ImageOps
        with Image.open(input_path) as source:
            count, width, height = (
                int(getattr(source, "n_frames", 1)),
                source.width,
                source.height,
            )
            if count > _MAX_FRAMES or count * width * height > _MAX_TOTAL_PIXELS:
                return _failure("image_frames_exceeded")
            if width * height > limits["max_image_pixels"]:
                return _failure("image_pixels_exceeded")
            durations = [
                max(0, int(source.seek(index) or source.info.get("duration", 0)))
                for index in range(count)
            ]
            duration = sum(durations) / 1000.0
            if kind == "animation" and duration > limits["max_animation_seconds"]:
                return _failure("animation_duration_exceeded")
            indexes = _sample_indexes(count, durations, limits["max_vision_frames"])
            frames = []
            for order, index in enumerate(indexes):
                source.seek(index)
                image = ImageOps.exif_transpose(source).convert("RGBA")
                if image.width * image.height > limits["max_image_pixels"]:
                    return _failure("image_pixels_exceeded")
                image.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", image.size, "white")
                canvas.alpha_composite(image)
                target = output / f".hunter-decode-{token}-{order}.png"
                canvas.convert("RGB").save(target, "PNG", optimize=True)
                frames.append(str(target))
    except Exception:
        return _failure("decode_failed")
    return {
        "ok": True,
        "frames": frames,
        "duration_seconds": duration,
        "sampled": count > 1,
        "mime": mime,
    }
def _sample_indexes(count: int, durations: list[int], maximum: int) -> list[int]:
    selected = min(count, maximum)
    if selected == count:
        return list(range(count))
    total = sum(durations)
    if total <= 0:
        return [
            round(index * (count - 1) / (selected - 1)) for index in range(selected)
        ]
    points, cursor, accumulated = [], 0, 0
    for point in [index * total / (selected - 1) for index in range(selected)]:
        while cursor < count - 1 and accumulated + durations[cursor] <= point:
            accumulated += durations[cursor]
            cursor += 1
        points.append(cursor)
    return points
def _decode_mp4(
    input_path: Path,
    output: Path,
    token: str,
    kind: str,
    mime: str,
    limits: dict,
    spec: dict,
) -> dict:
    probe = _probe(input_path, spec)
    if probe is None:
        return _failure("decode_failed")
    stream = next(
        (
            item
            for item in probe.get("streams", [])
            if item.get("codec_type") == "video"
        ),
        None,
    )
    if stream is None:
        return _failure("video_stream_missing")
    try:
        pixels = int(stream["width"]) * int(stream["height"])
    except (KeyError, TypeError, ValueError):
        return _failure("decode_failed")
    duration = _probe_duration(probe)
    if duration is None:
        return _failure("decode_failed")
    if duration > limits["max_animation_seconds"]:
        return _failure("animation_duration_exceeded")
    if pixels > limits["max_image_pixels"]:
        return _failure("image_pixels_exceeded")
    try:
        source_frames = int(stream.get("nb_frames") or 0)
        rate_num, rate_den = str(stream.get("r_frame_rate", "0/1")).split("/", 1)
        estimated_frames = duration * int(rate_num) / max(1, int(rate_den))
    except (TypeError, ValueError):
        source_frames, estimated_frames = 0, 0
    if source_frames > _MAX_FRAMES or estimated_frames > _MAX_FRAMES:
        return _failure("image_frames_exceeded")
    count = min(limits["max_vision_frames"], max(1, int(round(duration * 20))))
    final_time = max(0.0, duration - max(0.05, duration / 100))
    timestamps = (
        [0.0]
        if count == 1
        else [min(index * duration / (count - 1), final_time) for index in range(count)]
    )
    frames = []
    for index, timestamp in enumerate(timestamps):
        target = output / f".hunter-decode-{token}-{index}.png"
        command = [
            spec["ffmpeg"],
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-ss",
            f"{timestamp:.6f}",
            "-i",
            str(input_path),
            "-frames:v",
            "1",
            "-an",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-vf",
            "scale=1536:1536:force_original_aspect_ratio=decrease",
            str(target),
        ]
        if not _command(command):
            return _failure("decode_failed")
        frames.append(str(target))
    return {
        "ok": True,
        "frames": frames,
        "duration_seconds": duration,
        "sampled": count > 1,
        "mime": mime,
    }
def _command(command: list[str]) -> bool:
    try:
        return (
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
if __name__ == "__main__" and sys.argv[1:] == ["--child"]:
    _child_main()
# fmt: on
