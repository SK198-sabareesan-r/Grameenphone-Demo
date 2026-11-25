"""Ask Copilot: answers from the approved knowledge, with sources. The chat memory lives in the browser: the recent turns are sent with each question and nothing is kept here."""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container
from copilot.models.schemas import AskRequest

router = APIRouter(prefix="/api", tags=["knowledge"])


@router.post("/ask")
async def ask(body: AskRequest, request: Request) -> dict:
    try:
        history = [turn.model_dump() for turn in body.history]
        return await get_container(request).ask.answer(body.question, history)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ask Copilot failed: {exc}") from exc


@router.get("/knowledge/articles")
async def list_articles(request: Request) -> dict:
    articles = get_container(request).knowledge.articles.values()
    return {"articles": [a.public_view() for a in articles]}
