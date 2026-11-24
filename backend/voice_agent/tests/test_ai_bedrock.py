"""AI voice conversation: Amazon Bedrock Converse client. A fake boto3 client is injected, so no AWS call
is made and no credentials are needed."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, NoCredentialsError, ReadTimeoutError

from app.services.llm.base import (
    LlmAccessDeniedError,
    LlmConfigError,
    LlmEmptyResponseError,
    LlmMessage,
    LlmServiceError,
    LlmThrottledError,
    LlmTimeoutError,
)
from app.services.llm.bedrock import BedrockConverseClient, build_converse_messages, region_from_arn

ARN = "arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/abc123xyz"


def user(text: str) -> LlmMessage:
    return LlmMessage("user", text)


def assistant(text: str) -> LlmMessage:
    return LlmMessage("assistant", text)


class FakeBedrock:
    def __init__(self, response=None, error: BaseException | None = None) -> None:
        self.response = response if response is not None else {
            "output": {"message": {"role": "assistant", "content": [{"text": "  Sure, I can help.  "}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 12, "outputTokens": 5},
        }
        self.error = error
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"secret detail {ARN}"}}, "Converse")


def make(fake: FakeBedrock, **kw) -> BedrockConverseClient:
    return BedrockConverseClient(ARN, kw.pop("region", ""), client=fake, **kw)


async def test_inference_profile_arn_is_the_model_id_and_system_is_separate() -> None:
    fake = FakeBedrock()
    result = await make(fake, max_tokens=150, temperature=0.2).generate(
        system="You are a bot.", messages=[user("Hello")]
    )
    (call,) = fake.calls
    assert call["modelId"] == ARN
    assert call["system"] == [{"text": "You are a bot."}]
    assert call["messages"] == [{"role": "user", "content": [{"text": "Hello"}]}]
    assert call["inferenceConfig"] == {"maxTokens": 150, "temperature": 0.2}
    assert all("system" not in m["role"] for m in call["messages"])
    assert result.text == "Sure, I can help."
    assert (result.input_tokens, result.output_tokens, result.stop_reason) == (12, 5, "end_turn")


def test_history_maps_to_alternating_converse_messages() -> None:
    mapped = build_converse_messages(
        [
            assistant("Welcome to Grameenphone."),  # greeting: leading assistant message is dropped
            user("My internet"),
            user("is slow"),  # consecutive customer turns are merged
            assistant("Since when?"),
            user("Yesterday"),
        ]
    )
    assert mapped == [
        {"role": "user", "content": [{"text": "My internet\nis slow"}]},
        {"role": "assistant", "content": [{"text": "Since when?"}]},
        {"role": "user", "content": [{"text": "Yesterday"}]},
    ]


@pytest.mark.parametrize("history", [[], [assistant("Hi")], [user("Hi"), assistant("Hello")], [user("   ")]])
def test_history_must_end_with_a_customer_message(history) -> None:
    with pytest.raises(LlmConfigError):
        build_converse_messages(history)


def test_region_comes_from_the_arn_unless_configured() -> None:
    assert region_from_arn(ARN) == "ap-south-1"
    assert BedrockConverseClient(ARN, client=FakeBedrock()).region == "ap-south-1"
    assert BedrockConverseClient(ARN, "us-east-1", client=FakeBedrock()).region == "us-east-1"


@pytest.mark.parametrize("bad", ["", "not-an-arn", "arn:aws:s3:::bucket", "arn:aws:bedrock::123:x"])
def test_invalid_arn_is_rejected_without_echoing_it(bad: str) -> None:
    with pytest.raises(LlmConfigError) as info:
        BedrockConverseClient(bad, client=FakeBedrock())
    assert bad not in str(info.value) or bad == ""


@pytest.mark.parametrize(
    "response",
    [
        {"output": {"message": {"content": []}}},
        {"output": {"message": {"content": [{"text": "   "}]}}},
        {"output": {"message": {"content": [{"toolUse": {}}]}}},
        {"output": {}},
        {},
    ],
)
async def test_empty_responses_raise(response) -> None:
    with pytest.raises(LlmEmptyResponseError):
        await make(FakeBedrock(response=response)).generate(system="s", messages=[user("Hi")])


@pytest.mark.parametrize(
    "error, expected",
    [
        (client_error("ThrottlingException"), LlmThrottledError),
        (client_error("AccessDeniedException"), LlmAccessDeniedError),
        (client_error("ModelTimeoutException"), LlmTimeoutError),
        (client_error("ServiceUnavailableException"), LlmServiceError),
        (client_error("InternalServerException"), LlmServiceError),
        (client_error("ValidationException"), LlmServiceError),
        (ReadTimeoutError(endpoint_url="https://x"), LlmTimeoutError),
        (ConnectTimeoutError(endpoint_url="https://x"), LlmTimeoutError),
        (NoCredentialsError(), LlmConfigError),
        (RuntimeError("boom"), LlmServiceError),
    ],
)
async def test_provider_failures_map_to_llm_errors_and_never_leak_the_arn(error, expected) -> None:
    fake = FakeBedrock(error=error)
    with pytest.raises(expected) as info:
        await make(fake).generate(system="s", messages=[user("Hi")])
    assert ARN not in str(info.value)
    assert len(fake.calls) == 1  # no retry and no fallback to another model or provider
    assert fake.calls[0]["modelId"] == ARN


def test_default_boto3_client_has_no_retries_and_uses_the_arn_region() -> None:
    c = BedrockConverseClient(ARN, timeout_s=7)
    boto = c._get_client()  # constructing a client makes no network call
    assert boto.meta.region_name == "ap-south-1"
    assert boto.meta.config.read_timeout == 7
    assert boto.meta.config.retries["total_max_attempts"] == 1  # a single attempt, no retries
