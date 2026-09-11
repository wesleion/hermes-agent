from __future__ import annotations

import io
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

FFMPEG = "/home/won-agent/.cache/hunter-multimodal/ffmpeg-static/bin/ffmpeg"
FFPROBE = "/home/won-agent/.cache/hunter-multimodal/ffmpeg-static/bin/ffprobe"


def _config(**limits):
    return {
        "ffmpeg_binary": FFMPEG,
        "ffprobe_binary": FFPROBE,
        "max_threads": 2,
        **limits,
    }


def _decode(data, kind, tmp_path, **limits):
    from tools.whatsapp_ops_media_decode import decode_media

    output = tmp_path / "private"
    output.mkdir(exist_ok=True)
    return decode_media(
        data, kind, "application/octet-stream", _config(**limits), str(output)
    )


def _image(fmt, size=(12, 8), color=(255, 0, 0, 255), **save):
    image = Image.new(
        "RGB" if fmt == "JPEG" else "RGBA", size, color[:3] if fmt == "JPEG" else color
    )
    out = io.BytesIO()
    image.save(out, format=fmt, lossless=True if fmt == "WEBP" else False, **save)
    return out.getvalue()


def _ffmpeg(*args):
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        timeout=10,
    )


def _wav(seconds=1):
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\0\0" * (16_000 * seconds))
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_static_images_are_sniffed_and_normalized(fmt, tmp_path):
    data = _image(fmt, color=(12, 34, 56, 255))
    result = _decode(data, "image", tmp_path)
    assert result["ok"] and result["mime"] in {"image/png", "image/jpeg", "image/webp"}
    assert result["frame_count"] == 1 and result["sampled"] is False
    with Image.open(io.BytesIO(result["frames"][0])) as image:
        assert image.format == "PNG" and all(
            abs(actual - expected) <= 1
            for actual, expected in zip(image.getpixel((0, 0)), (12, 34, 56))
        )


def test_alpha_is_flattened_white_and_large_image_is_resized(tmp_path):
    result = _decode(_image("PNG", (2000, 1000), (0, 0, 0, 0)), "image", tmp_path)
    assert result["ok"]
    with Image.open(io.BytesIO(result["frames"][0])) as image:
        assert image.size == (1536, 768)
        assert image.getpixel((1, 1)) == (255, 255, 255)


def test_gif_samples_timeline_chronologically(tmp_path):
    frames = [
        Image.new("RGB", (16, 16), color)
        for color in ("red", "green", "blue", "yellow")
    ]
    out = io.BytesIO()
    frames[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[100] * 4,
        loop=0,
    )
    result = _decode(out.getvalue(), "animation", tmp_path)
    assert result["ok"] and result["frame_count"] == 4 and result["sampled"]
    colors = [
        Image.open(io.BytesIO(frame)).getpixel((8, 8)) for frame in result["frames"]
    ]
    assert len(set(colors)) == 4


def test_corrupt_and_wrong_magic_fail_closed(tmp_path):
    assert _decode(b"not media", "image", tmp_path) == {
        "ok": False,
        "error": "unsupported_media",
    }
    assert _decode(_image("PNG"), "audio", tmp_path) == {
        "ok": False,
        "error": "unsupported_media",
    }


def test_image_byte_pixel_and_frame_caps(tmp_path):
    assert (
        _decode(_image("PNG"), "image", tmp_path, max_image_bytes=10)["error"]
        == "image_bytes_exceeded"
    )
    assert (
        _decode(_image("PNG", (20, 20)), "image", tmp_path, max_image_pixels=100)[
            "error"
        ]
        == "image_pixels_exceeded"
    )
    frames = [Image.new("RGB", (2, 2), (index, 0, 0)) for index in range(101)]
    out = io.BytesIO()
    frames[0].save(
        out, format="GIF", save_all=True, append_images=frames[1:], duration=10
    )
    assert (
        _decode(out.getvalue(), "animation", tmp_path)["error"]
        == "image_frames_exceeded"
    )


