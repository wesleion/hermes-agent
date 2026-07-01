"""Gateway skill slash-command cache refresh tests."""


def test_skill_command_resolution_rescans_before_unknown(monkeypatch):
    """A skill installed after gateway startup should resolve before /unknown."""
    from gateway.run import _resolve_skill_command_after_optional_rescan

    state = {"scanned": False}

    def fake_get_skill_commands():
        if not state["scanned"]:
            return {}
        return {
            "/prompt-up": {
                "name": "prompt-up",
                "description": "Improve prompts",
                "skill_md_path": "/tmp/skills/prompt-up/SKILL.md",
                "skill_dir": "/tmp/skills/prompt-up",
            }
        }

    def fake_resolve_skill_command_key(command: str):
        key = f"/{command.replace('_', '-')}"
        return key if key in fake_get_skill_commands() else None

    def fake_scan_skill_commands():
        state["scanned"] = True
        return fake_get_skill_commands()

    monkeypatch.setattr("agent.skill_commands.get_skill_commands", fake_get_skill_commands)
    monkeypatch.setattr("agent.skill_commands.resolve_skill_command_key", fake_resolve_skill_command_key)
    monkeypatch.setattr("agent.skill_commands.scan_skill_commands", fake_scan_skill_commands)

    cmd_key, skill_cmds = _resolve_skill_command_after_optional_rescan("prompt-up")

    assert state["scanned"] is True
    assert cmd_key == "/prompt-up"
    assert "/prompt-up" in skill_cmds
