"""LLM provider interface for the voice conversation, and its error types.

Messages contain customer speech, so `LlmMessage.content` is excluded from repr. Error messages never
contain message text, credentials or the model identifier.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class LlmMessage:
    role: Literal["user", "assistant"]
    content: str = field(repr=False)


@dataclass(frozen=True)
class LlmResult:
    text: str = field(repr=False)
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None


class LlmError(Exception):
    """Base class. `code` is a short stable label for logs and metrics."""

    code = "llm_error"


class LlmConfigError(LlmError):
    """Local misconfiguration (missing ARN/region/credentials). No request reached the provider."""

    code = "llm_config_error"


class LlmThrottledError(LlmError):
    code = "llm_throttled"


class LlmTimeoutError(LlmError):
    code = "llm_timeout"


class LlmAccessDeniedError(LlmError):
    code = "llm_access_denied"


class LlmServiceError(LlmError):
    code = "llm_service_error"


class LlmEmptyResponseError(LlmError):
    code = "llm_empty_response"


@runtime_checkable
class LlmClient(Protocol):
    async def generate(self, *, system: str, messages: Sequence[LlmMessage]) -> LlmResult: ...
