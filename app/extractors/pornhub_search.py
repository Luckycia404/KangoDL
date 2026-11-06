from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin

import requests

BASE = "https://www.pornhub.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class _ResultsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.current = None
        self.anchor_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        href = a.get("href", "")
        if tag == "a" and "view_video.php?viewkey=" in href:
            key = re.search(r"viewkey=([A-Za-z0-9]+)", href)
            if key:
                self.current = {
                    "id": key.group(1),
                    "url": urljoin(BASE, href),
                    "title": a.get("title") or "",
                    "thumbnail": None,
                }
                self.anchor_depth = 1
        elif self.current is not None:
            if tag == "a":
                self.anchor_depth += 1
            elif tag == "img" and not self.current.get("thumbnail"):
                self.current["thumbnail"] = a.get("data-src") or a.get("src") or a.get("data-thumb")


    def handle_endtag(self, tag):
        if self.current is not None and tag == "a":
            self.anchor_depth -= 1
            if self.anchor_depth <= 0:
                if self.current.get("title"):
                    self.items.append(self.current)
                self.current = None
                self.anchor_depth = 0


def search_pornhub(query: str, limit: int = 20, page: int = 1) -> dict:
    query = re.sub(r"\s+", " ", (query or "")).strip()
    if not query:
        raise ValueError("Search query is required.")
    if len(query) > 160:
        raise ValueError("Search query is too long.")
    limit = max(1, min(int(limit), 50))
    page = max(1, min(int(page), 100))
    url = f"{BASE}/video/search?search={quote_plus(query)}&page={page}"
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    response = session.get(url, timeout=35, allow_redirects=True)
    response.raise_for_status()
    parser = _ResultsParser()
    parser.feed(response.text)
    results = []
    seen = set()
    for item in parser.items:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        results.append(item)
        if len(results) >= limit:
            break
    return {
        "ok": True,
        "source": "pornhub",
        "query": query,
        "page": page,
        "count": len(results),
        "results": results,
    }
