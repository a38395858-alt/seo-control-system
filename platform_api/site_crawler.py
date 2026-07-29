"""Small, conservative crawler used to build a site's first-party knowledge base.

It deliberately only follows pages on the website configured for the project.
The crawler is not a generic web scraper: binary files, account/search pages and
thin navigation-only pages are ignored before anything is stored.
"""

from __future__ import annotations

import concurrent.futures
import html
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


USER_AGENT = "SEO-KnowledgeBase-Crawler/1.0 (+local project knowledge collection)"
MAX_HTML_BYTES = 2_500_000
SKIP_SUFFIXES = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".7z",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".mp4", ".mp3",
)
SKIP_PATH_PARTS = ("/wp-admin", "/wp-login", "/search", "/tag/", "/category/", "/feed", "/cart", "/checkout", "/my-account", "/compare", "/wishlist", "/blog", "/news", "/author")
LANGUAGE_ROOTS = {"ar", "de", "es", "fi", "fr", "it", "ja", "ko", "nl", "pl", "pt", "ru", "sv", "tr", "vi"}
COMPANY_TERMS = ("about", "company", "factory", "manufacturer", "our-story", "who-we-are", "contact")
PRODUCT_TERMS = ("product", "products", "shop", "catalog", "series", "solution", "lighting")
KNOWLEDGE_TERMS = ("faq", "certificate", "certification", "quality", "case", "project", "application", "service")


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[str] = []
        self._parts: list[str] = []
        self._semantic_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._content_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"main", "article"}:
            self._content_depth += 1
        if tag == "a" and values.get("href") and not self._skip_depth:
            self.links.append(values["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"main", "article"} and self._content_depth:
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value or self._skip_depth:
            return
        if self._in_title:
            self.title = f"{self.title} {value}".strip()
        self._parts.append(value)
        if self._content_depth:
            self._semantic_parts.append(value)

    @property
    def text(self) -> str:
        # Navigation-heavy commerce pages frequently contain thousands of words
        # before their product description.  Prefer <main>/<article> whenever
        # present and only fall back to the readable body for older templates.
        parts = self._semantic_parts if len(" ".join(self._semantic_parts)) >= 180 else self._parts
        return re.sub(r"\s+", " ", html.unescape(" ".join(parts))).strip()


def _base_url(domain: str) -> str:
    candidate = domain.strip()
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("website domain must be a valid http(s) domain")
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def _is_public_hostname(hostname: str) -> bool:
    """Do not turn a project URL into an internal-network request."""
    try:
        addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        resolved = {item[4][0] for item in addresses}
    except OSError:
        return False
    for address in resolved:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return bool(resolved)


def _canonical(url: str) -> str:
    parsed = urlsplit(url)
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _is_same_site(url: str, host: str) -> bool:
    parsed = urlsplit(url)
    candidate = (parsed.hostname or "").lower().removeprefix("www.")
    return parsed.scheme in {"http", "https"} and candidate == host.removeprefix("www.")


def _is_candidate(url: str) -> bool:
    lowered = urlsplit(url).path.lower()
    root = lowered.strip("/")
    return (
        root not in LANGUAGE_ROOTS
        and not lowered.endswith(SKIP_SUFFIXES)
        and not any(part in lowered for part in SKIP_PATH_PARTS)
    )


def _priority(url: str) -> tuple[int, str]:
    """Put company and product pages ahead of generic leaf pages."""
    path = urlsplit(url).path.lower()
    if any(term in path for term in COMPANY_TERMS):
        return (0, path)
    if any(term in path for term in PRODUCT_TERMS):
        return (1, path)
    if any(term in path for term in KNOWLEDGE_TERMS):
        return (2, path)
    return (3, path)


def _knowledge_type(url: str, title: str, content: str) -> str:
    evidence = f"{url} {title} {content[:2500]}".lower()
    if urlsplit(url).path in {"", "/"}:
        return "company"
    if any(term in evidence for term in ("faq", "frequently asked")):
        return "faq"
    if any(term in urlsplit(url).path.lower() for term in ("case-study", "case_study", "/cases/", "/projects/")):
        return "case_study"
    if any(term in evidence for term in PRODUCT_TERMS) or any(term in evidence for term in ("model", "specification", "ip65", "ip67", "watt")):
        return "product"
    if any(term in evidence for term in ("certificate", "certification", "iso ", " ce ", "rohs")):
        return "certification"
    if any(term in evidence for term in COMPANY_TERMS):
        return "company"
    return "other"


def _read_page(url: str, expected_host: str) -> dict[str, object]:
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        with urlopen(request, timeout=12) as response:  # noqa: S310 - domain is restricted below
            final_url = _canonical(response.geturl())
            if not _is_same_site(final_url, expected_host):
                return {"url": url, "status": "skipped", "reason": "redirected outside the current website"}
            content_type = response.headers.get_content_type().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                return {"url": final_url, "status": "skipped", "reason": f"not an HTML page ({content_type})"}
            raw = response.read(MAX_HTML_BYTES + 1)
            if len(raw) > MAX_HTML_BYTES:
                return {"url": final_url, "status": "skipped", "reason": "HTML page is too large"}
            charset = response.headers.get_content_charset() or "utf-8"
        parser = _PageParser()
        parser.feed(raw.decode(charset, errors="replace"))
        if len(parser.text) < 180:
            return {"url": final_url, "status": "skipped", "reason": "page has too little readable body content", "links": parser.links}
        title = parser.title[:300] or final_url
        return {"url": final_url, "status": "ready", "title": title, "content": parser.text[:100000], "knowledge_type": _knowledge_type(final_url, title, parser.text), "links": parser.links}
    except HTTPError as error:
        return {"url": url, "status": "failed", "reason": f"HTTP {error.code}"}
    except (URLError, TimeoutError, OSError) as error:
        return {"url": url, "status": "failed", "reason": str(error.reason if isinstance(error, URLError) else error)[:300]}
    except Exception as error:  # keep one faulty page from aborting a site's crawl
        return {"url": url, "status": "failed", "reason": str(error)[:300]}


def crawl_site(domain: str, max_pages: int = 20) -> list[dict[str, object]]:
    """Recursively discover and fetch relevant same-domain knowledge pages.

    A batch is concurrent, while discovery stays breadth-first.  This means a
    product catalogue linked from a category page is included as well as the
    links initially visible on the homepage.
    """
    home = _base_url(domain)
    host = urlsplit(home).hostname or ""
    if not _is_public_hostname(host):
        raise ValueError("website domain does not resolve to a public internet address")

    results: list[dict[str, object]] = []
    seen = {home}
    pending = [home]
    while pending and len(results) < max_pages:
        batch = pending[: min(6, max_pages - len(results))]
        pending = pending[len(batch):]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as executor:
            pages = list(executor.map(lambda item: _read_page(item, host), batch))
        results.extend(pages)
        for page in pages:
            for href in page.get("links", []):
                candidate = _canonical(urljoin(str(page["url"]), str(href)))
                if candidate not in seen and _is_same_site(candidate, host) and _is_candidate(candidate):
                    seen.add(candidate)
                    pending.append(candidate)
        pending.sort(key=_priority)
    return results
