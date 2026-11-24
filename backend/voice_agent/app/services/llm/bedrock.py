"""Amazon Bedrock Runtime client (Converse API) for the voice conversation.

* `modelId` is the configured inference profile ARN, passed through unchanged. There is no fallback to
  any other model or provider: a failure is reported as an LlmError and the caller decides.
* System instructions go in the Converse `system` field, never in `messages`.
* boto3 is synchronous, so each request runs in a worker thread (`asyncio.to_thread`) and is bounded by
  both botocore's own connect/read timeouts and an asyncio timeout. Retries are disabled: for a live
  call a fast, reported failure beats a slow retry.
* Credentials come from the standard AWS chain (env, shared config/SSO, IAM role). Nothing is read or
  stored here.

IAM: the caller needs `bedrock:InvokeModel` on the inference profile ARN and on the foundation model(s)
(for a cross-region profile: in every region it can route to) that the profile points at.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any

from app.services.llm.base import (
    LlmAccessDeniedError,
    LlmConfigError,
    LlmEmptyResponseError,
    LlmError,
    LlmMessage,
    LlmResult,
    LlmServiceError,
    LlmThrottledError,
    LlmTimeoutError,
)

logger = logging.getLogger(__name__)

_THROTTLE_CODES = frozenset({"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"})
_ACCESS_CODES = frozenset(
    {"AccessDeniedException", "UnrecognizedClientException", "ExpiredTokenException", "InvalidSignatureException"}
)
_TIMEOUT_CODES = frozenset({"ModelTimeoutException", "RequestTimeout", "RequestTimeoutException"})


def region_from_arn(arn: str) -> str:
    """Region field of a Bedrock ARN (arn:aws:bedrock:<region>:<account>:...), or '' if not present."""
    parts = arn.split(":")
    return parts[3] if len(parts) >= 6 and parts[0] == "arn" and parts[2] == "bedrock" else ""


def validate_inference_profile_arn(arn: str) -> None:
    """Fail fast on an obviously wrong ARN. Never echoes the value."""
    parts = arn.split(":")
    if len(parts) < 6 or parts[0] != "arn" or parts[2] != "bedrock" or not parts[3]:
        raise LlmConfigError("BEDROCK_INFERENCE_PROFILE_ARN is not a Bedrock ARN (arn:aws:bedrock:<region>:...)")


def build_converse_messages(messages: Sequence[LlmMessage]) -> list[dict[str, Any]]:
    """Map conversation history to Converse messages.

    Converse requires the first message to be `user` and roles to alternate, so: empty messages are
    dropped, leading assistant messages (the greeting) are dropped (the system prompt already tells
    the model it was said), and consecutive messages of one role are merged (e.g. a customer who spoke
    again before the assistant answered). The last message must be from the user.
    """
    merged: list[tuple[str, str]] = []
    for message in messages:
        text = message.content.strip()
        if not text:
            continue
        if not merged and message.role == "assistant":
            continue
        if merged and merged[-1][0] == message.role:
            merged[-1] = (message.role, f"{merged[-1][1]}\n{text}")
        else:
            merged.append((message.role, text))
    if not merged or merged[-1][0] != "user":
        raise LlmConfigError("conversation must end with a customer message")
    return [{"role": role, "content": [{"text": text}]} for role, text in merged]


def _extract_text(response: dict[str, Any]) -> str:
    try:
        blocks = response["output"]["message"]["content"]
    except (KeyError, TypeError):
        raise LlmEmptyResponseError("Bedrock response has no message content") from None
    text = "".join(b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)).strip()
    if not text:
        raise LlmEmptyResponseError("Bedrock returned an empty reply")
    return text


def _map_exception(exc: BaseException) -> LlmError:
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        ConnectionClosedError,
        ConnectTimeoutError,
        EndpointConnectionError,
        NoCredentialsError,
        NoRegionError,
        PartialCredentialsError,
        ReadTimeoutError,
    )

    if isinstance(exc, LlmError):
        return exc
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ReadTimeoutError, ConnectTimeoutError)):
        return LlmTimeoutError("Bedrock request timed out")
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return LlmConfigError("no usable AWS credentials were found")
    if isinstance(exc, NoRegionError):
        return LlmConfigError("no AWS region is configured")
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _THROTTLE_CODES:
            return LlmThrottledError("Bedrock throttled the request")
        if code in _ACCESS_CODES:
            return LlmAccessDeniedError(f"Bedrock denied access ({code})")
        if code in _TIMEOUT_CODES:
            return LlmTimeoutError("Bedrock model timed out")
        return LlmServiceError(f"Bedrock error ({code or 'unknown'})")
    if isinstance(exc, (EndpointConnectionError, ConnectionClosedError)):
        return LlmServiceError("could not reach Bedrock")
    if isinstance(exc, BotoCoreError):
        return LlmServiceError(f"Bedrock client error ({type(exc).__name__})")
    return LlmServiceError(f"unexpected Bedrock failure ({type(exc).__name__})")


class BedrockConverseClient:
    """`LlmClient` backed by Bedrock Runtime `converse`."""

    def __init__(
        self,
        inference_profile_arn: str,
        region: str = "",
        *,
        max_tokens: int = 200,
        temperature: float = 0.3,
        timeout_s: float = 12.0,
        client: Any | None = None,
    ) -> None:
        validate_inference_profile_arn(inference_profile_arn)
        self._model_id = inference_profile_arn
        self._region = region or region_from_arn(inference_profile_arn)
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._timeout_s = timeout_s
        self._client = client  # a boto3 "bedrock-runtime" client; created lazily when not injected

    @property
    def region(self) -> str:
        return self._region

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self._region,
                config=Config(
                    connect_timeout=min(self._timeout_s, 5.0),
                    read_timeout=self._timeout_s,
                    # botocore counts RETRIES here: 0 = a single attempt (a retry would blow the live-call budget)
                    retries={"max_attempts": 0, "mode": "standard"},
                ),
            )
        return self._client

    def _converse(self, system: str, converse_messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self._get_client().converse(
            modelId=self._model_id,
            messages=converse_messages,
            system=[{"text": system}],
            inferenceConfig={"maxTokens": self._max_tokens, "temperature": self._temperature},
        )

    async def generate(self, *, system: str, messages: Sequence[LlmMessage]) -> LlmResult:
        converse_messages = build_converse_messages(messages)
        started = time.monotonic()
        try:
            # +1 s over the botocore read timeout so the library's own timeout normally fires first.
            response = await asyncio.wait_for(
                asyncio.to_thread(self._converse, system, converse_messages), self._timeout_s + 1.0
            )
            text = _extract_text(response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _map_exception(exc) from None
        usage = response.get("usage") or {}
        return LlmResult(
            text=text,
            latency_ms=(time.monotonic() - started) * 1000,
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
            stop_reason=response.get("stopReason"),
        )
