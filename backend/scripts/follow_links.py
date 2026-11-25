"""Pages and documents that an exported page points to: "View details" links, tabs that are really other pages,
and PDF documents (for example the ISD tariff).

Only one level is followed (a followed page's own links are not followed), the number of followed items is capped,
and the same rules as the main export apply: robots.txt, redirects checked hop by hop, a pause between requests.
A document is kept only if it really is a PDF and is not larger than MAX_FILE_BYTES.
"""
import hashlib
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger("follow_links")
SITE_SUFFIX = "grameenphone.com"
MAX_FILE_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 60


def normalize(url: str) -> str:
    """An address without #fragment and trailing slash, lower-case host, for telling whether two links are one page."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}" + (f"?{parts.query}" if parts.query else "")


def same_site(url: str) -> bool:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return host == SITE_SUFFIX or host.endswith("." + SITE_SUFFIX)


def child_id(parent_id: str, url: str, kind: str) -> str:
    """parent-id--last-part-of-the-address, or a short hash when the address has no plain ASCII part."""
    tail = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    tail = re.sub(r"\.pdf$", "", tail, flags=re.I)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", tail).strip("-").lower()[:50]
    return f"{parent_id}--{slug or hashlib.sha1(url.encode()).hexdigest()[:8]}"


def pick_children(
    parent: dict, links: list[dict], known: set[str], allowed: Callable[[str], bool], limit: int
) -> list[dict]:
    """The links worth exporting as their own documents: not already exported, on the site, allowed by robots.txt.
    Each is described like a manifest row, plus `kind` (page or file) and `parent`."""
    picked: list[dict] = []
    skipped = 0
    for link in links:
        url = link["href"]
        key = normalize(url)
        if key in known or key == normalize(parent["url"]):
            continue
        if not same_site(url) or not url.startswith("http") or not allowed(url):
            skipped += 1
            continue
        known.add(key)  # a second page that links to the same thing does not export it again
        label = link["text"].strip() or parent["title"]
        picked.append({
            "id": child_id(parent["id"], url, link["kind"]),
            "language": parent["language"],
            "category": parent["category"],
            "title": label if label == parent["title"] else f"{parent['title']}: {label}",
            "url": url,
            "kind": link["kind"],
            "parent": parent["id"],
        })
        if len(picked) >= limit:
            break
    if skipped:
        log.info("  %d link(s) of %s left out: other site, or not allowed by robots.txt", skipped, parent["id"])
    return picked


def download_pdf(url: str, out_path: Path, user_agent: str) -> tuple[str, str]:
    """Fetch a PDF. Returns (status, detail). Nothing is saved unless it is a PDF of acceptable size."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as reply:
            final = reply.geturl()
            if not same_site(final):
                return "off_site", final
            declared = int(reply.headers.get("Content-Length") or 0)
            if declared > MAX_FILE_BYTES:
                return "too_large", f"{declared} bytes"
            body = reply.read(MAX_FILE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return ("blocked" if error.code in (401, 403, 429) else "error"), f"HTTP {error.code}"
    except OSError as error:
        return "error", str(error)[:150]
    if len(body) > MAX_FILE_BYTES:
        return "too_large", f"over {MAX_FILE_BYTES} bytes"
    if not body.startswith(b"%PDF"):
        return "not_pdf", "the file is not a PDF"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(body)
    return "ok", final
