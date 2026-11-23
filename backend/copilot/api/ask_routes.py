"""Ask Copilot: answers from the approved knowledge, with sources."""

from fastapi import APIRouter, HTTPException, Request

from copilot.api.deps import get_container
from copilot.models.schemas import AskRequest

router = APIRouter(prefix="/api", tags=["knowledge"])


@router.post("/ask")
async def ask(body: AskRequest, request: Request) -> dict:
    try:
        return await get_container(request).ask.answer(body.question)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ask Copilot failed: {exc}") from exc


@router.get("/knowledge/articles")
async def list_articles(request: Request) -> dict:
    articles = get_container(request).knowledge.articles.values()
    return {"articles": [a.public_view() for a in articles]}
