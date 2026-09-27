"""Extract a bounded readable excerpt from a public HTML page."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from scout.page_fetch import Page

MAX_TEXT_CHARS = 12_000
MIN_TEXT_CHARS = 350
_SKIP = {"head", "script", "style", "nav", "footer", "header", "aside", "form", "svg"}
_PAYWALL_JSON = re.compile(
    r'''["']isAccessibleForFree["']\s*:\s*(?:false|["']false["'])''', re.I
)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.all: list[str] = []
        self.main: list[str] = []
        self.article: list[str] = []
        self.paywalled = False

    def handle_starttag(self, tag: str, attrs) -> None:
        self.stack.append(tag)
        if tag == "meta":
            values = {str(key).lower(): str(value or "").lower() for key, value in attrs}
            if (values.get("itemprop") or values.get("name")) == "isaccessibleforfree":
                self.paywalled = self.paywalled or values.get("content") == "false"

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag) :]

    def handle_data(self, data: str) -> None:
        if not data.strip() or any(tag in _SKIP for tag in self.stack):
            return
        self.all.append(data)
        if "main" in self.stack:
            self.main.append(data)
        if "article" in self.stack:
            self.article.append(data)


def extract_text(page: Page) -> str:
    try:
        html = page.body.decode(page.charset or "utf-8", errors="replace")
    except LookupError:
        html = page.body.decode("utf-8", errors="replace")
    parser = _Text()
    parser.feed(html)
    if parser.paywalled or _PAYWALL_JSON.search(html):
        return ""
    for chunks in (parser.article, parser.main, parser.all):
        text = re.sub(r"\s+", " ", " ".join(chunks)).strip()
        if len(text) >= MIN_TEXT_CHARS:
            return text[:MAX_TEXT_CHARS]
    return ""
