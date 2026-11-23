"""Twilio helpers: softphone access token and TwiML for an incoming call."""

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse

from config.settings import Settings

TOKEN_TTL_SECONDS = 3600


def make_access_token(settings: Settings) -> str:
    token = AccessToken(
        settings.twilio_account_sid,
        settings.twilio_api_key_sid,
        settings.twilio_api_key_secret.get_secret_value(),
        identity=settings.twilio_agent_identity,
        ttl=TOKEN_TTL_SECONDS,
    )
    token.add_grant(
        VoiceGrant(incoming_allow=True, outgoing_application_sid=settings.twilio_twiml_app_sid or None)
    )
    return token.to_jwt()


def build_incoming_twiml(settings: Settings, caller: str) -> str:
    """Stream both call legs to us, then ring the browser softphone."""
    response = VoiceResponse()
    stream = response.start().stream(url=settings.public_stream_url, track="both_tracks")
    stream.parameter(name="caller", value=caller)
    record = "record-from-answer-dual" if settings.twilio_record_calls else "do-not-record"
    dial = response.dial(
        record=record,
        recording_status_callback=f"{settings.public_base_url.rstrip('/')}/api/twilio/recording",
    )
    dial.client(settings.twilio_agent_identity)
    return str(response)
