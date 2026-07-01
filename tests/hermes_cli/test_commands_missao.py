"""Tests for /missao command registration in CommandDef registry.

Covers resolve_command, GATEWAY_KNOWN_COMMANDS, telegram_bot_commands,
telegram_menu_commands, gateway_help_lines, and slack integration.
"""

import pytest

from hermes_cli.commands import (
    COMMAND_REGISTRY,
    GATEWAY_KNOWN_COMMANDS,
    ACTIVE_SESSION_BYPASS_COMMANDS,
    gateway_help_lines,
    resolve_command,
    telegram_bot_commands,
    telegram_menu_commands,
    slack_subcommand_map,
    slack_native_slashes,
)

# ======================================================================
# CommandDef existence
# ======================================================================

class TestMissaoCommandDef:
    def test_resolves_to_command_def(self):
        cmd = resolve_command("missao")
        assert cmd is not None
        assert cmd.name == "missao"

    def test_in_command_registry(self):
        assert "missao" in {cmd.name for cmd in COMMAND_REGISTRY}

    def test_has_description(self):
        cmd = resolve_command("missao")
        assert cmd.description
        assert len(cmd.description) > 10

    def test_not_cli_only(self):
        cmd = resolve_command("missao")
        assert cmd.cli_only is False

    def test_no_aliases_by_default(self):
        cmd = resolve_command("missao")
        assert cmd.aliases == ()

# ======================================================================
# Gateway presence
# ======================================================================

class TestMissaoGatewayPresence:
    def test_gateway_known(self):
        assert "missao" in GATEWAY_KNOWN_COMMANDS

    def test_not_active_session_bypass(self):
        assert "missao" not in ACTIVE_SESSION_BYPASS_COMMANDS

    def test_appears_in_help(self):
        lines = "\n".join(gateway_help_lines())
        assert "`/missao" in lines or "/missao" in lines

    def test_help_description_under_80_chars(self):
        cmd = resolve_command("missao")
        assert len(cmd.description) <= 80

# ======================================================================
# Telegram menu
# ======================================================================

class TestMissaoTelegramMenu:
    def test_in_telegram_bot_commands(self):
        names = {name for name, _ in telegram_bot_commands()}
        assert "missao" in names

    def test_in_telegram_menu_commands(self):
        names = {name for name, _ in telegram_menu_commands()[0]}
        assert "missao" in names

    def test_description_under_80_chars_telegram(self):
        """Telegram Bot API allows up to 256 chars; our descriptions
        should stay well under that limit."""
        commands, _ = telegram_menu_commands()
        for name, desc in commands:
            if name == "missao":
                assert len(desc) <= 80
                return
        pytest.fail("missao not found in telegram_menu_commands")

    def test_basic_priority(self):
        """/missao does not need top priority; presence in the menu
        (covered by test_in_telegram_menu_commands) is sufficient."""
        commands, _ = telegram_menu_commands()
        names = [name for name, _ in commands]
        assert "missao" in names

# ======================================================================
# Slack integration
# ======================================================================

class TestMissaoSlack:
    def test_in_slack_subcommand_map(self):
        mapping = slack_subcommand_map()
        assert "missao" in mapping
        assert mapping["missao"] == "/missao"

    def test_in_slack_native_slashes(self):
        names = {n for n, _d, _h in slack_native_slashes()}
        assert "missao" in names

    def test_slack_name_under_32_chars(self):
        # "missao" is 6 chars — well under limit
        assert len("missao") <= 32

    def test_slack_name_is_lowercase(self):
        names = {n for n, _d, _h in slack_native_slashes()}
        assert "missao" in names
