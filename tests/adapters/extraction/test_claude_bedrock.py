import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import anthropic
import pytest
from anthropic.types import Message

from payments_bot.adapters.extraction.claude_bedrock import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    ClaudeBedrockExtractor,
    ExtractorConfigError,
)
from payments_bot.adapters.extraction.receipt_prompt import RECEIPT_SCHEMA, SYSTEM_PROMPT
from payments_bot.ports.amount_extractor import (
    Confidence,
    Extraction,
    ExtractionStatus,
    TokenUsage,
)
from tests.adapters.extraction.fakes import (
    FAKE_REQUEST,
    GIF_BYTES,
    HAIKU,
    JPEG_BYTES,
    OPUS,
    PNG_BYTES,
    WEBP_BYTES,
    FakeMessages,
    answer,
    fake_client,
    message,
    receipt_payload,
    status_error,
)

LIMA_OFFSET = timedelta(hours=-5)


def run(
    reply: Message | Exception, image: bytes = JPEG_BYTES, **options: Any
) -> tuple[Extraction, FakeMessages]:
    client = fake_client(reply)
    extraction = ClaudeBedrockExtractor(client, **options).extract(image)
    return extraction, client.messages


def sent_request(messages: FakeMessages) -> dict[str, Any]:
    (request,) = messages.calls
    return request


# --- request construction -------------------------------------------------------------


def test_defaults_are_claude_opus_5_5_on_bedrock_at_medium_effort() -> None:
    assert DEFAULT_MODEL == "anthropic.claude-opus-5-5"
    assert DEFAULT_EFFORT == "medium"


def test_request_sends_the_image_with_the_frozen_prompt_and_strict_output_schema() -> None:
    _, messages = run(answer())

    request = sent_request(messages)

    assert request["model"] == OPUS
    assert request["max_tokens"] == 16000
    assert request["system"] == SYSTEM_PROMPT
    assert request["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": RECEIPT_SCHEMA},
    }
    (turn,) = request["messages"]
    assert turn["role"] == "user"
    image_block, text_block = turn["content"]
    assert image_block == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(JPEG_BYTES).decode("ascii"),
        },
    }
    assert text_block["type"] == "text"
    assert text_block["text"].strip()


def test_request_omits_parameters_claude_opus_5_5_rejects() -> None:
    _, messages = run(answer())

    request = sent_request(messages)

    for rejected in ("thinking", "temperature", "top_p", "top_k", "tool_choice", "tools"):
        assert rejected not in request
    assert [turn["role"] for turn in request["messages"]] == ["user"]  # no assistant prefill


def test_model_and_effort_are_configurable() -> None:
    extraction, messages = run(answer(), model=HAIKU, effort="low")

    request = sent_request(messages)

    assert request["model"] == HAIKU
    assert request["output_config"]["effort"] == "low"
    assert extraction.model == HAIKU


@pytest.mark.parametrize("effort", ["", "minimal", "MEDIUM"])
def test_unknown_effort_is_rejected_at_construction(effort: str) -> None:
    with pytest.raises(ExtractorConfigError):
        ClaudeBedrockExtractor(fake_client(answer()), effort=effort)


@pytest.mark.parametrize(
    ("image", "media_type"),
    [
        (JPEG_BYTES, "image/jpeg"),
        (PNG_BYTES, "image/png"),
        (WEBP_BYTES, "image/webp"),
        (GIF_BYTES, "image/gif"),
    ],
)
def test_media_type_is_detected_from_the_image_bytes(image: bytes, media_type: str) -> None:
    _, messages = run(answer(), image=image)

    image_block = sent_request(messages)["messages"][0]["content"][0]

    assert image_block["source"]["media_type"] == media_type


@pytest.mark.parametrize(
    "image",
    [b"%PDF-1.7\n" + bytes(32), b"BM" + bytes(32), b"", b"\xff\xd8"],
    ids=["pdf", "bmp", "empty", "truncated-jpeg"],
)
def test_unsupported_image_fails_without_calling_the_provider(image: bytes) -> None:
    extraction, messages = run(answer(), image=image)

    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.error is not None
    assert "JPEG" in extraction.error
    assert extraction.amount_minor is None
    assert messages.calls == []


