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
from datetime import date, datetime
from functools import partial
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import anthropic

from payments_bot.adapters.extraction.image_format import SUPPORTED_FORMATS, detect_media_type
from payments_bot.adapters.extraction.receipt_prompt import (
    CURRENCIES,
    RECEIPT_SCHEMA,
    SYSTEM_PROMPT,
    USER_INSTRUCTION,
)
from payments_bot.domain.errors import InvalidAmountError
from payments_bot.domain.money import MAX_AMOUNT_MINOR, parse_amount
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
"""Receipts print Lima wall-clock time without an offset."""

MAX_PRINTED_NUMBER_DIGITS = 32
"""Longest operation number or security code kept. Real ones are far shorter; a longer digit
string is noise, and it could never equal an amount below `MAX_AMOUNT_MINOR` anyway."""

_RETRYABLE_STATUS_ERRORS = (
    anthropic.InternalServerError,
    anthropic.ServiceUnavailableError,
    anthropic.DeadlineExceededError,
    anthropic.OverloadedError,
)
"""5xx answers worth retrying later, 529 "overloaded" included (it is not an
`InternalServerError` in the SDK)."""

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
        except _RETRYABLE_STATUS_ERRORS as exc:
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
        """Turn a schema-valid answer into a status. Only a high-confidence amount that passes
        every check is `ok`; a doubtful candidate is `needs_confirmation`; a number that cannot
        be the amount is `unreadable`, so no candidate is shown for the user to rubber-stamp."""
        reference = _digits(answer.operation_number)
        paid_on, paid_at = _receipt_moment(answer.paid_at)
        confidence = Confidence(answer.confidence)
        result = partial(
            Extraction,
            model=self._model,
            reference=reference,
            paid_on=paid_on,
            paid_at=paid_at,
            provider=_clean(answer.provider),
            confidence=confidence,
            raw_json=raw_json,
            usage=usage,
        )
        unreadable = partial(result, status=ExtractionStatus.UNREADABLE)
        if not answer.is_payment_receipt:
            return result(status=ExtractionStatus.NOT_A_RECEIPT)
        if answer.amount is None:
            return unreadable(error="no legible amount on the receipt")
        shown = _excerpt(answer.amount)
        try:
            amount_minor = parse_amount(answer.amount)
        except InvalidAmountError:
            return unreadable(error=f"amount {shown} is not a canonical decimal amount")
        if amount_minor == 0:
            return unreadable(error=f"amount {shown} is zero")
        if amount_minor > MAX_AMOUNT_MINOR:
            return unreadable(error=f"amount {shown} is above the largest amount the ledger takes")
        if _spells(amount_minor, reference):
            return unreadable(error=f"amount {shown} repeats the operation number's digits")
        currency = _clean(answer.currency)
        if currency not in CURRENCIES:
            return unreadable(
                error=f"currency {answer.currency!r} is not one of {', '.join(CURRENCIES)}"
            )
        candidate = partial(result, amount_minor=amount_minor, currency=currency)
        if _spells(amount_minor, _digits(answer.security_code)):
            return candidate(
                status=ExtractionStatus.NEEDS_CONFIRMATION,
                error=f"amount {shown} repeats the security code's digits; confirm it",
            )
        if confidence is not Confidence.HIGH:
            return candidate(
                status=ExtractionStatus.NEEDS_CONFIRMATION,
                error=f"model confidence is {confidence}; confirm the amount",
            )
        return candidate(status=ExtractionStatus.OK)

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
    security_code: str | None
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
        if not self.model or any(char.isspace() for char in self.model):
            raise ExtractorConfigError(
                f"model {self.model!r} must be a Bedrock model id without spaces, e.g. "
                f"{DEFAULT_MODEL!r}"
            )
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


BASE_URL_ENV = "ANTHROPIC_BEDROCK_MANTLE_BASE_URL"
"""Replaces the regional Bedrock endpoint when set, even to an empty string."""

BEARER_TOKEN_ENVS = ("AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY")
"""Switch auth from SigV4 to a bearer token when set (first one wins), unless a profile is."""


