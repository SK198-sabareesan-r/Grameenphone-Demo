"""Write a .metadata.json sidecar next to each knowledge article, for a Bedrock Knowledge Base.

Run from backend/:  python -m scripts.make_kb_metadata
The sidecars are generated files and are git-ignored. The articles stay the source of truth.
"""
import json
from pathlib import Path

ARTICLES = Path(__file__).resolve().parents[1] / "data" / "knowledge" / "articles"
FIELDS = ("id", "title", "type", "category", "source_url", "fetched")


def read_front_matter(text: str) -> dict[str, str]:
    """Return the simple `key: value` lines between the first two --- markers."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    return meta


def main() -> None:
    count = 0
    for path in sorted(ARTICLES.glob("*.md")):
        meta = read_front_matter(path.read_text(encoding="utf-8"))
        attrs = {k: meta[k] for k in FIELDS if meta.get(k)}
        attrs["language"] = "en"
        sidecar = path.with_name(path.name + ".metadata.json")
        sidecar.write_text(json.dumps({"metadataAttributes": attrs}, indent=2), encoding="utf-8")
        count += 1
    print(f"wrote {count} sidecars in {ARTICLES}")


if __name__ == "__main__":
    main()
