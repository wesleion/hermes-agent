"""Tests for mission_intake_prompt — structured mission request intake.

Covers the shared prompt builder (agent.mission_intake_prompt.build_mission_intake_prompt).
The builder produces a system-style prompt that guides the agent to elicit a
Mission Contract from the user's raw request. No engine, no model tool: these
are behavior contracts for the prompt content.
"""

from agent.mission_intake_prompt import build_mission_intake_prompt


class TestBuildMissionIntakePrompt:
    def test_embeds_the_raw_request_verbatim(self):
        req = "criar um script que monitora a pasta uploads e move arquivos PDF"
        prompt = build_mission_intake_prompt(req)
        assert req in prompt

    def test_empty_request_is_self_sufficient(self):
        prompt = build_mission_intake_prompt("")
        assert "request" in prompt.lower() or "mission" in prompt.lower()
        assert "objetivo" in prompt.lower()
        assert "autonomia" in prompt.lower()

    def test_whitespace_only_request_treated_as_empty(self):
        assert build_mission_intake_prompt("   \n  ") == build_mission_intake_prompt("")

    def test_clarify_instruction_missing_fields(self):
        prompt = build_mission_intake_prompt("deploy a cron job")
        assert "clarify" in prompt.lower()

    def test_mission_contract_fields_present(self):
        prompt = build_mission_intake_prompt("build a slack bot")
        for field in ("objetivo", "tipo", "autonomia", "superficie", "sucesso", "gates", "reporting"):
            assert field in prompt.lower()

    def test_autonomy_levels_a0_to_a5(self):
        prompt = build_mission_intake_prompt("monitor system health")
        for level in ("A0", "A1", "A2", "A3", "A4", "A5"):
            assert level in prompt

    def test_protected_resources_guarded(self):
        prompt = build_mission_intake_prompt("set up a new tool")
        for protected in ("secret", "provider", "cron", "gateway"):
            assert protected in prompt.lower()
        assert "NEVER" in prompt or "ALWAYS" in prompt

    def test_source_included_when_provided(self):
        prompt = build_mission_intake_prompt("help me debug", source="telegram")
        assert "telegram" in prompt

    def test_not_specified_when_source_omitted(self):
        prompt = build_mission_intake_prompt("set up a new tool")
        assert "Source: (not specified)" in prompt

    def test_returns_string(self):
        assert isinstance(build_mission_intake_prompt("anything"), str)

    def test_empty_with_source(self):
        prompt = build_mission_intake_prompt("", source="discord")
        assert "discord" in prompt
        assert "objetivo" in prompt.lower()
