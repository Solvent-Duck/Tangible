"""Web provider: allow-listed sources only, https only, no off-list redirects, size and time caps.

Everything fetched is untrusted data. The provider never follows a URL the agent supplied; the
agent names a source and a query string, and the source adapter builds the URLs.
"""

import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

MAX_TEXT_CHARS = 4_000
MAX_BODY_BYTES = 1_000_000
TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 3
MAX_ITEMS = 10

Allow = Callable[[str], bool]


class WebError(Exception):
    """A lookup failure whose message is safe to show the model."""


class FetchError(WebError):
    pass


@dataclass(frozen=True)
class WebItem:
    source: str
    url: str
    title: str
    text: str  # plain text, at most MAX_TEXT_CHARS
    fetched_at: datetime
    sha256: str  # of the raw response body


class WebProvider(Protocol):
    def lookup(self, source: str, query: str) -> list[WebItem]: ...


@dataclass(frozen=True)
class FetchResponse:
    url: str  # final URL after any (allowed) redirects
    body: bytes


class Fetcher(Protocol):
    def fetch(
        self, url: str, allow: Allow, *, max_bytes: int = MAX_BODY_BYTES,
        timeout: float = TIMEOUT_SECONDS,
    ) -> FetchResponse: ...  # fmt: skip


class SourceAdapter(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def domains(self) -> tuple[str, ...]:  # exact hosts (listed subdomains spelled out); https only
        ...

    def urls(self, query: str) -> list[str]: ...
    def parse(self, url: str, body: bytes) -> list[WebItem]: ...


def host_allowed(url: str, domains: tuple[str, ...]) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    return port in (None, 443) and (parts.hostname or "").lower() in {d.lower() for d in domains}


# ---------------------------------------------------------------- HTML -> text

_SKIP = {"script", "style", "noscript", "template", "head", "svg", "iframe", "object"}
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
}  # fmt: skip
_BLOCK = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre",
}  # fmt: skip
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])",
    re.I,
)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._stack: list[tuple[str, bool]] = []  # (tag, hides content)
        self._hidden = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            if tag == "br" and not self._hidden:
                self.parts.append("\n")
            return
        a = {k: (v or "") for k, v in attrs}
        hides = (
            tag in _SKIP
            or "hidden" in a
            or a.get("aria-hidden", "").lower() == "true"
            or bool(_HIDDEN_STYLE.search(a.get("style", "")))
        )
        self._stack.append((tag, hides))
        if hides:
            self._hidden += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK and not self._hidden:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not self._hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                self._hidden -= sum(1 for _, hides in self._stack[i:] if hides)
                del self._stack[i:]
                break
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK and not self._hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        if not self._hidden:
            self.parts.append(data)


def _squash(s: str) -> str:
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in s.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def extract_text(html: str, limit: int = MAX_TEXT_CHARS) -> tuple[str, str]:
    """(title, visible text). Drops scripts, styles, comments and detectably hidden elements."""
    p = _Extractor()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    return _squash("".join(p.title))[:200], _squash("".join(p.parts))[:limit]


def make_item(source: str, url: str, body: bytes, title: str, text: str) -> WebItem:
    return WebItem(
        source, url, title[:200], text[:MAX_TEXT_CHARS],
        datetime.now(UTC), hashlib.sha256(body).hexdigest(),
    )  # fmt: skip


# ---------------------------------------------------------------- fetchers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class UrllibFetcher:
    """Live fetcher. Not used in tests. Follows redirects by hand, checking each hop."""

    def __init__(self, user_agent: str = "tangible-diligence/0.1") -> None:
        self.user_agent = user_agent
        self._opener = urllib.request.build_opener(_NoRedirect)

    def fetch(
        self, url: str, allow: Allow, *, max_bytes: int = MAX_BODY_BYTES,
        timeout: float = TIMEOUT_SECONDS,
    ) -> FetchResponse:  # fmt: skip
        for _ in range(MAX_REDIRECTS + 1):
            if not allow(url):
                raise FetchError("url is not on the allow-list")
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            try:
                with self._opener.open(req, timeout=timeout) as resp:
                    body = resp.read(max_bytes + 1)
            except urllib.error.HTTPError as e:
                loc = e.headers.get("Location") if e.headers else None
                if e.code in (301, 302, 303, 307, 308) and loc:
                    url = urljoin(url, loc)
                    continue
                raise FetchError(f"http status {e.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise FetchError(f"fetch failed: {type(e).__name__}") from None
            if len(body) > max_bytes:
                raise FetchError("response too large")
            return FetchResponse(url, body)
        raise FetchError("too many redirects")


class FixtureFetcher:
    """Serves canned responses from `dir/index.json`.

    index.json maps URL -> {"file": "page.html"} or {"redirect": "https://other/..."}.
    Redirects are checked against `allow` exactly as the live fetcher does.
    """

    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir)
        self.index: dict[str, dict[str, str]] = json.loads(
            (self.dir / "index.json").read_text(encoding="utf-8")
        )

    def fetch(
        self, url: str, allow: Allow, *, max_bytes: int = MAX_BODY_BYTES,
        timeout: float = TIMEOUT_SECONDS,
    ) -> FetchResponse:  # fmt: skip
        for _ in range(MAX_REDIRECTS + 1):
            if not allow(url):
                raise FetchError("url is not on the allow-list")
            entry = self.index.get(url)
            if entry is None:
                raise FetchError("no fixture for url")
            if "redirect" in entry:
                url = entry["redirect"]
                continue
            body = (self.dir / entry["file"]).read_bytes()
            if len(body) > max_bytes:
                raise FetchError("response too large")
            return FetchResponse(url, body)
        raise FetchError("too many redirects")


# ---------------------------------------------------------------- provider


@dataclass
class AllowListedProvider:
    sources: Mapping[str, SourceAdapter]
    fetcher: Fetcher
    max_bytes: int = MAX_BODY_BYTES
    timeout: float = TIMEOUT_SECONDS
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    def lookup(self, source: str, query: str) -> list[WebItem]:
        adapter = self.sources.get(source)
        if adapter is None:
            raise WebError(f"unknown source '{source}'")

        def allow(url: str) -> bool:
            return host_allowed(url, adapter.domains)

        items: list[WebItem] = []
        errors: list[str] = []
        for url in adapter.urls(query):
            if not allow(url):  # adapter misconfiguration: never fetch
                errors.append("adapter produced a url off the allow-list")
                continue
            try:
                resp = self.fetcher.fetch(
                    url, allow, max_bytes=self.max_bytes, timeout=self.timeout
                )
                if not allow(resp.url) or len(resp.body) > self.max_bytes:
                    raise FetchError("response rejected")
                parsed = adapter.parse(resp.url, resp.body)
            except WebError as e:
                errors.append(str(e))
                continue
            now = self.clock()
            items.extend(
                replace(i, text=i.text[:MAX_TEXT_CHARS], title=i.title[:200], fetched_at=now)
                for i in parsed
            )
        if not items and errors:
            raise WebError(f"lookup failed: {errors[0]}")
        return items[:MAX_ITEMS]
