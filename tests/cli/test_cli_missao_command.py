"""Tests for /missao CLI command dispatch.

Covers that process_command rewrites /missao to a mission-intake prompt
and puts it on _pending_input, mirroring the /queue pattern.
"""

import importlib
import os
import sys
from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_cli(env_overrides=None, config_overrides=None, **kwargs):
    """Minimal CLI factory (mirrors pattern from test_cli_init.py)."""
    _clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    if config_overrides:
        _clean_config.update(config_overrides)
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    if env_overrides:
        clean_env.update(env_overrides)
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
        "prompt_toolkit.auto_suggest": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), \
         patch.dict("os.environ", clean_env, clear=False):
        import cli as _cli_mod
        _cli_mod = importlib.reload(_cli_mod)
        with patch.object(_cli_mod, "get_tool_definitions", return_value=[]), \
             patch.dict(_cli_mod.__dict__, {"CLI_CONFIG": _clean_config}):
            return _cli_mod.HermesCLI(**kwargs)


class TestMissaoCliDispatch:
    """/missao on CLI should rewrite and inject the prompt via _pending_input."""

    def test_process_command_handles_missao(self):
        cli = _make_cli()
        result = cli.process_command("/missao")
        # process_command returns True to signal "continue" (not exit)
        assert result is True

    def test_missao_puts_prompt_on_pending_input(self):
        cli = _make_cli()
        cli.process_command("/missao")
        # _pending_input is a Queue; it should have the prompt now
        prompt = cli._pending_input.get_nowait()
        assert len(prompt) > 200
        assert "Mission" in prompt or "missão" in prompt.lower()

    def test_missao_preserves_user_args(self):
        cli = _make_cli()
        cli.process_command("/missao configurar Hunter")
        prompt = cli._pending_input.get_nowait()
        assert "configurar Hunter" in prompt

    def test_missao_empty_produces_self_contained(self):
        cli = _make_cli()
        cli.process_command("/missao")
        prompt = cli._pending_input.get_nowait()
        assert "Mission Contract" in prompt or "gate" in prompt.lower()

    def test_missao_whitespace_treated_as_empty(self):
        cli = _make_cli()
        cli.process_command("/missao   ")
        prompt_ws = cli._pending_input.get_nowait()

        cli2 = _make_cli()
        cli2.process_command("/missao")
        prompt_empty = cli2._pending_input.get_nowait()

        assert prompt_ws == prompt_empty
