"""Start-up warm-up: make the first AWS call of each kind now, so the first live lookup is not slow.

The first call to DynamoDB, the Knowledge Base and Bedrock each spends 1 to 4 s on credentials and a new
TLS connection. Every step is optional: a failure is logged and the app carries on.
"""
import asyncio
import json
import logging
import time

from copilot.container import Container

log = logging.getLogger(__name__)


async def _timed(name: str, work) -> None:
    started = time.perf_counter()
    try:
        await work
        log.info("Warm-up %s: %d ms", name, (time.perf_counter() - started) * 1000)
    except Exception as error:
        log.warning("Warm-up %s skipped: %s", name, type(error).__name__)


async def warm_up(c: Container) -> None:
    """Runs the steps together. Needs no customer data and writes nothing."""
    settings = c.settings
    steps = [_timed("data", c.repo.list_packs()), _timed("knowledge", c.retriever.warm_up())]
    if settings.bedrock_inference_profile_arn_fast:
        steps.append(
            _timed(
                "bedrock",
                c.bedrock.complete_json(
                    settings.bedrock_inference_profile_arn_fast, "Reply with JSON only.",
                    json.dumps({"reply": "ok"}), max_tokens=10,
                ),
            )
        )
    await asyncio.gather(*steps)
