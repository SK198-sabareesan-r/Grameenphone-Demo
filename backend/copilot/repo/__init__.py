"""Chooses where the Copilot's data lives, from config/.env."""

from config.settings import Settings
from copilot.repo.base import DataUnavailable, Repo
from copilot.repo.dynamo import DynamoRepo
from copilot.repo.files import FileRepo
from copilot.repo.resilient import ResilientRepo

__all__ = ["DataUnavailable", "Repo", "build_repo"]


def build_repo(settings: Settings) -> Repo:
    """DATA_BACKEND=files: local files only. dynamodb: the tables, with the files as a fallback unless
    DATA_FALLBACK_TO_FILES=false."""
    backend = settings.data_backend.strip().lower()
    if backend == "files":
        return FileRepo(settings)
    if backend != "dynamodb":
        raise ValueError(f"DATA_BACKEND must be 'dynamodb' or 'files', not {settings.data_backend!r}")
    dynamo = DynamoRepo(settings)
    if not settings.data_fallback_to_files:
        return dynamo
    return ResilientRepo(dynamo, FileRepo(settings), settings.data_retry_after_s)
