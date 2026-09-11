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


def _message(payload: dict[str, Any]) -> dict[str, Any]:
    for candidate in (payload, payload.get("data"), (payload.get("body") or {}).get("data")):
        if isinstance(candidate, dict) and isinstance(candidate.get("message"), dict):
            return candidate["message"]
    return {}


def classify_inbound_media(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only safe metadata; never return URLs, tokens, or binary fields."""
    message = _message(payload if isinstance(payload, dict) else {})
    cases = (("audioMessage", "audio"), ("imageMessage", "image"), ("stickerMessage", "sticker"),
             ("videoMessage", "animation"), ("documentMessage", "document"))
    for key, kind in cases:
        value = message.get(key)
        if not isinstance(value, dict):
            continue
        mime = str(value.get("mimetype") or value.get("mime") or "").split(";", 1)[0].strip().lower()
        if key == "videoMessage" and not (value.get("gifPlayback") or mime in {"image/gif", "video/gif"}):
            return {"kind": "unsupported", "reason": "video_not_animation"}
        if key == "documentMessage":
            if mime.startswith("image/"):
                kind = "animation" if mime == "image/gif" else "image"
            elif mime.startswith("audio/"):
                kind = "audio"
            else:
                return {"kind": "unsupported", "reason": "document_unsupported"}
        if key == "stickerMessage" and mime not in {"image/webp", "image/gif"}:
            return {"kind": "unsupported", "reason": "sticker_unsupported"}
        if kind == "audio" and not (mime.startswith("audio/") or value.get("ptt") or value.get("voice")):
            return {"kind": "unsupported", "reason": "audio_mime_invalid"}
        size = value.get("fileLength") or value.get("filelength") or value.get("file_size") or 0
        try: size = max(0, int(size))
        except (TypeError, ValueError): size = 0
        caption = str(value.get("caption") or "").strip()[:4096]
        return {"kind": kind, "mime": mime[:100], "size_hint": size, "caption": caption,
                "duration_hint": value.get("seconds") or value.get("duration") or 0}
    return {"kind": "none"}


def sniff_media(data: bytes, declared_mime: str, expected_kind: str) -> str:
    """Bytes win over declaration and unsupported content fails closed."""
    if not isinstance(data, bytes) or not data:
        return ""
    if data.startswith(b"\x89PNG\r\n\x1a\n"): actual = "image/png"
    elif data[:3] == b"\xff\xd8\xff": actual = "image/jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP": actual = "image/webp"
    elif data[:6] in {b"GIF87a", b"GIF89a"}: actual = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WAVE": actual = "audio/wav"
    elif data.startswith(b"OggS"): actual = "audio/ogg"
    elif data.startswith(b"ID3") or data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}: actual = "audio/mpeg"
    elif len(data) >= 12 and data[4:8] == b"ftyp": actual = "video/mp4"
    else: return ""
    if expected_kind == "audio": return actual if actual.startswith("audio/") else ""
    if expected_kind in {"image", "sticker", "animation"}: return actual if actual.startswith("image/") or (expected_kind == "animation" and actual == "video/mp4") else ""
    return ""


def normalize_image(data: bytes, mime: str, *, max_bytes: int = _MAX_IMAGE_BYTES, max_pixels: int = _MAX_IMAGE_PIXELS) -> list[bytes]:
    """Decode bounded image bytes to EXIF-free PNG frames. Pillow is optional."""
    if not isinstance(data, bytes) or len(data) > max(1, min(int(max_bytes), _MAX_IMAGE_BYTES)):
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
            if image.width * image.height > max(1, min(int(max_pixels), _MAX_IMAGE_PIXELS)):
                raise ValueError("image_pixels_invalid")
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(image)
            out = io.BytesIO(); background.convert("RGB").save(out, format="PNG", optimize=True)
            frames.append(out.getvalue())
    return frames
