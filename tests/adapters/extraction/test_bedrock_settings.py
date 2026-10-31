"""Building the Bedrock extractor from configuration. No real client is ever constructed."""

from typing import Any

import anthropic
import pytest

from payments_bot.adapters.extraction import claude_bedrock
from payments_bot.adapters.extraction.claude_bedrock import (
    BASE_URL_ENV,
    BedrockEndpoint,
    BedrockSettings,
    ExtractorConfigError,
    build_bedrock_extractor,
    resolve_endpoint,
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


@pytest.mark.parametrize("model", ["", " ", " anthropic.claude-opus-5-5", "anthropic. x"])
def test_settings_reject_a_blank_or_padded_model(model: str) -> None:
    with pytest.raises(ExtractorConfigError, match="model"):
        BedrockSettings(region="us-east-1", model=model)


# --- endpoint and auth mode, as the SDK will resolve them -----------------------------

REGIONAL = "https://bedrock-mantle.us-east-1.api.aws/anthropic"
PROXY = "https://proxy.example.test/anthropic"
TOKEN = "test-token-value"


@pytest.mark.parametrize(
    ("env", "profile", "base_url", "base_url_from", "bearer_token_from"),
    [
        ({}, None, REGIONAL, None, None),
        ({}, "payments", REGIONAL, None, None),
        ({"ANTHROPIC_BEDROCK_MANTLE_BASE_URL": PROXY}, None, PROXY, BASE_URL_ENV, None),
        ({"ANTHROPIC_BEDROCK_MANTLE_BASE_URL": ""}, None, "", BASE_URL_ENV, None),
        ({"AWS_BEARER_TOKEN_BEDROCK": TOKEN}, None, REGIONAL, None, "AWS_BEARER_TOKEN_BEDROCK"),
        ({"ANTHROPIC_AWS_API_KEY": TOKEN}, None, REGIONAL, None, "ANTHROPIC_AWS_API_KEY"),
        ({"AWS_BEARER_TOKEN_BEDROCK": ""}, None, REGIONAL, None, "AWS_BEARER_TOKEN_BEDROCK"),
        ({"AWS_BEARER_TOKEN_BEDROCK": TOKEN}, "payments", REGIONAL, None, None),
    ],
    ids=[
        "default-chain",
        "profile",
        "base-url-override",
        "empty-base-url-still-replaces-the-regional-endpoint",
        "bearer-token",
        "anthropic-aws-api-key",
        "empty-bearer-token-still-switches",
        "profile-wins-over-bearer-token",
    ],
)
def test_endpoint_follows_the_sdk_precedence(
    env: dict[str, str],
    profile: str | None,
    base_url: str,
    base_url_from: str | None,
    bearer_token_from: str | None,
) -> None:
    settings = BedrockSettings(region="us-east-1", profile=profile)

    endpoint = resolve_endpoint(settings, env)

    assert endpoint == BedrockEndpoint(
        base_url=base_url,
        base_url_from=base_url_from,
        bearer_token_from=bearer_token_from,
        profile=profile,
    )


def test_endpoint_description_names_variables_but_never_their_secret_values() -> None:
    env = {
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL": "https://user:s3cr3tpw@proxy.example.test/a?key=k3y",
        "AWS_BEARER_TOKEN_BEDROCK": TOKEN,
    }

    description = resolve_endpoint(BedrockSettings(region="us-east-1"), env).describe()

    assert "proxy.example.test/a" in description
    assert "ANTHROPIC_BEDROCK_MANTLE_BASE_URL" in description
    assert "bearer token" in description
    assert "AWS_BEARER_TOKEN_BEDROCK" in description
    for secret in ("s3cr3tpw", "k3y", TOKEN):
        assert secret not in description


PASSWORD_PARTS = ("pa55", "w0rd")


@pytest.mark.parametrize(
    "base_url",
    [
        "user:pa55w0rd@proxy.example.test/anthropic",
        "user:pa55w0rd@proxy.example.test/anthropic?next=https://example.test",
        "https:user:pa55w0rd@proxy.example.test/anthropic",
        "//user:pa55w0rd@proxy.example.test/anthropic",
        "https://user:pa55/w0rd@proxy.example.test/anthropic",
        "https://user:pa55?w0rd@proxy.example.test/anthropic",
        "https://user:pa55#w0rd@proxy.example.test/anthropic",
        "https://proxy.example.test/anthropic?key=pa55&k=w0rd",
        "https://proxy.example.test/anthropic#pa55w0rd",
        "ftp://user:pa55w0rd@proxy.example.test/anthropic",
    ],
    ids=[
        "no-scheme",
        "no-scheme-with-a-url-in-the-query",
        "scheme-without-slashes",
        "scheme-relative",
        "slash-in-password",
        "question-mark-in-password",
        "hash-in-password",
        "query",
        "fragment",
        "not-http",
    ],
)
def test_endpoint_description_hides_credentials_in_any_url_shape(base_url: str) -> None:
    """A URL parser ends the user part early at "/", "?" or "#", and finds no user part at
    all without "scheme://", so the password would be printed as part of the host or path."""
    description = resolve_endpoint(
        BedrockSettings(region="us-east-1"), {BASE_URL_ENV: base_url}
    ).describe()

    assert BASE_URL_ENV in description
    for part in PASSWORD_PARTS:
        assert part not in description


def test_endpoint_description_keeps_the_host_and_path_of_an_http_url() -> None:
    env = {BASE_URL_ENV: "https://user:pa55w0rd@proxy.example.test/anthropic?k=v#top"}

    endpoint = resolve_endpoint(BedrockSettings(region="us-east-1"), env)

    assert endpoint.base_url == "https://***@proxy.example.test/anthropic?***"


def test_endpoint_description_says_when_the_base_url_is_empty() -> None:
    endpoint = resolve_endpoint(BedrockSettings(region="us-east-1"), {BASE_URL_ENV: ""})

    assert "endpoint (empty)" in endpoint.describe()


@pytest.mark.parametrize(
    ("profile", "auth"),
    [(None, "SigV4 with the default AWS credential chain"), ("payments", "AWS profile payments")],
)
def test_endpoint_description_names_the_sigv4_credentials(profile: str | None, auth: str) -> None:
    endpoint = resolve_endpoint(BedrockSettings(region="us-east-1", profile=profile), {})

    assert REGIONAL in endpoint.describe()
    assert auth in endpoint.describe()


SDK_ENV_NAMES = (
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_AWS_API_KEY",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


@pytest.mark.parametrize(
    ("env", "profile"),
    [
        ({}, None),
        ({}, "payments"),
        ({"ANTHROPIC_BEDROCK_MANTLE_BASE_URL": PROXY}, None),
        ({"ANTHROPIC_BEDROCK_MANTLE_BASE_URL": ""}, None),
        ({"AWS_BEARER_TOKEN_BEDROCK": TOKEN}, None),
        ({"ANTHROPIC_AWS_API_KEY": TOKEN}, None),
        ({"AWS_BEARER_TOKEN_BEDROCK": TOKEN}, "payments"),
    ],
)
def test_endpoint_matches_what_the_real_sdk_client_resolves(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], profile: str | None
) -> None:
    """Drift guard. Constructing the client only reads these variables: it sends nothing and
    resolves no AWS credentials (those are read when a request is signed)."""
    for name in SDK_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = BedrockSettings(region="us-east-1", profile=profile)
    options: dict[str, Any] = {}

    def capture_options(**kwargs: Any) -> FakeClient:
        options.update(kwargs)
        return fake_client(answer())

    build_bedrock_extractor(settings, client_factory=capture_options)
    real = anthropic.AnthropicBedrockMantle(**options)

    endpoint = resolve_endpoint(settings, env)

    assert str(real.base_url).rstrip("/") == endpoint.base_url
    assert bool(real.auth_headers) == (endpoint.bearer_token_from is not None)


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
