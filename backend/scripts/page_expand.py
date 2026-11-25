"""Open everything a page hides before it is printed, so the PDF holds the whole page and not only what shows first.

Handled: tabs (every tab is opened and its panel copied), accordions and <details> (forced open), "Load more"
buttons (clicked until nothing new appears), and "View details" / "Read more" buttons (clicked; a pop-up they open
is read and closed). Links to detail pages and to PDF documents are collected so the exporter can fetch them too.

Nothing here logs in, fills a form, buys or submits anything: only buttons whose text says "more" or "details"
are clicked, and a button that is a link to another page is never clicked, only recorded.
"""
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("page_expand")

# The same patterns are used in Python and in the page's JavaScript, so they only use syntax both share.
MORE_PATTERN = r"^\s*(?:(?:load|show|view|see)\s+(?:more|all)|more|(?:আরও|আরো)(?:\s+(?:দেখুন|লোড))?|সব\s+দেখুন)\s*$"
DETAIL_PATTERN = (
    r"^\s*(?:(?:view|see|show|more)\s+details?|details?|(?:read|learn|know)\s+more|more\s+info(?:rmation)?"
    r"|বিস্তারিত(?:\s+দেখুন)?|(?:আরও|আরো)\s+জানুন|জানুন)\s*[>»→]?\s*$"
)
MAX_MORE_ROUNDS = 40  # clicks on "Load more" per page
MAX_DETAIL_CLICKS = 80  # "View details" buttons per page
SETTLE_MS = 500
NETWORK_WAIT_MS = 3000
CLICK_TIMEOUT_MS = 4000

_VISIBLE_JS = (
    "const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);"
    " return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };"
)

# Forces every collapsed section open. Tabs are left alone (they are handled one by one).
OPEN_COLLAPSED_JS = (
    r"""
() => {
  """
    + _VISIBLE_JS
    + r"""
  let opened = 0;
  document.querySelectorAll('details:not([open])').forEach((d) => { d.open = true; opened++; });
  const show = (el, count = true) => {
    const hiddenBefore = count && !vis(el);
    el.classList.add('show', 'open');
    el.removeAttribute('hidden');
    el.removeAttribute('data-bs-parent');
    for (const [k, v] of [['display', 'block'], ['height', 'auto'], ['max-height', 'none'], ['visibility', 'visible'], ['opacity', '1']]) {
      el.style.setProperty(k, v, 'important');
    }
    if (hiddenBefore) opened++;
  };
  const isTab = (el) => el.getAttribute('role') === 'tab' || el.closest('[role=tablist]');
  document.querySelectorAll('.accordion-collapse, [class*=accordion-collapse], [class*=accordion__panel]').forEach((e) => show(e));
  document.querySelectorAll('.accordion-body').forEach((e) => show(e, false));
  const toggles = document.querySelectorAll(
    '[data-bs-toggle=collapse], [data-toggle=collapse], .accordion-button, [aria-expanded=false]');
  toggles.forEach((t) => {
    if (isTab(t) || t.hasAttribute('aria-haspopup')) return;
    const id = t.getAttribute('aria-controls') || (t.getAttribute('data-bs-target') || t.getAttribute('data-target') || '').replace('#', '')
      || (t.getAttribute('href') || '').replace('#', '');
    const target = id && document.getElementById(id);
    if (target && target.getAttribute('role') !== 'tabpanel' && !target.matches('nav, [role=menu], [role=dialog]')) show(target);
    t.setAttribute('aria-expanded', 'true');
    t.classList.remove('collapsed');
  });
  return opened;
}
"""
)

# Marks the visible buttons whose text matches `pattern` with data-gp-<name>="<n>". Links to other pages are skipped.
MARK_BUTTONS_JS = (
    r"""
([pattern, attr]) => {
  """
    + _VISIBLE_JS
    + r"""
  const re = new RegExp(pattern, 'i');
  let n = 0;
  document.querySelectorAll('button, [role=button], summary, a').forEach((el) => {
    if (el.hasAttribute(attr) || el.hasAttribute('data-gp-done') || el.disabled) return;
    if (el.tagName === 'A') {
      const h = el.getAttribute('href') || '';
      if (h && !h.startsWith('#') && !h.toLowerCase().startsWith('javascript')) return;
    }
    const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
    if (!text || text.length > 40 || !re.test(text) || !vis(el)) return;
    el.setAttribute(attr, String(n++));
  });
  return n;
}
"""
)

