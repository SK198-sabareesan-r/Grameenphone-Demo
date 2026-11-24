"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import conversation as conversation_routes
from app.api.routes import health, sarvam_config, vobiz, vobiz_config, vobiz_ws
from app.services.conversation.manager import ConversationManager, TtsClient
from app.services.llm.base import LlmClient, LlmConfigError
from app.services.sarvam.tts import SarvamTtsClient
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.integrations.vobiz.call_control import DuplicateCallGuard
from app.integrations.vobiz.errors import VobizError
from app.integrations.vobiz.webhooks import EventDeduper
from app.services.voice.audio_consumer import AudioSinkFactory
from app.services.voice.call_manager import CallStateManager
from app.publishers.pipeline import PipelineCallListener, PublisherSubscriber, TranscriptEventPipeline
from app.publishers.transcripts import LoggingTranscriptPublisher, TranscriptPublisher
from app.services.sarvam.errors import SarvamConfigError
from app.services.voice.dev_capture import build_dev_capture
from app.services.voice.sarvam_stt_sink import SarvamSttSinkFactory
from app.services.voice.media_stream_manager import VobizMediaStreamHandler
from app.services.voice.track_router import TrackRoutingSinkFactory
from app.services.sarvam.errors import SarvamError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    conversation = app.state.conversation
    if conversation is not None:  # stop LLM/TTS/playback work before the streams are closed
        await conversation.shutdown()
    # Graceful shutdown: close live streams so their audio consumers are cancelled and awaited.
    closed = await app.state.call_manager.close_all("shutdown")
    if closed:
        logger.info("vobiz_streams_closed_on_shutdown", extra={"streams": closed})
    # Streams are closed first so their final transcripts reach the pipeline, then it is drained.
    pipeline = app.state.transcript_pipeline
    await pipeline.shutdown(app.state.settings.transcript_drain_timeout_s + 1.0)
    tts = app.state.conversation_tts
    if tts is not None and hasattr(tts, "aclose"):
        await tts.aclose()


def _install_conversation(
    app: FastAPI,
    settings: Settings,
    stt_available: bool,
    llm: LlmClient | None,
    tts: TtsClient | None,
) -> None:
    """Validate the AI-conversation settings (fail fast, no secrets in errors) and attach the manager to
    the call manager (stream lifecycle) and the transcript pipeline (final transcripts, speech start).
    No provider connection is made here."""
    if not stt_available:
        raise LlmConfigError(
            "AI_CONVERSATION_ENABLED requires VOBIZ_SARVAM_STT_ENABLED=true (customer speech comes from STT)."
        )
    if llm is None:
        if not settings.bedrock_inference_profile_arn:
            raise LlmConfigError("AI_CONVERSATION_ENABLED requires BEDROCK_INFERENCE_PROFILE_ARN.")
        try:
            from app.services.llm.bedrock import BedrockConverseClient, region_from_arn
        except ImportError as exc:
            raise LlmConfigError("boto3 is not installed (pip install -r requirements.txt).") from exc
        llm = BedrockConverseClient(
            settings.bedrock_inference_profile_arn,
            settings.aws_region,
            max_tokens=settings.bedrock_max_tokens,
            temperature=settings.bedrock_temperature,
            timeout_s=settings.ai_llm_timeout_s,
        )
        arn_region = region_from_arn(settings.bedrock_inference_profile_arn)
        if settings.aws_region and arn_region and settings.aws_region != arn_region:
            logger.warning("ai_bedrock_region_mismatch", extra={"aws_region": settings.aws_region, "arn_region": arn_region})
        logger.info("ai_bedrock_configured", extra={"aws_region": llm.region, "max_tokens": settings.bedrock_max_tokens})
    if tts is None:
        if not settings.sarvam_api_key.get_secret_value():
            raise SarvamConfigError("AI_CONVERSATION_ENABLED requires SARVAM_API_KEY (Sarvam TTS).")
        tts = SarvamTtsClient(settings)
    app.state.conversation_tts = tts
    manager = app.state.conversation = ConversationManager(
        llm=llm,
        tts=tts,
        get_stream=app.state.call_manager.current_stream,
        history_max_messages=settings.ai_history_max_messages,
        llm_timeout_s=settings.ai_llm_timeout_s + 2.0,  # the client's own timeout normally fires first
        tts_timeout_s=settings.ai_tts_timeout_s + 2.0,
        barge_in_enabled=settings.ai_barge_in_enabled,
        barge_in_grace_ms=settings.ai_barge_in_grace_ms,
    )
    app.state.call_manager.add_listener(manager)  # added after PipelineCallListener: its session opens first
    app.state.transcript_pipeline.add_global_subscriber(manager, "conversation")
    logger.info(
        "ai_conversation_enabled",
        extra={
            "tts_model": settings.sarvam_tts_model, "tts_language": settings.sarvam_tts_language,
            "tts_sample_rate": settings.sarvam_tts_sample_rate, "barge_in": settings.ai_barge_in_enabled,
            "history_max_messages": settings.ai_history_max_messages,
        },
    )


