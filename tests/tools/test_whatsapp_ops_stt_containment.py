"""OS-level containment contracts for the private local STT child."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))


def _state(pid: int) -> str | None:
    try:
        return (
            Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split(") ", 1)[1][0]
        )
    except (FileNotFoundError, ProcessLookupError):
        return None


def test_process_disappearance_during_proc_read_is_gone(monkeypatch) -> None:
    original = Path.read_text
    sentinel = Path("/proc/987654321/stat")

    def disappeared(path, *args, **kwargs):
        if path == sentinel:
            raise ProcessLookupError(3, "synthetic process exit during proc read")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", disappeared)
    assert _state(987654321) is None


def _wait_gone(pid: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _state(pid)
        if state is None:
            return
        # Orphans are reaped by init asynchronously. Observe until the bounded
        # deadline; a transient zombie is not a live descendant or a leak.
        time.sleep(0.05)
    if _state(pid) == "Z":
        pytest.fail("STT descendant remained a zombie past the reap deadline")
    pytest.fail("STT descendant remained alive")


def _probe(
    tmp_path: Path, *, contained: bool
) -> tuple[subprocess.Popen[str], dict[str, object]]:
    probe = tmp_path / ("contained.py" if contained else "legacy.py")
    apply = "_apply_worker_limits()" if contained else ""
    probe.write_text(
        "import json, os, resource, sys, time\n"
        f"sys.path.insert(0, {str(TOOLS)!r})\n"
        "from whatsapp_ops_local_perception import _apply_worker_limits\n"
        f"{apply}\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    while True: time.sleep(1)\n"
        "print(json.dumps({'child': child, 'limits': "
        "{name: resource.getrlimit(value) for name, value in "
        "{'as': resource.RLIMIT_AS, 'cpu': resource.RLIMIT_CPU, "
        "'nofile': resource.RLIMIT_NOFILE, 'fsize': resource.RLIMIT_FSIZE}.items()}}), flush=True)\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(probe)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    return process, json.loads(process.stdout.readline())


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(
    os.name == "nt", reason="RLIMIT and process groups are POSIX contracts"
)
def test_private_stt_child_applies_limits_and_group_kill_reaps_descendant(
    tmp_path: Path,
) -> None:
    """Reproduces legacy PID-only escape, then verifies real child limits and cleanup."""
    from whatsapp_ops_local_perception import _kill_process_group

    legacy, legacy_message = _probe(tmp_path, contained=False)
    legacy_child = int(legacy_message["child"])
    try:
        legacy.terminate()
        legacy.wait(timeout=5)
        assert _state(legacy_child) not in (None, "Z"), (
            "legacy PID-only cleanup must leak a live child"
        )
    finally:
        if _state(legacy_child) not in (None, "Z"):
            os.kill(legacy_child, signal.SIGKILL)
        _wait_gone(legacy_child)

    process, message = _probe(tmp_path, contained=True)
    child = int(message["child"])
    try:
        limits = message["limits"]
        assert 0 < limits["as"][0] < 2 * 1024**3
        assert 0 < limits["cpu"][0] <= 60
        assert 0 < limits["nofile"][0] <= 128
        assert 0 < limits["fsize"][0] <= 32 * 1024**2
        _kill_process_group(process)
        _wait_gone(child)
    finally:
        if process.poll() is None:
            _kill_process_group(process)
        if _state(child) not in (None, "Z"):
            os.kill(child, signal.SIGKILL)
        _wait_gone(child)
