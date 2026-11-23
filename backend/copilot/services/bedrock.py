"""Amazon Bedrock access through the Converse API."""

import asyncio
import logging

from botocore.config import Config

from config.settings import Settings
from copilot.utils.aws import aws_session
from copilot.utils.helpers import extract_json

log = logging.getLogger(__name__)


class BedrockClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None  # created on first use, so the app starts without AWS config

    def _get_client(self):
        if self._client is None:
            self._client = aws_session(self._settings).client(
                "bedrock-runtime",
                config=Config(read_timeout=25, retries={"max_attempts": 2}),
            )
        return self._client

    def _converse(
        self, model_id: str, system: str, user: str, max_tokens: int, temperature: float
    ) -> str:
        reply = self._get_client().converse(
            modelId=model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        return reply["output"]["message"]["content"][0]["text"]

    async def complete_json(
        self,
        model_id: str,
        system: str,
        user: str,
        max_tokens: int = 500,
        temperature: float = 0.1,
    ) -> dict:
        if not model_id:
            raise RuntimeError("Bedrock inference profile ARN is not set in config/.env")
        text = await asyncio.to_thread(
            self._converse, model_id, system, user, max_tokens, temperature
        )
        return extract_json(text)