def create_app(
    settings: Settings | None = None,
    *,
    audio_sink_factory: AudioSinkFactory | None = None,
    transcript_publisher: TranscriptPublisher | None = None,
    monitor_sink_factory: AudioSinkFactory | None = None,
    conversation_llm: LlmClient | None = None,
    conversation_tts: TtsClient | None = None,
) -> FastAPI:
    """Create the FastAPI app. Accepts settings for easy overriding in tests.

    `audio_sink_factory` registers the per-stream inbound-audio sink (M4: Sarvam realtime STT).
    None (the default, for now) means streams run without a consumer and say so explicitly.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=_lifespan)
    app.state.settings = settings
    app.include_router(health.router)
    app.include_router(sarvam_config.router)
    app.include_router(vobiz_config.router)
    # In-memory, per-process guards (no persistence).
    app.state.call_guard = DuplicateCallGuard()
    app.state.hangup_deduper = EventDeduper()
    app.include_router(vobiz.create_router(settings))
    # Per-call stream state, in memory, single process (see app/services/voice/call_manager.py).
    app.state.call_manager = CallStateManager()
    # Internal transcript event pipeline (M4.4): the STT sink publishes into it; subscribers hang off it.
    pipeline = app.state.transcript_pipeline = TranscriptEventPipeline.from_settings(settings)
    # live_stream_count: one stream ending must not idle-close a transcript session another stream still feeds.
    app.state.call_manager.add_listener(
        PipelineCallListener(pipeline, live_stream_count=app.state.call_manager.live_stream_count)
    )
    pipeline.add_global_subscriber(PublisherSubscriber(LoggingTranscriptPublisher()), "logging")
    if transcript_publisher is not None:
        pipeline.add_global_subscriber(PublisherSubscriber(transcript_publisher), "injected")
    # Development-only inbound capture (M4.2). Never installed unless APP_ENV=development and
    # VOBIZ_DEV_CAPTURE_ENABLED=true with a test destination; never overrides an explicit sink.
    app.state.dev_capture = None
    app.state.stt_sink_factory = None
    if audio_sink_factory is None:
        if settings.vobiz_sarvam_stt_enabled and settings.vobiz_dev_capture_enabled:
            raise SarvamConfigError(
                "Enable only one of VOBIZ_SARVAM_STT_ENABLED and VOBIZ_DEV_CAPTURE_ENABLED "
                "(both consume the same inbound audio)."
            )
        if settings.vobiz_sarvam_stt_enabled:
            # Validates SARVAM_API_KEY and the realtime STT settings now (fail fast, no secrets in errors).
            # No Sarvam connection is made until a Vobiz stream starts.
            audio_sink_factory = app.state.stt_sink_factory = SarvamSttSinkFactory(settings, pipeline)
            logger.info("vobiz_sarvam_stt_enabled", extra={
                **app.state.stt_sink_factory.config.query_params(),
                "finalize_timeout_s": settings.vobiz_sarvam_stt_finalize_timeout_s,
                "drain_timeout_s": settings.vobiz_audio_drain_timeout_s,
            })
        else:
            dev_capture = build_dev_capture(settings)
            if dev_capture is not None:
                app.state.dev_capture, audio_sink_factory = dev_capture
    # M5: monitored streams get one independent Sarvam session per recognized track. Built only when the
    # Sarvam STT feature is enabled (or injected for tests); legacy streams never use it.
    if monitor_sink_factory is None and app.state.stt_sink_factory is not None:
        stt_factory = app.state.stt_sink_factory
        monitor_sink_factory = TrackRoutingSinkFactory(
            stt_factory.create_for_track,
            queue_frames=settings.vobiz_ws_queue_frames,  # per track; same default as the per-stream queue
            drain_timeout_s=settings.vobiz_audio_drain_timeout_s,
            child_close_timeout_s=stt_factory.close_timeout_s,
        )
    app.state.monitor_sink_factory = monitor_sink_factory
    # Inbound AI voice conversation. Off by default; when on it reuses the STT pipeline above.
    app.state.conversation = None
    app.state.conversation_tts = None
    if settings.ai_conversation_enabled:
        stt_available = app.state.dev_capture is None and audio_sink_factory is not None
        _install_conversation(app, settings, stt_available, conversation_llm, conversation_tts)
    app.include_router(conversation_routes.router)
    app.state.stream_handler = VobizMediaStreamHandler(
        settings, app.state.call_manager, audio_sink_factory=audio_sink_factory,
        monitor_sink_factory=monitor_sink_factory,
    )
    app.include_router(vobiz_ws.create_ws_router(settings))

    @app.exception_handler(VobizError)
    async def _vobiz_error_handler(_: Request, exc: VobizError) -> JSONResponse:
        headers = {}
        retry_after = getattr(exc, "retry_after_s", None)
        if retry_after is not None:
            headers["Retry-After"] = str(int(retry_after))
        return JSONResponse(exc.to_dict(), status_code=exc.http_status, headers=headers)

    @app.exception_handler(SarvamError)
    async def _sarvam_error_handler(_: Request, exc: SarvamError) -> JSONResponse:
        headers = {}
        retry_after = getattr(exc, "retry_after_s", None)
        if retry_after is not None:
            headers["Retry-After"] = str(int(retry_after))
        return JSONResponse(exc.to_dict(), status_code=exc.http_status, headers=headers)

    logger.info("app_created", extra={"app": settings.app_name})
    return app


app = create_app()
