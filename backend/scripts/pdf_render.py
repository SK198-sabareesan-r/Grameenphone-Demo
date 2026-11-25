"""Render one web page to a PDF with a headless browser (Playwright, Chromium).

A real browser is used so Bangla text is shaped correctly and the page's own scripts run.
Nothing here tries to get around a site's blocks: a refused page is reported and skipped.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from scripts.page_expand import expand_page

NAV_TIMEOUT_MS = 60_000
MIN_TEXT_CHARS = 200
BLOCKED_STATUSES = {401, 403, 429}
ALLOWED_HOST_SUFFIX = "grameenphone.com"

# Removes menus, footers, pop-ups and floating widgets. Done before anything is opened, so a menu is never expanded.
CLEAN_JS = """
() => {
  const kill = (el) => el && el.remove();
  document.querySelectorAll('nav, footer, dialog, [role="navigation"], [role="dialog"], [role="banner"]')
    .forEach(kill);
  document.querySelectorAll('[class*="footer" i], [id*="footer" i], [class*="survey" i], [class*="share-" i]').forEach((el) => {
    if (el !== document.body && !el.querySelector('h1') && !el.querySelector('main, article')) kill(el);
  });
  document.querySelectorAll('header').forEach((h) => { if (!h.querySelector('h1')) kill(h); });
  for (const el of Array.from(document.body.querySelectorAll('*'))) {
    const pos = getComputedStyle(el).position;
    if ((pos === 'fixed' || pos === 'sticky') && !el.querySelector('h1')) kill(el);
  }
}
"""
# Adds a source line at the top, and returns how much text the page now holds.
LABEL_JS = """
(info) => {
  const box = document.createElement('div');
  box.style.cssText = 'border:1px solid #0078cf;padding:8px 12px;margin:0 0 16px;' +
    'font:12px sans-serif;color:#00508f;word-break:break-all';
  box.textContent = info.title + ' | Source: ' + info.url + ' | Retrieved: ' + info.retrieved;
  document.body.prepend(box);
  return document.body.innerText.length;
}
"""
SCROLL_JS = """
async () => {
  const step = 600;
  for (let y = 0; y < document.body.scrollHeight; y += step) {
    window.scrollTo(0, y);
    await new Promise((r) => setTimeout(r, 120));
  }
  window.scrollTo(0, 0);
}
"""

FOOTER_TEMPLATE = (
    '<div style="width:100%;font-size:8px;text-align:center;color:#666">'
    'Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>'
)


@dataclass
class RenderResult:
    status: str  # ok | blocked | error | empty | off_site | disallowed_by_robots
    http_status: int | None = None
    final_url: str = ""
    text_chars: int = 0
    error: str = ""
    expand: dict | None = None  # what was opened on the page (tabs, accordions, Load more, details)
    links: list = field(default_factory=list)  # detail pages and PDF documents the page links to


def _host_allowed(url: str) -> bool:
    host = url.split("/")[2] if "//" in url else ""
    return host == ALLOWED_HOST_SUFFIX or host.endswith("." + ALLOWED_HOST_SUFFIX)


class PdfRenderer:
    """Keeps one browser open for the whole run. Use as a context manager."""

    def __init__(self, allow_navigation: Callable[[str], bool]) -> None:
        self._allow = allow_navigation  # robots.txt check, applied to every page the browser opens
        self._playwright = None
        self._browser = None

    def __enter__(self) -> "PdfRenderer":
        from playwright.sync_api import sync_playwright  # imported late so --dry-run works without it

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        return self

    def __exit__(self, *exc) -> None:
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def render(self, page_info: dict, out_path: Path, retrieved: str) -> RenderResult:
        context = self._browser.new_context(viewport={"width": 1280, "height": 1600})
        page = context.new_page()
        refused: list[str] = []

        def guard(route) -> None:
            request = route.request
            if request.resource_type != "document":
                route.continue_()
            elif request.frame != page.main_frame:
                route.abort()  # embedded frames (ads, chat widgets) are not part of the document
            elif not self._allow(request.url):
                refused.append(request.url)  # redirects are checked earlier, in redirects.py
                route.abort()
            else:
                route.continue_()

        page.route("**/*", guard)
        try:
            return self._render(page, page_info, out_path, retrieved)
        except Exception as exc:  # network error, timeout, browser crash on this page
            if refused:
                return RenderResult(status="disallowed_by_robots", final_url=refused[0])
            return RenderResult(status="error", error=str(exc).splitlines()[0][:200])
        finally:
            context.close()

    def _render(self, page, info: dict, out_path: Path, retrieved: str) -> RenderResult:
        response = page.goto(info["url"], wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        http = response.status if response else None
        if http in BLOCKED_STATUSES:
            return RenderResult(status="blocked", http_status=http, final_url=page.url)
        if http is None or http >= 400:
            return RenderResult(status="error", http_status=http, final_url=page.url, error=f"HTTP {http}")
        if not self._allow(page.url):  # last line of defence if a script redirected the page
            return RenderResult(status="disallowed_by_robots", http_status=http, final_url=page.url)
        if not _host_allowed(page.url):
            return RenderResult(status="off_site", http_status=http, final_url=page.url)
        page.evaluate(SCROLL_JS)
        page.emulate_media(media="screen")
        page.evaluate(CLEAN_JS)
        report = expand_page(page)  # open tabs, accordions, Load more and View details before printing
        chars = page.evaluate(
            LABEL_JS, {"title": info["title"], "url": info["url"], "retrieved": retrieved}
        )
        if chars < MIN_TEXT_CHARS:
            return RenderResult(status="empty", http_status=http, final_url=page.url, text_chars=chars, expand=report.summary())
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Exact page text next to the PDF. Bengali text inside a PDF can gain stray spaces when read back.
        out_path.with_suffix(".txt").write_text(page.evaluate("document.body.innerText"), encoding="utf-8")
        page.pdf(
            path=str(out_path),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=FOOTER_TEMPLATE,
            margin={"top": "14mm", "bottom": "16mm", "left": "12mm", "right": "12mm"},
        )
        return RenderResult(status="ok", http_status=http, final_url=page.url, text_chars=chars,
                            expand=report.summary(), links=report.links)
