"""One place that builds the boto3 session from config/.env (or the default AWS credential chain)."""

import boto3

from config.settings import Settings


def aws_session(settings: Settings) -> boto3.Session:
    """Use the keys in config/.env when present. Otherwise fall back to the role or profile on the machine."""
    creds: dict[str, str] = {}
    key = settings.aws_access_key_id.get_secret_value()
    if key:
        creds["aws_access_key_id"] = key
        creds["aws_secret_access_key"] = settings.aws_secret_access_key.get_secret_value()
        token = settings.aws_session_token.get_secret_value()
        if token:
            creds["aws_session_token"] = token
    return boto3.Session(region_name=settings.aws_region or None, **creds)
