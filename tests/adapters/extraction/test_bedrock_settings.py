"""Building the Bedrock extractor from configuration. No real client is ever constructed."""

from typing import Any

import anthropic
import pytest

from payments_bot.adapters.extraction import claude_bedrock
from payments_bot.adapters.extraction.claude_bedrock import (
    BedrockSettings,
    ExtractorConfigError,
    build_bedrock_extractor,
)
from payments_bot.ports.amount_extractor import ExtractionStatus
from tests.adapters.extraction.fakes import HAIKU, JPEG_BYTES, OPUS, FakeClient, answer, fake_client


def test_settings_require_an_explicit_region() -> None:
    with pytest.raises(ExtractorConfigError, match="AWS_REGION"):
        BedrockSettings.from_env({})


def test_settings_default_to_opus_at_medium_effort_with_the_default_credential_chain() -> None:
    settings = BedrockSettings.from_env({"AWS_REGION": "us-east-1"})

    assert settings == BedrockSettings(
        region="us-east-1", model=OPUS, effort="medium", profile=None
    )


def test_settings_read_model_effort_and_profile_overrides() -> None:
    env = {
        "AWS_REGION": " us-west-2 ",
        "EXTRACTOR_MODEL": HAIKU,
        "EXTRACTOR_EFFORT": "high",
        "AWS_PROFILE": "payments",
    }

    settings = BedrockSettings.from_env(env)

    assert settings == BedrockSettings(
        region="us-west-2", model=HAIKU, effort="high", profile="payments"
    )


def test_blank_variables_fall_back_to_defaults() -> None:
    env = {
        "AWS_REGION": "us-east-1",
        "EXTRACTOR_MODEL": " ",
        "EXTRACTOR_EFFORT": "",
        "AWS_PROFILE": "",
    }

    settings = BedrockSettings.from_env(env)

    assert settings == BedrockSettings(region="us-east-1")


def test_settings_reject_an_unknown_effort() -> None:
    with pytest.raises(ExtractorConfigError, match="EXTRACTOR_EFFORT"):
        BedrockSettings.from_env({"AWS_REGION": "us-east-1", "EXTRACTOR_EFFORT": "turbo"})


def test_settings_reject_a_first_party_model_id() -> None:
    with pytest.raises(ExtractorConfigError, match="anthropic."):
        BedrockSettings.from_env({"AWS_REGION": "us-east-1", "EXTRACTOR_MODEL": "claude-opus-5-5"})


def test_the_real_client_is_the_bedrock_mantle_client() -> None:
    assert claude_bedrock.DEFAULT_CLIENT_FACTORY is anthropic.AnthropicBedrockMantle


@pytest.mark.parametrize(
    ("profile", "expected_kwargs"),
    [
        (None, {"aws_region": "eu-west-1"}),
        ("payments", {"aws_region": "eu-west-1", "aws_profile": "payments"}),
    ],
)
def test_factory_wires_region_profile_model_and_effort(
    profile: str | None, expected_kwargs: dict[str, Any]
) -> None:
    received: list[dict[str, Any]] = []
    client = fake_client(answer())

    def client_factory(**kwargs: Any) -> FakeClient:
        received.append(kwargs)
        return client

    settings = BedrockSettings(region="eu-west-1", model=HAIKU, effort="low", profile=profile)

    extractor = build_bedrock_extractor(settings, client_factory=client_factory)
    extraction = extractor.extract(JPEG_BYTES)

    assert received == [expected_kwargs]
    assert extraction.status is ExtractionStatus.OK
    assert extraction.model == HAIKU
    (request,) = client.messages.calls
    assert request["model"] == HAIKU
    assert request["output_config"]["effort"] == "low"