def test_output_schema_is_closed_and_requires_every_field() -> None:
    assert RECEIPT_SCHEMA["type"] == "object"
    assert RECEIPT_SCHEMA["additionalProperties"] is False
    assert (
        set(RECEIPT_SCHEMA["required"])
        == set(RECEIPT_SCHEMA["properties"])
        == {
            "is_payment_receipt",
            "amount",
            "currency",
            "operation_number",
            "paid_at",
            "provider",
            "confidence",
        }
    )
    assert RECEIPT_SCHEMA["properties"]["confidence"]["enum"] == ["high", "medium", "low"]


@pytest.mark.parametrize(
    "phrase",
    [
        "S/",
        "Monto transferido",
        "¡Yapeaste!",
        "operation number",
        "Código de seguridad",
        "phone",
        "account",
        "date",
        "banner",
        "is_payment_receipt",
        "Never guess",
    ],
)
def test_system_prompt_names_the_amount_and_its_known_traps(phrase: str) -> None:
    assert phrase in SYSTEM_PROMPT


# --- successful extraction ------------------------------------------------------------


@pytest.mark.parametrize(("amount", "minor"), [("250", 25000), ("40.00", 4000), ("0.5", 50)])
def test_receipt_amount_is_converted_to_minor_units(amount: str, minor: int) -> None:
    reply = answer(amount=amount)

    extraction, _ = run(reply)

    assert extraction.status is ExtractionStatus.OK
    assert extraction.amount_minor == minor
    assert extraction.currency == "PEN"
    assert extraction.reference == "00012345"
    assert extraction.provider == "Yape"
    assert extraction.confidence is Confidence.HIGH
    assert extraction.model == OPUS
    assert extraction.error is None
    assert extraction.raw_json is not None
    assert json.loads(extraction.raw_json) == receipt_payload(amount=amount)


def test_usage_is_reported_for_cost_comparison() -> None:
    extraction, _ = run(
        message(json.dumps(receipt_payload()), input_tokens=2100, output_tokens=310)
    )

    assert extraction.usage == TokenUsage(input_tokens=2100, output_tokens=310)


def test_naive_receipt_time_is_read_as_lima_local_time() -> None:
    extraction, _ = run(answer(paid_at="2026-01-15T09:30:00"))

    assert extraction.paid_at is not None
    assert extraction.paid_at.utcoffset() == LIMA_OFFSET
    assert extraction.paid_at == datetime(2026, 10, 23, 1, 28, tzinfo=UTC)


@pytest.mark.parametrize("paid_at", ["2026-01-15T09:30:00-05:00", "2026-01-15T14:30:00Z"])
def test_receipt_time_with_an_offset_keeps_its_instant(paid_at: str) -> None:
    extraction, _ = run(answer(paid_at=paid_at))

    assert extraction.paid_at == datetime(2026, 10, 23, 1, 28, tzinfo=UTC)