PAGE_TEXT_LENGTH_JS = "() => document.body.innerText.length"

# The text of the pop-up a button opened, or null.
READ_DIALOG_JS = (
    r"""
() => {
  """
    + _VISIBLE_JS
    + r"""
  const sel = '[role=dialog], [aria-modal=true], .modal.show, [class*=modal-content], [class*=Modal], [class*=drawer], [class*=Drawer], [class*=popup], [class*=Popup]';
  const found = [...document.querySelectorAll(sel)].filter((el) => vis(el) && (el.innerText || '').trim().length > 0);
  if (!found.length) return null;
  const strong = found.filter((el) => el.matches('[role=dialog], [aria-modal=true]'));
  const pool = strong.length ? strong : found;
  pool.sort((a, b) => b.innerText.length - a.innerText.length);
  return pool[0].innerText.trim();
}
"""
)

CLOSE_DIALOG_JS = (
    r"""
() => {
  """
    + _VISIBLE_JS
    + r"""
  const sel = '[role=dialog], [aria-modal=true], .modal.show, [class*=modal-content], [class*=Modal], [class*=drawer], [class*=Drawer], [class*=popup], [class*=Popup]';
  const open = [...document.querySelectorAll(sel)].filter(vis);
  open.forEach((el) => {
    const close = el.querySelector('[aria-label*=close i], [class*=close i], .btn-close, button[title*=close i]');
    if (close) close.click();
  });
  return open.length;
}
"""
)

REMOVE_DIALOGS_JS = (
    r"""
() => {
  document.querySelectorAll('[role=dialog], [aria-modal=true], .modal, .modal-backdrop, [class*=Overlay], [class*=overlay]').forEach((e) => e.remove());
  document.body.style.overflow = 'auto';
}
"""
)

# The heading of the card a button sits in, for the title of what its pop-up said.
LABEL_FOR_JS = r"""
(attr) => {
  const el = document.querySelector('[' + attr + ']');
  let node = el;
  for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
    const h = node.querySelector('h1, h2, h3, h4, h5, [class*=title], [class*=Title], strong');
    if (h && (h.innerText || '').trim()) return h.innerText.trim().replace(/\s+/g, ' ').slice(0, 80);
  }
  return 'Details';
}
"""

APPEND_DETAIL_JS = r"""
([label, text]) => {
  let holder = document.getElementById('gp-details');
  if (!holder) { holder = document.createElement('div'); holder.id = 'gp-details'; document.body.appendChild(holder); }
  const section = document.createElement('section');
  const h = document.createElement('h3');
  h.textContent = 'Details: ' + label;
  const body = document.createElement('div');
  body.style.whiteSpace = 'pre-wrap';
  body.textContent = text;
  section.append(h, body);
  holder.appendChild(section);
}
"""

# Tabs: tag each tab list, its tabs and the box that holds the panels.
TAG_TABS_JS = r"""
() => {
  const out = [];
  let n = 0;
  document.querySelectorAll('[role=tablist]').forEach((list) => {
    if (list.closest('[role=tabpanel]')) return;
    const tabs = [...list.querySelectorAll('[role=tab]')];
    if (tabs.length < 2) return;
    let box = list.parentElement;
    while (box && !box.querySelector('[role=tabpanel]')) box = box.parentElement;
    if (!box) return;
    box.setAttribute('data-gp-box', String(n));
    tabs.forEach((t, i) => t.setAttribute('data-gp-tab', n + '-' + i));
    out.push({ box: n, labels: tabs.map((t) => (t.innerText || t.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ')) });
    n++;
  });
  return out;
}
"""

