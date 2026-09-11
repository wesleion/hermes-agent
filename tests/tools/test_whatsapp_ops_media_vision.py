"""Vision evidence validation is exercised with real PNG bytes, not text placeholders."""

import io
import json
from types import SimpleNamespace
import pytest
from PIL import Image
from tools.whatsapp_ops_media_vision import describe_images


def png():
    data = io.BytesIO()
    Image.new("RGB", (24, 24), "green").save(data, format="PNG")
    return data.getvalue()


@pytest.mark.parametrize(
    "provider,model",
    [
        ("openrouter", "google/gemini-2.5-flash-lite"),
        ("openai-codex", "gpt-5.5"),
        ("gemini", "gemini-2.5-flash-lite"),
    ],
)
def test_vision_sends_pixels_and_preserves_ocr_negation(provider, model):
    calls = []

    def call(client, content, resolved):
        calls.append(content)
        assert resolved == model
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps({
                            "ocr_text": "NÃO QUERO",
                            "description": "figurinha",
                            "uncertainty": "não inferir intenção",
                        })
                    )
                )
            ]
        )

    result = describe_images(
        [png()],
        {"auxiliary": {"vision": {"provider": provider, "model": model}}},
        client=object(),
        call=call,
    )
    assert result["ok"] and result["ocr_text"] == "NÃO QUERO" and len(calls) == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"description": "missing fields"},
        [{"ocr_text": "x", "description": "x", "uncertainty": ""}],
        {"ocr_text": 2, "description": "x", "uncertainty": ""},
        {"ocr_text": "", "description": "", "uncertainty": ""},
        "not-json",
    ],
)
def test_invalid_vision_evidence_never_becomes_authoritative_text(raw):
    result = describe_images(
        [png()],
        {
            "auxiliary": {
                "vision": {
                    "provider": "openrouter",
                    "model": "google/gemini-2.5-flash-lite",
                }
            }
        },
        client=object(),
        call=lambda *_: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(raw)))]
        ),
    )
    assert not result["ok"] and result["error"] == "vision_invalid_output"


def test_invalid_or_missing_route_does_not_call_provider():
    def forbidden(*args):
        raise AssertionError("provider reached")

    assert (
        describe_images([png()], {}, client=object(), call=forbidden)["error"]
        == "vision_not_configured"
    )
    assert (
        describe_images(
            [b"not-png"],
            {"auxiliary": {"vision": {"provider": "openai-codex", "model": "gpt-5.5"}}},
            client=object(),
            call=forbidden,
        )["error"]
        == "vision_frames_invalid"
    )


def test_real_resolver_result_must_match_requested_route(monkeypatch):
    import agent.auxiliary_client as aux

    monkeypatch.setattr(
        aux,
        "resolve_vision_provider_client",
        lambda **kw: ("unexpected", object(), kw["model"]),
    )
    assert (
        describe_images(
            [png()],
            {
                "auxiliary": {
                    "vision": {
                        "provider": "openrouter",
                        "model": "google/gemini-2.5-flash-lite",
                    }
                }
            },
        )["error"]
        == "vision_not_configured"
    )


def test_actual_client_receives_timeout_no_tools_and_no_automatic_fallback():
    seen = {}

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **kwargs):
            seen["options"] = kwargs
            return self

        def create(self, **kwargs):
            seen["request"] = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"ocr_text":"","description":"verde","uncertainty":""}'
                        )
                    )
                ]
            )

    result = describe_images(
        [png()],
        {
            "auxiliary": {
                "vision": {
                    "provider": "openrouter",
                    "model": "google/gemini-2.5-flash-lite",
                    "timeout": 5,
                }
            }
        },
        client=Client(),
    )
    assert result["ok"]
    assert seen["options"]["max_retries"] == 0 and seen["request"]["timeout"] == 5
    assert seen["request"]["tools"] == []
    assert seen["request"]["extra_body"]["provider"]["allow_fallbacks"] is False
