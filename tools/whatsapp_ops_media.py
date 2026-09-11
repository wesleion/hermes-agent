"""Pure, bounded media classification and normalization for WhatsApp Ops.

No provider, configuration, database, or model calls live here.  Inbound URLs
are intentionally not accepted as media handles; the durable worker fetches by
provider message id only after authorization and ACK.
"""

from __future__ import annotations

import io
from typing import Any

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_PIXELS = 20_000_000
_MAX_AUDIO_BYTES = 20 * 1024 * 1024


def _containers(payload: dict[str, Any]) -> list[dict]:
    body = payload.get("body")
    candidates = [
        payload,
        payload.get("data"),
        body,
        body.get("data") if isinstance(body, dict) else None,
    ]
    return [item for item in candidates if isinstance(item, dict)]


def classify_inbound_media(payload: dict[str, Any]) -> dict[str, Any]:
    """Safe descriptor for both QuePasa attachment and Whatsmeow message shapes."""
    if not isinstance(payload, dict):
        return {"kind": "none"}
    cases = {
        "audioMessage": "audio",
        "imageMessage": "image",
        "stickerMessage": "sticker",
        "videoMessage": "animation",
        "documentMessage": "document",
    }
    for container in _containers(payload):
        message = container.get("message")
        message = message if isinstance(message, dict) else {}
        value, kind = None, "none"
        for key, candidate in cases.items():
            if isinstance(message.get(key), dict):
                value, kind = message[key], candidate
                if key == "videoMessage" and value.get("gifPlayback") is not True:
                    return {"kind": "unsupported", "reason": "video_not_animation"}
                break
        if value is None:
            attachment = container.get("attachment")
            if not isinstance(attachment, dict):
                continue
            value = attachment
            hint = str(
                container.get("type") or container.get("messageType") or ""
            ).lower()
            kinds = {
                "voice": "audio",
                "ptt": "audio",
                "audio": "audio",
                "image": "image",
                "sticker": "sticker",
                "gif": "animation",
                "animation": "animation",
                "document": "document",
            }
            kind = kinds.get(hint, "document")
            if hint == "video" and container.get("gifPlayback") is not True:
                return {"kind": "unsupported", "reason": "video_not_animation"}
        mime = (
            str(value.get("mimetype") or value.get("mime") or value.get("type") or "")
            .split(";", 1)[0]
            .strip()
            .lower()[:100]
        )
        if kind == "document":
            if mime.startswith("image/"):
                kind = "animation" if mime == "image/gif" else "image"
            elif mime.startswith("audio/"):
                kind = "audio"
            else:
                return {"kind": "unsupported", "reason": "document_unsupported"}
        if kind == "sticker" and mime not in {"image/webp", "image/gif"}:
            return {"kind": "unsupported", "reason": "sticker_unsupported"}
        if (
            kind == "audio"
            and mime
            and not mime.startswith("audio/")
            and mime != "video/mp4"
        ):
            return {"kind": "unsupported", "reason": "audio_mime_invalid"}
        size = (
            value.get("fileLength")
            or value.get("filelength")
            or value.get("file_size")
            or 0
        )
        try:
            size = max(0, min(int(size), 21 * 1024 * 1024))
        except (TypeError, ValueError):
            size = 0
        caption = value.get("caption") or container.get("text") or ""
        return {
            "kind": kind,
            "mime": mime,
            "size_hint": size,
            "caption": str(caption)[:4096],
        }
    return {"kind": "none"}


def sniff_media(data: bytes, declared_mime: str, expected_kind: str) -> str:
    """Bytes win over declaration and unsupported content fails closed."""
    if not isinstance(data, bytes) or not data:
        return ""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        actual = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        actual = "image/jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        actual = "image/webp"
    elif data[:6] in {b"GIF87a", b"GIF89a"}:
        actual = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        actual = "audio/wav"
    elif data.startswith(b"OggS"):
        actual = "audio/ogg"
    elif data.startswith(b"ID3") or data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        actual = "audio/mpeg"
    elif len(data) >= 12 and data[4:8] == b"ftyp":
        actual = "video/mp4"
    else:
        return ""
    if expected_kind == "audio":
        return actual if actual.startswith("audio/") or actual == "video/mp4" else ""
    if expected_kind in {"image", "sticker", "animation"}:
        return (
            actual
            if actual.startswith("image/")
            or (expected_kind == "animation" and actual == "video/mp4")
            else ""
        )
    return ""


def normalize_image(
    data: bytes,
    mime: str,
    *,
    max_bytes: int = _MAX_IMAGE_BYTES,
    max_pixels: int = _MAX_IMAGE_PIXELS,
) -> list[bytes]:
    """Decode bounded image bytes to EXIF-free PNG frames. Pillow is optional."""
    if not isinstance(data, bytes) or len(data) > max(
        1, min(int(max_bytes), _MAX_IMAGE_BYTES)
    ):
        raise ValueError("image_size_invalid")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("pillow_unavailable") from exc
    frames: list[bytes] = []
    with Image.open(io.BytesIO(data)) as source:
        count = min(int(getattr(source, "n_frames", 1)), 4)
        for index in range(count):
            source.seek(index)
            image = ImageOps.exif_transpose(source).convert("RGBA")
            if image.width * image.height > max(
                1, min(int(max_pixels), _MAX_IMAGE_PIXELS)
            ):
                raise ValueError("image_pixels_invalid")
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(image)
            out = io.BytesIO()
            background.convert("RGB").save(out, format="PNG", optimize=True)
            frames.append(out.getvalue())
    return frames
