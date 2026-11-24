"""Upload the knowledge documents and their .metadata.json sidecars to the S3 bucket for a Bedrock Knowledge Base.

Run from backend/:
    python -m scripts.upload_kb --dry-run     list what would be uploaded
    python -m scripts.upload_kb               upload (overwrites objects with the same key)

Bucket and credentials come from config/.env (S3_BUCKET, AWS_*). Layout in the bucket:
    en/articles/<id>.md   en/pdf/<id>.pdf   bn/pdf/<id>.pdf   (each with <name>.metadata.json beside it)
Not uploaded: the .txt copies, index.json, sources.json and the planning .md files.
"""
import argparse
import json
from pathlib import Path

from config.settings import Settings
from copilot.utils.aws import aws_session
from scripts.make_kb_metadata import FIELDS, read_front_matter

KNOWLEDGE = Path(__file__).resolve().parents[1] / "data" / "knowledge"
LANG_CODE = {"english": "en", "bangla": "bn"}


def article_items() -> list[tuple[Path, str, dict]]:
    """(local file, S3 key, metadata) for each markdown article."""
    items = []
    for path in sorted((KNOWLEDGE / "articles").glob("*.md")):
        front = read_front_matter(path.read_text(encoding="utf-8"))
        attrs = {k: front[k] for k in FIELDS if front.get(k)}
        attrs["doc_id"] = attrs.pop("id", path.stem)  # "id" is not accepted as a metadata key by the KB
        attrs.update(language="en", source_format="md")
        items.append((path, f"en/articles/{path.name}", attrs))
    return items


def pdf_items() -> list[tuple[Path, str, dict]]:
    """(local file, S3 key, metadata) for each exported PDF listed as ok in index.json."""
    index = json.loads((KNOWLEDGE / "pdf" / "index.json").read_text(encoding="utf-8"))
    items = []
    for row in index:
        path = KNOWLEDGE / "pdf" / row["file"]
        if row.get("status") != "ok" or not path.exists():
            continue
        code = LANG_CODE[row["language"]]
        attrs = {
            "doc_id": row["id"], "title": row["title"], "type": "public", "category": row["category"],
            "source_url": row["url"], "fetched": row["retrieved"], "language": code, "source_format": "pdf",
        }
        items.append((path, f"{code}/pdf/{path.name}", attrs))
    return items


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    dry_run = parser.parse_args().dry_run
    settings = Settings()
    if not settings.s3_bucket:
        raise SystemExit("S3_BUCKET is empty in config/.env")
    s3 = None if dry_run else aws_session(settings).client("s3")
    count = 0
    for path, key, attrs in article_items() + pdf_items():
        sidecar = json.dumps({"metadataAttributes": attrs}, ensure_ascii=False, indent=2).encode("utf-8")
        print(("would upload " if dry_run else "upload ") + key)
        if s3:
            s3.upload_file(str(path), settings.s3_bucket, key)
            s3.put_object(Bucket=settings.s3_bucket, Key=key + ".metadata.json", Body=sidecar,
                          ContentType="application/json")
        count += 1
    print(f"{count} documents, {count} sidecars -> s3://{settings.s3_bucket}/")


if __name__ == "__main__":
    main()
