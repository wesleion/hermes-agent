"""Cross-process lifecycle contracts for private media scratch directories."""

from __future__ import annotations

from datetime import datetime, timezone
import multiprocessing
import os
from pathlib import Path
import threading

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
import gateway.whatsapp_ops_media_worker as media_worker
from gateway.whatsapp_ops_media_worker import WhatsAppOpsMediaWorker


def _config():
    return {
        "whatsapp_ops": {"media_perception": {"enabled": True, "cache_ttl_seconds": 1}}
    }


def _worker():
    return WhatsAppOpsMediaWorker(
        config=_config(), clock=lambda: datetime.now(timezone.utc)
    )


def _active_worker(home, ready, release, results):
    token = set_hermes_home_override(Path(home))
    try:
        with _worker()._cache_dir() as directory:
            directory = Path(directory)
            artifact = directory / "normalized.wav"
            artifact.write_bytes(b"single backend result")
            os.utime(directory, (1, 1))
            results.put(("ready", str(artifact)))
            ready.set()
            release.wait(10)
            results.put(("done", artifact.exists(), 1))
    finally:
        reset_hermes_home_override(token)


def _cleanup_worker(home, entered, results):
    token = set_hermes_home_override(Path(home))
    try:
        entered.set()
        _worker()._cleanup()
        results.put(("cleaned",))
    finally:
        reset_hermes_home_override(token)


def _join(process):
    process.join(10)
    assert process.exitcode == 0


def test_active_old_media_directory_survives_another_workers_cleanup(tmp_path):
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    results = context.Queue()
    active = context.Process(
        target=_active_worker, args=(tmp_path, ready, release, results)
    )
    active.start()
    assert ready.wait(5)
    kind, artifact = results.get(timeout=5)
    assert kind == "ready"

    cleaner = context.Process(
        target=_cleanup_worker, args=(tmp_path, context.Event(), results)
    )
    cleaner.start()
    _join(cleaner)
    assert Path(artifact).exists()

    release.set()
    _join(active)
    assert results.get(timeout=5) == ("cleaned",)
    assert results.get(timeout=5) == ("done", True, 1)
    assert not Path(artifact).parent.exists()


def test_expired_orphan_is_removed_but_symlink_is_not_followed(tmp_path, monkeypatch):
    token = set_hermes_home_override(tmp_path)
    try:
        worker = _worker()
        root = worker._cache_root()
        orphan = root / "media_orphan"
        orphan.mkdir()
        (orphan / "artifact").write_bytes(b"orphan")
        os.utime(orphan, (1, 1))
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "media_link"
        link.symlink_to(outside, target_is_directory=True)

        worker._cleanup()
        assert not orphan.exists()
        assert link.is_symlink() and outside.exists()
    finally:
        reset_hermes_home_override(token)


def test_cache_context_removes_artifacts_after_exception(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        worker = _worker()
        with pytest.raises(RuntimeError, match="decoder failed"):
            with worker._cache_dir() as directory:
                directory = Path(directory)
                artifact = directory / "normalized.wav"
                artifact.write_bytes(b"partial")
                raise RuntimeError("decoder failed")
        assert not artifact.parent.exists()
    finally:
        reset_hermes_home_override(token)


def test_creation_is_not_scavenged_between_mkdir_and_lock(tmp_path, monkeypatch):
    token = set_hermes_home_override(tmp_path)
    try:
        original_mkdtemp = media_worker.tempfile.mkdtemp
        created, proceed, inside, release = (threading.Event() for _ in range(4))
        state = {}
        result = multiprocessing.get_context("fork").Queue()

        def staged_mkdtemp(*args, **kwargs):
            directory = original_mkdtemp(*args, **kwargs)
            os.utime(directory, (1, 1))
            created.set()
            assert proceed.wait(5)
            return directory

        def creator():
            with _worker()._cache_dir() as directory:
                directory = Path(directory)
                artifact = directory / "normalized.wav"
                artifact.write_bytes(b"protected")
                state["artifact"] = artifact
                inside.set()
                assert release.wait(5)

        monkeypatch.setattr(media_worker.tempfile, "mkdtemp", staged_mkdtemp)
        thread = threading.Thread(target=creator)
        thread.start()
        assert created.wait(5)
        entered = multiprocessing.get_context("fork").Event()
        cleaner = multiprocessing.get_context("fork").Process(
            target=_cleanup_worker, args=(tmp_path, entered, result)
        )
        cleaner.start()
        assert entered.wait(5)
        proceed.set()
        assert inside.wait(5)
        _join(cleaner)
        assert result.get(timeout=5) == ("cleaned",)
        assert state["artifact"].exists()
        release.set()
        thread.join(5)
        assert not thread.is_alive()
    finally:
        reset_hermes_home_override(token)
