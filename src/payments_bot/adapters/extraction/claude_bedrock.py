"""Claude vision on Amazon Bedrock as an `AmountExtractor`.

The real client is the official SDK's Bedrock Mantle client (`anthropic.AnthropicBedrockMantle`,
installed with the `anthropic[bedrock]` extra); it is injected so tests can pass a fake.
Structured outputs (`output_config.format`) constrain the answer to `RECEIPT_SCHEMA`, and the
amount is validated again with the domain's `parse_amount` before it is trusted.

SDK errors become `failed` results. Configuration problems outside the SDK's typed errors (for
example AWS credentials that cannot be resolved) propagate on purpose, so a misconfigured
deployment fails loudly instead of marking every receipt as failed.
"""

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from functools import partial
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import anthropic

from payments_bot.adapters.extraction.image_format import SUPPORTED_FORMATS, detect_media_type
from payments_bot.adapters.extraction.receipt_prompt import (
    RECEIPT_SCHEMA,
    SYSTEM_PROMPT,
    USER_INSTRUCTION,
)
from payments_bot.domain.errors import InvalidAmountError, InvalidCurrencyError
from payments_bot.domain.money import MINOR_UNITS_PER_MAJOR, parse_amount, validate_currency
from payments_bot.ports.amount_extractor import (
    Confidence,
    Extraction,
    ExtractionStatus,
    TokenUsage,
)

DEFAULT_MODEL = "anthropic.claude-opus-5-5"
DEFAULT_EFFORT = "medium"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS = 16000
"""Claude Opus 5.5 always thinks, and thinking counts toward this limit."""

RECEIPT_TIMEZONE = ZoneInfo("America/Lima")
"""Receipts print local time without an offset."""

DEFAULT_CLIENT_FACTORY = anthropic.AnthropicBedrockMantle


class ExtractorConfigError(ValueError):
    """The extractor configuration is missing or invalid."""


class _Messages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class MessagesClient(Protocol):
    """The slice of the `anthropic` client this adapter uses."""

    @property
    def messages(self) -> _Messages: ...


class ClaudeBedrockExtractor:
    def __init__(
        self,
        client: MessagesClient,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        _check_effort(effort)
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    def extract(self, image: bytes) -> Extraction:
        media_type = detect_media_type(image)
        if media_type is None:
            return self._failed(f"unsupported image format; expected {SUPPORTED_FORMATS}")
        try:
            response = self._client.messages.create(**self._request(image, media_type))
        except anthropic.NotFoundError as exc:
            return self._failed(f"model {self._model!r} not found in this region: {exc.message}")
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            return self._failed(
                f"access denied ({exc.status_code}); check AWS credentials and Bedrock model "
                f"access: {exc.message}"
            )
        except anthropic.RateLimitError as exc:
            return self._failed(f"rate limited (retryable): {exc.message}")
        except anthropic.InternalServerError as exc:
            return self._failed(f"provider error {exc.status_code} (retryable): {exc.message}")
        except anthropic.APIStatusError as exc:
            return self._failed(f"request rejected ({exc.status_code}): {exc.message}")
        except anthropic.APITimeoutError:
            return self._failed("request timed out (retryable)")
        except anthropic.APIConnectionError:
            return self._failed("could not reach Amazon Bedrock (retryable)")
        except anthropic.APIError as exc:
            return self._failed(f"unexpected provider response: {exc.message}")
        return self._read(response)

    def _request(self, image: bytes, media_type: str) -> dict[str, Any]:
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image).decode("ascii"),
            },
        }
        return {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": SYSTEM_PROMPT,
            "output_config": {
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": RECEIPT_SCHEMA},
            },
            "messages": [
                {
                    "role": "user",
                    "content": [image_block, {"type": "text", "text": USER_INSTRUCTION}],
                }
            ],
        }

    def _read(self, response: Any) -> Extraction:
        usage = _usage(response)
        if response.stop_reason == "refusal":
            return Extraction(
                status=ExtractionStatus.REFUSED,
                model=self._model,
                usage=usage,
                error=_refusal_detail(response),
            )
        if response.stop_reason != "end_turn":
            return self._failed(
                f"answer stopped early (stop_reason={response.stop_reason})", usage=usage
            )
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            return self._failed("answer has no text block", usage=usage)
        try:
            answer = _ReceiptAnswer.parse(text)
        except _SchemaMismatchError as exc:
            return self._failed(str(exc), usage=usage, raw_json=text)
        return self._interpret(answer, raw_json=text, usage=usage)

    def _interpret(
        self, answer: "_ReceiptAnswer", *, raw_json: str, usage: TokenUsage | None
    ) -> Extraction:
        reference = _clean(answer.operation_number)
        result = partial(
            Extraction,
            model=self._model,
            reference=reference,
            paid_at=_receipt_time(answer.paid_at),
            provider=_clean(answer.provider),
            confidence=Confidence(answer.confidence),
            raw_json=raw_json,
            usage=usage,
        )
        unreadable = partial(result, status=ExtractionStatus.UNREADABLE)
        if not answer.is_payment_receipt:
            return result(status=ExtractionStatus.NOT_A_RECEIPT)
        if answer.amount is None:
            return unreadable(error="no legible amount on the receipt")
        try:
            amount_minor = parse_amount(answer.amount)
        except InvalidAmountError:
            return unreadable(error=f"amount {answer.amount!r} is not a canonical decimal")
        if _is_operation_number(amount_minor, reference):
            return unreadable(error=f"amount {answer.amount!r} is the operation number")
        currency = _clean(answer.currency)
        try:
            validate_currency(currency or "")
        except InvalidCurrencyError:
            return unreadable(error=f"currency {answer.currency!r} is not an ISO 4217 code")
        return result(status=ExtractionStatus.OK, amount_minor=amount_minor, currency=currency)

    def _failed(
        self, error: str, *, usage: TokenUsage | None = None, raw_json: str | None = None
    ) -> Extraction:
        return Extraction(
            status=ExtractionStatus.FAILED,
            model=self._model,
            error=error,
            usage=usage,
            raw_json=raw_json,
        )