def test_date_only_receipt_time_is_midnight_in_lima() -> None:
    extraction, _ = run(answer(paid_at="2026-01-15"))

    assert extraction.paid_at == datetime(2026, 10, 22, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize("paid_at", [None, "15 ene. 2026 09:30 a. m.", ""])
def test_missing_or_unparsable_receipt_time_does_not_block_the_amount(
    paid_at: str | None,
) -> None:
    extraction, _ = run(answer(paid_at=paid_at))

    assert extraction.status is ExtractionStatus.OK
    assert extraction.paid_at is None


def test_blank_optional_fields_become_none() -> None:
    extraction, _ = run(answer(operation_number="  ", provider=None, confidence="medium"))

    assert extraction.reference is None
    assert extraction.provider is None
    assert extraction.confidence is Confidence.MEDIUM


# --- explicit non-ok outcomes ---------------------------------------------------------


def test_missing_amount_is_unreadable_not_guessed() -> None:
    extraction, _ = run(answer(amount=None, confidence="low"))

    assert extraction.status is ExtractionStatus.UNREADABLE
    assert extraction.amount_minor is None
    assert extraction.reference == "00012345"
    assert extraction.confidence is Confidence.LOW
    assert extraction.error is not None


def test_non_receipt_is_reported_even_if_an_amount_came_back() -> None:
    extraction, _ = run(answer(is_payment_receipt=False, amount="250"))

    assert extraction.status is ExtractionStatus.NOT_A_RECEIPT
    assert extraction.amount_minor is None
    assert extraction.raw_json is not None


@pytest.mark.parametrize("amount", ["1,250.00", "S/ 40", "40.555", "-5", "", "00012345.123"])
def test_non_canonical_amount_is_unreadable(amount: str) -> None:
    extraction, _ = run(answer(amount=amount))

    assert extraction.status is ExtractionStatus.UNREADABLE
    assert extraction.amount_minor is None
    assert extraction.error is not None
    assert repr(amount) in extraction.error


@pytest.mark.parametrize("amount", ["00012345", "00012345.00"])
def test_operation_number_reported_as_the_amount_is_unreadable(amount: str) -> None:
    extraction, _ = run(answer(amount=amount, operation_number="00012345"))

    assert extraction.status is ExtractionStatus.UNREADABLE
    assert extraction.amount_minor is None
    assert extraction.error is not None
    assert "operation number" in extraction.error


@pytest.mark.parametrize("currency", [None, "soles", "S/", "pen"])
def test_amount_without_a_valid_currency_is_unreadable(currency: str | None) -> None:
    extraction, _ = run(answer(currency=currency))

    assert extraction.status is ExtractionStatus.UNREADABLE
    assert extraction.amount_minor is None
    assert extraction.error is not None


def test_refusal_is_an_explicit_result() -> None:
    reply = message(
        None,
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": None},
        output_tokens=0,
    )

    extraction, _ = run(reply)

    assert extraction.status is ExtractionStatus.REFUSED
    assert extraction.amount_minor is None
    assert extraction.error is not None
    assert "cyber" in extraction.error
    assert extraction.usage == TokenUsage(input_tokens=1830, output_tokens=0)


def test_refusal_without_details_is_still_refused() -> None:
    extraction, _ = run(message(None, stop_reason="refusal"))

    assert extraction.status is ExtractionStatus.REFUSED


def test_truncated_answer_fails_instead_of_parsing_partial_json() -> None:
    extraction, _ = run(message('{"is_payment_receipt": true, "amo', stop_reason="max_tokens"))

    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.error is not None
    assert "max_tokens" in extraction.error


def test_invalid_json_fails_and_keeps_the_raw_output() -> None:
    extraction, _ = run(message("The amount is S/ 250"))

    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.raw_json == "The amount is S/ 250"


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        json.dumps(receipt_payload(amount=250)),
        json.dumps(receipt_payload(is_payment_receipt="yes")),
        json.dumps({"amount": "250"}),
    ],
    ids=["array", "numeric-amount", "string-flag", "missing-fields"],
)
def test_output_that_breaks_the_schema_fails(text: str) -> None:
    extraction, _ = run(message(text))

    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.amount_minor is None


def test_reply_without_text_fails() -> None:
    extraction, _ = run(message(None))

    assert extraction.status is ExtractionStatus.FAILED


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (status_error(anthropic.BadRequestError, 400), "400"),
        (status_error(anthropic.AuthenticationError, 401), "credentials"),
        (status_error(anthropic.PermissionDeniedError, 403), "credentials"),
        (status_error(anthropic.NotFoundError, 404), OPUS),
        (status_error(anthropic.UnprocessableEntityError, 422), "422"),
        (status_error(anthropic.RateLimitError, 429), "retryable"),
        (status_error(anthropic.InternalServerError, 503), "retryable"),
        (status_error(anthropic.APIStatusError, 409), "409"),
        (anthropic.APITimeoutError(FAKE_REQUEST), "timed out"),
        (anthropic.APIConnectionError(request=FAKE_REQUEST), "reach"),
        (
            anthropic.APIResponseValidationError(
                status_error(anthropic.APIStatusError, 200).response, None
            ),
            "unexpected",
        ),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else None,
)
def test_sdk_errors_become_failed_results_without_raising(error: Exception, hint: str) -> None:
    extraction, messages = run(error)

    assert len(messages.calls) == 1
    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.amount_minor is None
    assert extraction.error is not None
    assert hint in extraction.error
