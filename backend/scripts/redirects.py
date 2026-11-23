"""Follow redirects one hop at a time, so robots.txt can be checked before a page is ever loaded.

A browser follows redirects inside its network layer, where scripts cannot stop it. Checking the
chain first with light HEAD requests keeps the export from touching a site that forbids it.
"""

import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urljoin

MAX_HOPS = 5
REDIRECT_CODES = {301, 302, 303, 307, 308}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface the 3xx instead of following it


def check_redirect_chain(url: str, allow: Callable[[str], bool], user_agent: str) -> tuple[str, str]:
    """Return (last_url, verdict). Verdict is 'ok', 'disallowed_by_robots' or 'too_many_redirects'.

    Any answer that is not a redirect counts as 'ok'. The renderer reports refusals such as 403 itself.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(MAX_HOPS + 1):
        if not allow(current):
            return current, "disallowed_by_robots"
        request = urllib.request.Request(current, method="HEAD", headers={"User-Agent": user_agent})
        try:
            opener.open(request, timeout=30).close()
            return current, "ok"
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location")
            if error.code not in REDIRECT_CODES or not location:
                return current, "ok"
            current = urljoin(current, location)
        except OSError:
            return current, "ok"  # network trouble: let the renderer report it
    return current, "too_many_redirects"
