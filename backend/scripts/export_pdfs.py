"""Render official Grameenphone pages to PDF files for document ingestion (for example Bedrock Data Automation).

Run from the backend folder:

    python -m scripts.export_pdfs --dry-run                 check the page list and robots.txt, no browser
    python -m scripts.export_pdfs                           render every page
    python -m scripts.export_pdfs --lang bangla             only the Bangla pages
    python -m scripts.export_pdfs --only roaming-malaysia-pack
    python -m scripts.export_pdfs --upload                  also copy the PDFs and index.json to S3

First time only:  pip install -r requirements.txt  then  playwright install chromium

Output: data/knowledge/pdf/<language>/<id>.pdf, a matching <id>.txt with the exact page text, and index.json
Rules followed: robots.txt is honoured, a delay is kept between pages (at least the site's Crawl-delay),
redirects are checked hop by hop against robots.txt before a page is loaded, and a refused page (403 and similar)
is reported and skipped, never retried or worked around.
"""

import argparse
import hashlib
import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from config.settings import get_settings
from copilot.utils.aws import aws_session
from scripts.pdf_render import PdfRenderer
from scripts.redirects import check_redirect_chain

log = logging.getLogger("export_pdfs")
USER_AGENT = "Mozilla/5.0"
DEFAULT_DELAY_SECONDS = 10.0
NO_PAGE_LOADED = {"disallowed_by_robots", "too_many_redirects"}  # nothing was fetched, so no need to wait


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=settings.data_dir / "knowledge" / "sources.json")
    parser.add_argument("--out", type=Path, default=settings.data_dir / "knowledge" / "pdf")
    parser.add_argument("--lang", choices=["english", "bangla", "all"], default="all")
    parser.add_argument("--only", nargs="*", default=[], help="page ids to render")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS, help="seconds between pages")
    parser.add_argument("--dry-run", action="store_true", help="list what would run, no browser")
    parser.add_argument("--upload", action="store_true", help="copy PDFs and index.json to S3")
    parser.add_argument("--force", action="store_true", help="render again even if the PDF already exists")
    return parser.parse_args()


def select_pages(manifest: Path, lang: str, only: list[str]) -> list[dict]:
    pages = json.loads(manifest.read_text(encoding="utf-8-sig"))["pages"]
    seen: set[tuple[str, str]] = set()
    for page in pages:
        key = (page["language"], page["id"])
        if key in seen:
            raise SystemExit(f"Duplicate page in manifest: {key}")
        seen.add(key)
    if lang != "all":
        pages = [p for p in pages if p["language"] == lang]
    if only:
        pages = [p for p in pages if p["id"] in only]
    return pages


class Robots:
    """robots.txt rules per site, loaded once."""

    def __init__(self) -> None:
        self._parsers: dict[str, RobotFileParser | None] = {}

    def _load(self, origin: str) -> RobotFileParser | None:
        parser = RobotFileParser(origin + "/robots.txt")
        try:
            request = urllib.request.Request(origin + "/robots.txt", headers={"User-Agent": USER_AGENT})
            parser.parse(urllib.request.urlopen(request, timeout=20).read().decode("utf-8", "ignore").splitlines())
            return parser
        except Exception:
            log.warning("Could not read %s/robots.txt. Pages on that site will be skipped.", origin)
            return None

    def _parser(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._parsers:
            self._parsers[origin] = self._load(origin)
        return self._parsers[origin]

    def allowed(self, url: str) -> bool:
        parser = self._parser(url)
        return bool(parser and parser.can_fetch(USER_AGENT, url))

    def crawl_delay(self, url: str) -> float:
        parser = self._parser(url)
        return float((parser.crawl_delay(USER_AGENT) or 0) if parser else 0)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_index(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {f'{e["language"]}/{e["id"]}': e for e in json.loads(path.read_text(encoding="utf-8"))}


def save_index(path: Path, entries: dict[str, dict]) -> None:
    ordered = sorted(entries.values(), key=lambda e: (e["language"], e["id"]))
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=1), encoding="utf-8")


