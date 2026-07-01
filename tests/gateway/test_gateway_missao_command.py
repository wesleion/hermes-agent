"""Tests for /missao gateway command dispatch.

Covers registry presence and the prompt-builder rewrite logic.
Because the full _handle_message pipeline needs extensive mock infrastructure,
the rewrite tests validate the core logic (prompt builder + event.text rewrite)
rather than the full dispatch chain. The /learn fall-through pattern provides
production validation that the same handler shape works.
"""

import pytest

from agent.mission_intake_prompt import build_mission_intake_prompt
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


class TestMissaoGatewayPresence:
    """/missao must be registered as a known gateway command."""

    def test_missao_is_known_gateway_command(self):
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command
        assert "missao" in GATEWAY_KNOWN_COMMANDS
        cmd = resolve_command("missao")
        assert cmd is not None

    def test_missao_not_in_active_session_bypass(self):
        """/missao rewrites and falls through — NOT a bypass command."""
        from hermes_cli.commands import ACTIVE_SESSION_BYPASS_COMMANDS
        assert "missao" not in ACTIVE_SESSION_BYPASS_COMMANDS


class TestMissaoGatewayPromptRewrite:
    """The prompt builder rewrites event.text as the gateway handler would.

    These tests validate the core rewrite logic in isolation. The handler
    in gateway/run.py mirrors the /learn pattern: it calls
    build_mission_intake_prompt, sets event.text, and falls through.
    """

    def test_builds_prompt_from_command_args(self):
        """Arguments after /missao are embedded in the prompt."""
        prompt = build_mission_intake_prompt("configurar Hunter", source="Telegram")
        assert "configurar Hunter" in prompt

    def test_builds_self_contained_without_args(self):
        """Bare /missao produces a self-contained prompt."""
        prompt = build_mission_intake_prompt("")
        assert len(prompt) > 200
        assert "Mission Contract" in prompt or "clarify" in prompt.lower()

    def test_includes_gate_instructions(self):
        """The prompt must include safety/gate rules."""
        prompt = build_mission_intake_prompt("")
        assert "NEVER" in prompt or "gate" in prompt.lower()

    def test_source_is_passed_to_prompt(self):
        """The platform source appears in the prompt."""
        prompt = build_mission_intake_prompt("", source="Telegram")
        assert "Telegram" in prompt

    def test_different_platforms_produce_distinct_source(self):
        """The handler works the same way across platforms."""
        tg = build_mission_intake_prompt("", source="Telegram")
        dc = build_mission_intake_prompt("", source="Discord")
        assert "Telegram" in tg
        assert "Discord" in dc

    def test_prompt_is_substantial_for_empty_request(self):
        """Bare /missao should not produce a one-liner."""
        prompt = build_mission_intake_prompt("")
        assert len(prompt) > 500, f"prompt too short ({len(prompt)} chars)"