class _SchemaMismatchError(Exception):
    """The model's answer does not have the shape `RECEIPT_SCHEMA` promises."""


@dataclass(frozen=True, slots=True)
class _ReceiptAnswer:
    """The model's answer, checked against the schema before any field is trusted."""

    is_payment_receipt: bool
    amount: str | None
    currency: str | None
    operation_number: str | None
    paid_at: str | None
    provider: str | None
    confidence: str

    @classmethod
    def parse(cls, text: str) -> "_ReceiptAnswer":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _SchemaMismatchError("answer is not valid JSON") from exc
        expected = {field.name for field in fields(cls)}
        if not isinstance(data, dict) or set(data) != expected:
            raise _SchemaMismatchError("answer does not have the receipt schema's fields")
        if not isinstance(data["is_payment_receipt"], bool):
            raise _SchemaMismatchError("is_payment_receipt is not a boolean")
        optional_text = expected - {"is_payment_receipt", "confidence"}
        if any(data[key] is not None and not isinstance(data[key], str) for key in optional_text):
            raise _SchemaMismatchError("a text field is neither a string nor null")
        if data["confidence"] not in {level.value for level in Confidence}:
            raise _SchemaMismatchError(f"unknown confidence {data['confidence']!r}")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class BedrockSettings:
    region: str
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    profile: str | None = None
    """AWS profile to sign requests with; None uses the default AWS credential chain."""

    def __post_init__(self) -> None:
        _check_effort(self.effort)
        if self.model.startswith("claude-"):
            raise ExtractorConfigError(
                f"model {self.model!r} is a first-party id; Bedrock model ids take the "
                f"'anthropic.' prefix, e.g. {DEFAULT_MODEL!r}"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "BedrockSettings":
        """Read `AWS_REGION` (required), `EXTRACTOR_MODEL`, `EXTRACTOR_EFFORT`, `AWS_PROFILE`."""
        region = _setting(env, "AWS_REGION")
        if region is None:
            raise ExtractorConfigError("AWS_REGION is required: the Bedrock region to use")
        return cls(
            region=region,
            model=_setting(env, "EXTRACTOR_MODEL") or DEFAULT_MODEL,
            effort=_setting(env, "EXTRACTOR_EFFORT") or DEFAULT_EFFORT,
            profile=_setting(env, "AWS_PROFILE"),
        )


def build_bedrock_extractor(
    settings: BedrockSettings,
    *,
    client_factory: Callable[..., MessagesClient] = DEFAULT_CLIENT_FACTORY,
) -> ClaudeBedrockExtractor:
    """Create the Bedrock client for `settings` and wrap it in an extractor."""
    options = {"aws_region": settings.region}
    if settings.profile is not None:
        options["aws_profile"] = settings.profile
    return ClaudeBedrockExtractor(
        client_factory(**options), model=settings.model, effort=settings.effort
    )


def _check_effort(effort: str) -> None:
    if effort not in EFFORT_LEVELS:
        raise ExtractorConfigError(
            f"unknown effort {effort!r} (EXTRACTOR_EFFORT); expected one of "
            f"{', '.join(EFFORT_LEVELS)}"
        )


def _setting(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _clean(text: str | None) -> str | None:
    if text is None or not text.strip():
        return None
    return text.strip()


def _is_operation_number(amount_minor: int, reference: str | None) -> bool:
    """Guard against the most common misread: the operation number taken as the amount."""
    if reference is None or not reference.isdigit():
        return False
    return amount_minor == int(reference) * MINOR_UNITS_PER_MAJOR


def _receipt_time(text: str | None) -> datetime | None:
    """Parse an ISO 8601 receipt time; a time without an offset is Lima local time."""
    if text is None or not text.strip():
        return None
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if moment.utcoffset() is None:
        return moment.replace(tzinfo=RECEIPT_TIMEZONE)
    return moment


def _usage(response: Any) -> TokenUsage | None:
    if response.usage is None:
        return None
    return TokenUsage(response.usage.input_tokens, response.usage.output_tokens)


def _refusal_detail(response: Any) -> str:
    detail = "the model declined to read this image"
    stop_details = response.stop_details
    if stop_details is None:
        return detail
    if stop_details.category:
        detail += f" (category: {stop_details.category})"
    if stop_details.explanation:
        detail += f": {stop_details.explanation}"
    return detail