COPY_PANEL_JS = (
    r"""
([n, i, label]) => {
  """
    + _VISIBLE_JS
    + r"""
  const box = document.querySelector('[data-gp-box="' + n + '"]');
  const tab = document.querySelector('[data-gp-tab="' + n + '-' + i + '"]');
  if (!box || !tab) return false;
  let panel = null;
  const id = tab.getAttribute('aria-controls');
  if (id) panel = document.getElementById(id);
  if (!panel || !vis(panel)) panel = [...box.querySelectorAll('[role=tabpanel]')].find(vis) || null;
  if (!panel) return false;
  let holder = document.getElementById('gp-tabs-' + n);
  if (!holder) { holder = document.createElement('div'); holder.id = 'gp-tabs-' + n; box.parentNode.insertBefore(holder, box); }
  const copy = panel.cloneNode(true);
  copy.querySelectorAll('button').forEach((b) => {
    if (/^\s*(buy|load more|add to cart)\s*$/i.test(b.innerText || '')) b.remove();
  });
  copy.removeAttribute('hidden');
  copy.style.setProperty('display', 'block', 'important');
  const section = document.createElement('section');
  const heading = document.createElement('h2');
  heading.textContent = label || ('Tab ' + (i + 1));
  section.append(heading, copy);
  holder.appendChild(section);
  return true;
}
"""
)

REMOVE_TAB_BOX_JS = "(n) => { const b = document.querySelector('[data-gp-box=\"' + n + '\"]'); if (b) b.remove(); }"

# Links worth following: "View details" style links to pages on the site, and PDF documents.
COLLECT_LINKS_JS = (
    r"""
([pattern]) => {
  const re = new RegExp(pattern, 'i');
  const out = [];
  document.querySelectorAll('a[href]').forEach((a) => {
    const raw = a.getAttribute('href') || '';
    if (!raw || raw.startsWith('#') || raw.toLowerCase().startsWith('javascript') || raw.startsWith('mailto') || raw.startsWith('tel')) return;
    const text = (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
    const isFile = /\.pdf($|[?#])/i.test(a.href);
    if (isFile || (text.length < 40 && re.test(text))) out.push({ href: a.href, text: text.slice(0, 80), kind: isFile ? 'file' : 'page' });
  });
  return out;
}
"""
)


@dataclass
class ExpandReport:
    chars_before: int = 0
    chars_after: int = 0
    tabs: int = 0
    collapsed_opened: int = 0
    more_clicks: int = 0
    detail_clicks: int = 0
    dialogs_read: int = 0
    links: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "chars_before": self.chars_before, "chars_after": self.chars_after, "tabs": self.tabs,
            "collapsed_opened": self.collapsed_opened, "more_clicks": self.more_clicks,
            "detail_clicks": self.detail_clicks, "dialogs_read": self.dialogs_read,
            "links_found": len(self.links), "errors": self.errors[:5],
        }


def _wait_for_content(page) -> None:
    page.wait_for_timeout(SETTLE_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=NETWORK_WAIT_MS)
    except Exception:
        pass  # a page that keeps polling never goes idle; what has loaded is enough


def _click(page, attr: str, index: int) -> bool:
    """Click the element marked data-gp-<attr>=index. False when it cannot be clicked (gone, covered, disabled)."""
    try:
        page.locator(f"[{attr}='{index}']").first.click(timeout=CLICK_TIMEOUT_MS)
        return True
    except Exception as error:
        log.debug("click skipped: %s", str(error).splitlines()[0][:100])
        return False


def _mark_done(page, attr: str, index: int) -> None:
    page.evaluate(
        "([a, i]) => { const e = document.querySelector('[' + a + '=\"' + i + '\"]');"
        " if (e) { e.removeAttribute(a); e.setAttribute('data-gp-done', '1'); } }",
        [attr, str(index)],
    )


def open_collapsed(page, report: ExpandReport) -> None:
    report.collapsed_opened += int(page.evaluate(OPEN_COLLAPSED_JS) or 0)