def process_page(page: dict, args, robots: Robots, renderer: PdfRenderer, today: str) -> dict:
    out_path = args.out / page["language"] / f'{page["id"]}.pdf'
    entry = {k: page[k] for k in ("id", "language", "category", "title", "url")}
    entry.update(status="", file=str(out_path.relative_to(args.out)).replace("\\", "/"), retrieved=today)
    if not robots.allowed(page["url"]):
        return {**entry, "status": "disallowed_by_robots"}
    if out_path.exists() and not args.force:
        return {**entry, "status": "ok", "note": "already rendered", "bytes": out_path.stat().st_size,
                "sha256": sha256(out_path)}
    last_url, verdict = check_redirect_chain(page["url"], robots.allowed, USER_AGENT)
    if verdict != "ok":
        return {**entry, "status": verdict, "final_url": last_url}
    result = renderer.render(page, out_path, today)
    entry.update(status=result.status, http_status=result.http_status, final_url=result.final_url,
                 text_chars=result.text_chars, error=result.error, expanded=result.expand)
    entry["_links"] = result.links  # followed by render_all, not kept in index.json
    if result.status == "ok":
        entry.update(bytes=out_path.stat().st_size, sha256=sha256(out_path))
    return entry


def render_all(pages: list[dict], args, robots: Robots) -> dict[str, dict]:
    args.out.mkdir(parents=True, exist_ok=True)
    index_path = args.out / "index.json"
    index = load_index(index_path)
    today = datetime.now(timezone.utc).date().isoformat()
    with PdfRenderer(allow_navigation=robots.allowed) as renderer:
        for n, page in enumerate(pages, 1):
            entry = process_page(page, args, robots, renderer, today)
            index[f'{entry["language"]}/{entry["id"]}'] = entry
            save_index(index_path, index)  # saved after every page, so an interrupted run keeps its progress
            log.info("[%d/%d] %-8s %s/%s", n, len(pages), entry["status"], entry["language"], entry["id"])
            if n < len(pages) and not entry.get("note") and entry["status"] not in NO_PAGE_LOADED:
                time.sleep(max(args.delay, robots.crawl_delay(page["url"])))
    return index


def upload(index: dict[str, dict], out: Path) -> int:
    settings = get_settings()
    if not settings.s3_bucket:
        raise SystemExit("S3_BUCKET is not set in config/.env")
    client = aws_session(settings).client("s3")
    prefix = settings.s3_knowledge_prefix.strip("/")
    sent = 0
    for entry in index.values():
        if entry["status"] != "ok":
            continue
        path = out / entry["file"]
        client.upload_file(
            str(path), settings.s3_bucket, f"{prefix}/{entry['file']}",
            ExtraArgs={"ContentType": "application/pdf",
                       "Metadata": {"source-url": entry["url"], "retrieved": entry["retrieved"]}},
        )
        sent += 1
    client.upload_file(str(out / "index.json"), settings.s3_bucket, f"{prefix}/index.json",
                       ExtraArgs={"ContentType": "application/json"})
    return sent


def summarize(index: dict[str, dict], selected: list[dict]) -> None:
    keys = {f'{p["language"]}/{p["id"]}' for p in selected}
    counts: dict[str, int] = {}
    for key in keys & index.keys():
        counts[index[key]["status"]] = counts.get(index[key]["status"], 0) + 1
    log.info("Done. %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing processed")
    for key in sorted(keys & index.keys()):
        if index[key]["status"] not in ("ok",):
            e = index[key]
            log.info("  not rendered: %-45s %s %s", key, e["status"], e.get("error") or e.get("http_status") or "")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    pages = select_pages(args.manifest, args.lang, args.only)
    if not pages:
        raise SystemExit("No pages matched.")
    robots = Robots()
    if args.dry_run:
        for page in pages:
            verdict = "allowed" if robots.allowed(page["url"]) else "BLOCKED by robots.txt"
            log.info("%-8s %-45s %s", page["language"], page["id"], verdict)
        log.info("%d pages. Delay between pages: %.0fs or the site's Crawl-delay, whichever is longer.",
                 len(pages), args.delay)
        return
    index = render_all(pages, args, robots)
    summarize(index, pages)
    if args.upload:
        log.info("Uploaded %d PDFs to S3.", upload(index, args.out))


if __name__ == "__main__":
    main()
