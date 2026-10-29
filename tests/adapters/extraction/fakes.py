"""Offline stand-ins for the Anthropic SDK client. Nothing here reaches the network.

Replies are real `anthropic.types.Message` objects validated from API-shaped JSON, so the
adapter is exercised against the same attribute shapes the SDK returns.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2
from anthropic.types import Message

OPUS = "anthropic.claude-opus-5-5"
HAIKU = "anthropic.claude-haiku-5-5"

JPEG_BYTES = b"\xff\xd8\xff\xe0" + bytes(32)
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(32)
GIF_BYTES = b"GIF89a" + bytes(32)
WEBP_BYTES = b"RIFF" + (32).to_bytes(4, "little") + b"WEBPVP8 " + bytes(32)

FAKE_REQUEST = httpx2.Request("POST", "https://bedrock-mantle.invalid/anthropic/v1/messages")


def receipt_payload(**overrides: Any) -> dict[str, Any]:
    """A well-formed model answer for a Yape receipt; keyword overrides replace fields."""
    payload: dict[str, Any] = {
        "is_payment_receipt": True,
        "amount": "250",
        "currency": "PEN",
        "operation_number": "00012345",
        "paid_at": "2026-01-15T09:30:00",
        "provider": "Yape",
        "confidence": "high",
    }
    return payload | overrides


def message(
    text: str | None,
    *,
    stop_reason: str = "end_turn",
    stop_details: dict[str, Any] | None = None,
    input_tokens: int = 1830,
    output_tokens: int = 95,
    model: str = OPUS,
) -> Message:
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if text is not None:
        content.append({"type": "text", "text": text})
    return Message.model_validate(
        {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    )


def answer(**overrides: Any) -> Message:
    """A successful reply whose text is `receipt_payload(**overrides)` as JSON."""
    return message(json.dumps(receipt_payload(**overrides)))


def status_error(error_class: type, status_code: int) -> Exception:
    response = httpx2.Response(status_code, request=FAKE_REQUEST)
    return error_class("provider said no", response=response, body=None)


@dataclass
class FakeMessages:
    reply: Message | Exception
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@dataclass
class FakeClient:
    messages: FakeMessages


def fake_client(reply: Message | Exception) -> FakeClient:
    return FakeClient(FakeMessages(reply))
