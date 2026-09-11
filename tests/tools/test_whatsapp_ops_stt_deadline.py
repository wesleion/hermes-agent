"""Exercise the real helper/child path with a bounded instrumented local backend."""

from pathlib import Path
import json
import subprocess
import time
import wave

import pytest
from tools import whatsapp_ops_local_perception as stt


def _instrumented_backend(tmp_path, monkeypatch, mode):
    model = tmp_path / "model"
    model.mkdir()
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "NONE"))
        wav.writeframes(b"\0\0" * 1600)
    receipt = tmp_path / "limits.json"
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import os, sys, runpy, types, json, resource, time\n"
        f"mode={mode!r}\nreceipt={str(receipt)!r}\n"
        "class Model:\n"
        " def __init__(self,*args,**kwargs):\n"
        '  limits={k:resource.getrlimit(getattr(resource,k)) for k in ("RLIMIT_AS","RLIMIT_CPU","RLIMIT_FSIZE","RLIMIT_NOFILE")}\n'
        '  with open(receipt,"w") as f: json.dump({"limits":limits,"own_session":os.getsid(0)==os.getpid(),"key_absent":"OPENROUTER_API_KEY" not in os.environ},f)\n'
        " def transcribe(self,*args,**kwargs):\n"
        '  if mode=="partial":\n'
        '   os.write(1,b"{");time.sleep(2.5)\n'
        '  text="ç"*12000 if mode=="unicode" else "synthetic"\n'
        '  return [types.SimpleNamespace(text=text)],types.SimpleNamespace(language="pt")\n'
        'fake=types.ModuleType("faster_whisper");fake.WhisperModel=Model;sys.modules["faster_whisper"]=fake\n'
        'module=runpy.run_path(sys.argv[1]);raise SystemExit(module["_child_main"](sys.argv[1:]))\n',
        encoding="utf-8",
    )
    actual_popen = subprocess.Popen

    def launch(argv, **kwargs):
        # Insert instrumentation but execute the real candidate _child_main.
        return actual_popen([argv[0], str(driver), *argv[1:]], **kwargs)

    monkeypatch.setattr(stt.subprocess, "Popen", launch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-credential")
    return (
        stt.LocalMediaPerception({"stt_model_path": str(model), "max_threads": 2}),
        audio,
        receipt,
    )


def test_actual_child_enforces_hard_limits_before_backend(tmp_path, monkeypatch):
    helper, audio, receipt = _instrumented_backend(tmp_path, monkeypatch, "limits")
    assert helper.transcribe_audio(str(audio))["ok"]
    data = json.loads(receipt.read_text())
    assert data["own_session"] and data["key_absent"]
    for name, cap in [
        ("RLIMIT_AS", 1536 * 1024 * 1024),
        ("RLIMIT_CPU", 60),
        ("RLIMIT_FSIZE", 32 * 1024 * 1024),
        ("RLIMIT_NOFILE", 64),
    ]:
        soft, hard = data["limits"][name]
        assert 0 < soft <= hard <= cap, name


def test_partial_child_output_cannot_extend_absolute_deadline(tmp_path, monkeypatch):
    helper, audio, _ = _instrumented_backend(tmp_path, monkeypatch, "partial")
    helper._AUDIO_TIMEOUT_SECONDS = 0.5
    start = time.monotonic()
    result = helper.transcribe_audio(str(audio))
    elapsed = time.monotonic() - start
    assert not result["ok"] and result["error"] == "stt_timeout"
    assert elapsed < 1.8, f"partial stdout bypassed deadline: {elapsed:.2f}s"


def test_bounded_unicode_transcript_is_not_corrupted_by_json_limit(
    tmp_path, monkeypatch
):
    helper, audio, _ = _instrumented_backend(tmp_path, monkeypatch, "unicode")
    result = helper.transcribe_audio(str(audio))
    assert result["ok"]
    assert len(result["text"]) == 12000 and set(result["text"]) == {"ç"}