@dataclass(frozen=True, slots=True)
class BedrockEndpoint:
    """Where requests go and how they are authenticated. Holds variable names, never secrets."""

    base_url: str
    """The endpoint, with any user, password or query string masked."""
    base_url_from: str | None
    """The variable that replaced the regional endpoint, or None."""
    bearer_token_from: str | None
    """The variable holding the bearer token; None means requests are signed with SigV4."""
    profile: str | None

    def describe(self) -> str:
        source = f"from {self.base_url_from}" if self.base_url_from else "regional default"
        if self.bearer_token_from is not None:
            auth = f"bearer token from {self.bearer_token_from}"
        elif self.profile is not None:
            auth = f"SigV4 with AWS profile {self.profile}"
        else:
            auth = "SigV4 with the default AWS credential chain"
        return f"endpoint {self.base_url or '(empty)'} ({source}); auth: {auth}"


def resolve_endpoint(settings: BedrockSettings, env: Mapping[str, str]) -> BedrockEndpoint:
    """The endpoint and auth mode the client from `build_bedrock_extractor` will use.

    Mirrors the precedence of `anthropic.AnthropicBedrockMantle` (anthropic 2.3.0), which reads
    `BASE_URL_ENV` and `BEARER_TOKEN_ENVS` from the process environment by itself: a profile
    means SigV4; otherwise a bearer-token variable, even an empty one, means a bearer token;
    otherwise SigV4 with the default AWS credential chain. A test compares this with the real
    client so an SDK upgrade cannot silently change the answer. Pass `os.environ` as `env`.
    """
    base_url = env.get(BASE_URL_ENV)
    base_url_from = BASE_URL_ENV if base_url is not None else None
    if base_url is None:
        base_url = f"https://bedrock-mantle.{settings.region}.api.aws/anthropic"
    bearer_token_from = None
    if settings.profile is None:
        bearer_token_from = next((name for name in BEARER_TOKEN_ENVS if name in env), None)
    return BedrockEndpoint(
        base_url=_mask_url_secrets(base_url),
        base_url_from=base_url_from,
        bearer_token_from=bearer_token_from,
        profile=settings.profile,
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


def _mask_url_secrets(url: str) -> str:
    """Hide credentials a URL may carry (user, password, query string) before printing it."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unparsable URL)"
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, netloc, parts.path, "***" if parts.query else "", ""))


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


def _excerpt(text: str, limit: int = 40) -> str:
    return repr(text) if len(text) <= limit else f"{text[:limit]!r}... ({len(text)} characters)"


def _digits(text: str | None) -> str | None:
    """The ASCII digits of a printed number: "N° 0001-2345" -> "00012345".

    None when there are no digits or more than `MAX_PRINTED_NUMBER_DIGITS`. Other digit
    scripts (superscripts, Arabic-Indic) are dropped, never converted. Never raises.
    """
    if text is None:
        return None
    digits = "".join(char for char in text if char in "0123456789")
    if not digits or len(digits) > MAX_PRINTED_NUMBER_DIGITS:
        return None
    return digits


def _spells(amount_minor: int, digits: str | None) -> bool:
    """Whether the amount's digits are `digits` (leading zeros ignored), wherever the decimal
    point fell: operation number 00012345 matches S/ 12345, S/ 12345.00, S/ 1234.50 and
    S/ 123.45. Compares text, so no printed number is ever converted to an integer."""
    if digits is None or not digits.lstrip("0"):
        return False
    spellings = {str(amount_minor)}
    for shift in (10, 100):
        if amount_minor % shift == 0:
            spellings.add(str(amount_minor // shift))
    return digits.lstrip("0") in spellings


def _receipt_moment(text: str | None) -> tuple[date | None, datetime | None]:
    """The printed payment date and, when printed, its time, as Lima local time.

    The receipt shows Lima wall-clock time, so any UTC offset or "Z" in the answer was added by
    the model and is dropped, not applied. A date alone stays a date: no midnight is invented.
    Unparsable text gives (None, None).
    """
    if text is None or not text.strip():
        return None, None
    text = text.strip()
    try:
        return date.fromisoformat(text), None
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None, None
    local = moment.replace(tzinfo=RECEIPT_TIMEZONE)
    return local.date(), local


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
