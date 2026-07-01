"""Tests for /missao handling in tui_gateway.

/missao queues a normal agent message in the CLI. The TUI slash-worker process
cannot read CLI _pending_input, so both command.dispatch and slash.exec must
handle it directly and return a {type: send, message: ...} payload.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home


@pytest.fixture()
def server(hermes_home):
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


@pytest.fixture()
def session(server):
    sid = "sid-missao"
    server._sessions[sid] = {
        "session_key": "tui-missao-session-1",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
    }
    return sid


def _call(server, method, **params):
    return server._methods[method](1, params)


def test_missao_is_pending_input_command(server):
    assert "missao" in server._PENDING_INPUT_COMMANDS


def test_command_dispatch_missao_returns_send_payload(server, session):
    response = _call(
        server,
        "command.dispatch",
        name="missao",
        arg="configurar Hunter",
        session_id=session,
    )

    result = response["result"]
    assert result["type"] == "send"
    assert "Starting Mission Intake" in result["notice"]
    assert "configurar Hunter" in result["message"]
    assert "Mission Contract" in result["message"]
    assert "Source: tui" in result["message"]


def test_slash_exec_missao_routes_to_command_dispatch(server, session):
    response = _call(
        server,
        "slash.exec",
        command="missao configurar Hunter",
        session_id=session,
    )

    result = response["result"]
    assert result["type"] == "send"
    assert "configurar Hunter" in result["message"]
    assert "Mission Contract" in result["message"]
