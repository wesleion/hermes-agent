from __future__ import annotations


def test_concrete_generator_is_zero_tool_and_validates_json():
    from tools.whatsapp_ops_conversation import FriendsHermesGenerator

    seen = {}

    class Agent:
        tools = []
        skip_memory = True

        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run_conversation(self, *_args, **_kwargs):
            return {
                "final_response": '{"stage":"qualify","qualification":{"problem":"x"},"action":"qualify","blocks":["Posso entender melhor o processo atual?"],"next_step":"conversar","escalation":false}'
            }

    generator = FriendsHermesGenerator(
        agent_factory=Agent,
        config_loader=lambda: {"model": {"provider": "test", "default": "model"}},
        runtime_resolver=lambda **_k: {
            "provider": "test",
            "api_key": "k",
            "base_url": "u",
            "api_mode": "chat_completions",
        },
    )
    result = generator.generate(
        grant_id="g",
        contact_id="c",
        channel_id="ch",
        messages=[{"role": "user", "text": "oi"}],
        offer={"id": "offer"},
        highwatermark="e",
    )
    assert result and result["blocks"] == ["Posso entender melhor o processo atual?"]
    assert seen["enabled_toolsets"] == [] and seen["skip_memory"] is True
    assert seen["skip_context_files"] is True and seen["fallback_model"] is None


def test_generator_accepts_zero_block_terminal_actions_and_bounds_metadata():
    from tools.whatsapp_ops_conversation import validate_friends_generation

    terminal = {"stage": "handoff", "qualification": {"problem": "x"}, "action": "escalate", "blocks": [], "next_step": "human review", "escalation": True}
    assert validate_friends_generation(terminal) == terminal
    terminal["stage"] = "x" * 81
    assert validate_friends_generation(terminal) is None


def test_generator_rejects_unbounded_commercial_claim():
    from tools.whatsapp_ops_conversation import validate_friends_generation

    assert (
        validate_friends_generation({
            "stage": "x",
            "qualification": {},
            "action": "qualify",
            "blocks": ["O preço é R$ 10"],
            "next_step": "x",
            "escalation": False,
        })
        is None
    )