def click_more(page, report: ExpandReport) -> None:
    """Click "Load more" until no new text appears."""
    attr = "data-gp-more"
    for _ in range(MAX_MORE_ROUNDS):
        if not page.evaluate(MARK_BUTTONS_JS, [MORE_PATTERN, attr]):
            return
        before = page.evaluate(PAGE_TEXT_LENGTH_JS)
        if not _click(page, attr, 0):
            _mark_done(page, attr, 0)
            continue
        report.more_clicks += 1
        _wait_for_content(page)
        if page.evaluate(PAGE_TEXT_LENGTH_JS) <= before:  # nothing new: do not click this one again
            _mark_done(page, attr, 0)
        else:
            page.evaluate("(a) => document.querySelectorAll('[' + a + ']').forEach((e) => e.removeAttribute(a))", attr)


def _read_and_close_dialog(page, label: str, report: ExpandReport) -> None:
    text = page.evaluate(READ_DIALOG_JS)
    if not text:
        return
    report.dialogs_read += 1
    page.evaluate(APPEND_DETAIL_JS, [label, text])
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if page.evaluate(CLOSE_DIALOG_JS):
        page.wait_for_timeout(300)
    if page.evaluate(READ_DIALOG_JS):  # still open: remove it so it cannot block the next click
        page.evaluate(REMOVE_DIALOGS_JS)


def click_details(page, report: ExpandReport) -> None:
    """Click each "View details" style button. A pop-up it opens is read, kept as text and closed."""
    attr = "data-gp-detail"
    count = page.evaluate(MARK_BUTTONS_JS, [DETAIL_PATTERN, attr])
    for index in range(min(count, MAX_DETAIL_CLICKS - report.detail_clicks)):
        label = page.evaluate(LABEL_FOR_JS, f"{attr}='{index}'")
        before = page.evaluate(PAGE_TEXT_LENGTH_JS)
        if _click(page, attr, index):
            report.detail_clicks += 1
            _wait_for_content(page)
            if page.evaluate(PAGE_TEXT_LENGTH_JS) != before:
                _read_and_close_dialog(page, label, report)
        _mark_done(page, attr, index)


def settle(page, report: ExpandReport) -> None:
    """Open everything that is currently hidden. Run again after a tab changes, since a tab can hold more."""
    open_collapsed(page, report)
    click_more(page, report)
    open_collapsed(page, report)
    click_details(page, report)
    open_collapsed(page, report)


def expand_tabs(page, report: ExpandReport) -> None:
    """Open every tab in turn and copy its panel in place of the tab box, so all tabs are printed."""
    for group in page.evaluate(TAG_TABS_JS):
        copied = 0
        for index, label in enumerate(group["labels"]):
            if not _click(page, "data-gp-tab", f"{group['box']}-{index}"):
                continue
            _wait_for_content(page)
            settle(page, report)
            copied += bool(page.evaluate(COPY_PANEL_JS, [group["box"], index, label]))
        if copied:
            page.evaluate(REMOVE_TAB_BOX_JS, group["box"])
            report.tabs += copied


def expand_page(page) -> ExpandReport:
    """Open everything the page hides. A step that fails is recorded and skipped: the page is still printed."""
    report = ExpandReport(chars_before=page.evaluate(PAGE_TEXT_LENGTH_JS))
    for name, step in (("settle", settle), ("tabs", expand_tabs), ("settle again", settle)):
        try:
            step(page, report)
        except Exception as error:
            report.errors.append(f"{name}: {str(error).splitlines()[0][:120]}")
            log.warning("expansion step %r failed: %s", name, report.errors[-1])
    try:
        seen = set()
        for link in page.evaluate(COLLECT_LINKS_JS, [DETAIL_PATTERN]):
            if link["href"] not in seen:
                seen.add(link["href"])
                report.links.append(link)
    except Exception as error:
        report.errors.append(f"links: {str(error).splitlines()[0][:120]}")
    report.chars_after = page.evaluate(PAGE_TEXT_LENGTH_JS)
    return report


def looks_like_file(url: str) -> bool:
    return bool(re.search(r"\.pdf($|[?#])", url, re.I))
