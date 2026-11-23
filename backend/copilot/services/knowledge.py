"""Knowledge pack: markdown articles with front matter and keyword retrieval."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from copilot.utils.helpers import read_text

_TOKEN = re.compile(r"\w+", re.UNICODE)


@dataclass
class Article:
    id: str
    title: str
    type: str
    source_url: str
    fetched: str
    category: str
    keywords: list[str]
    body: str
    tokens: set[str] = field(default_factory=set)

    def public_view(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "source_url": self.source_url,
        }


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if len(t) > 1}


def parse_article(raw: str) -> Article:
    if not raw.startswith("---"):
        raise ValueError("Article is missing front matter")
    _, header, body = raw.split("---", 2)
    meta: dict[str, str] = {}
    for line in header.strip().splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    keywords = [k.strip() for k in meta.get("keywords", "").split(",") if k.strip()]
    article = Article(
        id=meta["id"],
        title=meta.get("title", meta["id"]),
        type=meta.get("type", "public"),
        source_url=meta.get("source_url", ""),
        fetched=meta.get("fetched", ""),
        category=meta.get("category", ""),
        keywords=keywords,
        body=body.strip(),
    )
    article.tokens = _tokens(" ".join(keywords) + " " + article.title)
    return article


class KnowledgeBase:
    def __init__(self, articles_dir: Path) -> None:
        self.articles: dict[str, Article] = {}
        for path in sorted(articles_dir.glob("*.md")):
            article = parse_article(read_text(path))
            self.articles[article.id] = article

    def get(self, article_id: str) -> Article | None:
        return self.articles.get(article_id)

    def search(self, query: str, limit: int = 4) -> list[Article]:
        """Rank articles by keyword phrase hits and token overlap."""
        q_lower, q_tokens = query.lower(), _tokens(query)
        scored: list[tuple[float, Article]] = []
        for article in self.articles.values():
            phrase_hits = sum(1 for k in article.keywords if k.lower() in q_lower)
            overlap = len(q_tokens & article.tokens)
            score = phrase_hits * 3 + overlap
            if score > 0:
                scored.append((score, article))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [a for _, a in scored[:limit]]