def test_output_directory_symlink_is_rejected(tmp_path):
    from tools.whatsapp_ops_media_decode import decode_media

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    result = decode_media(_image("PNG"), "image", "image/png", _config(), str(link))
    assert result == {"ok": False, "error": "output_dir_invalid"}


def test_wav_is_normalized_to_pcm_mono_16khz(tmp_path):
    result = _decode(_wav(), "audio", tmp_path)
    assert (
        result["ok"]
        and result["kind"] == "audio"
        and Path(result["audio_path"]).is_absolute()
    )
    with wave.open(result["audio_path"], "rb") as decoded:
        assert (
            decoded.getnchannels(),
            decoded.getframerate(),
            decoded.getsampwidth(),
        ) == (1, 16_000, 2)


@pytest.mark.parametrize("extension", ["ogg", "m4a"])
def test_real_ogg_and_m4a_decode(extension, tmp_path):
    source = tmp_path / f"voice.{extension}"
    codec = "libopus" if extension == "ogg" else "aac"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=0.25",
        "-c:a",
        codec,
        str(source),
    )
    result = _decode(source.read_bytes(), "audio", tmp_path)
    assert result["ok"] and result["duration_seconds"] > 0


def test_audio_duration_and_video_only_are_rejected(tmp_path):
    assert _decode(_wav(301), "audio", tmp_path)["error"] == "audio_duration_exceeded"
    video = tmp_path / "video.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=16x16:d=0.5",
        "-an",
        "-c:v",
        "mpeg4",
        str(video),
    )
    assert (
        _decode(video.read_bytes(), "audio", tmp_path)["error"]
        == "audio_stream_missing"
    )


def test_short_mp4_samples_distinct_frames(tmp_path):
    video = tmp_path / "colors.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=32x32:rate=20:duration=1",
        "-an",
        "-c:v",
        "mpeg4",
        str(video),
    )
    result = _decode(video.read_bytes(), "animation", tmp_path)
    assert result["ok"] and result["frame_count"] == 4 and result["sampled"]
    pixels = [
        Image.open(io.BytesIO(frame)).getpixel((16, 16)) for frame in result["frames"]
    ]
    assert len(set(pixels)) > 1


def _animated_webp(duration=200):
    frames = [Image.new("RGB", (16, 16), color) for color in ("red", "blue")]
    out = io.BytesIO()
    frames[0].save(
        out,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        lossless=True,
    )
    return out.getvalue()


def test_animated_sticker_duration_is_checked_even_without_animation_flag(tmp_path):
    assert (
        _decode(_animated_webp(9000), "sticker", tmp_path)["error"]
        == "animation_duration_exceeded"
    )


def test_single_requested_animation_frame_does_not_divide_by_zero(tmp_path):
    result = _decode(_animated_webp(), "sticker", tmp_path, max_vision_frames=1)
    assert result["ok"] and result["frame_count"] == 1


def test_decoder_timeout_terminates_the_whole_process_group(monkeypatch):
    import signal
    from tools import whatsapp_ops_media_decode as module

    calls = []

    class Child:
        pid = 987654321

        def communicate(self, *args, **kwargs):
            if kwargs.get("timeout"):
                raise subprocess.TimeoutExpired("bounded-child", kwargs["timeout"])
            calls.append("reaped")
            return "", None

    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: Child())
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    assert module._run_child({}) == {"ok": False, "error": "decode_timeout"}
    assert calls == [(987654321, signal.SIGKILL), "reaped"]


def test_animation_duration_limit(tmp_path):
    video = tmp_path / "long.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=red:s=16x16:d=16",
        "-an",
        "-c:v",
        "mpeg4",
        str(video),
    )
    assert (
        _decode(video.read_bytes(), "animation", tmp_path)["error"]
        == "animation_duration_exceeded"
    )
