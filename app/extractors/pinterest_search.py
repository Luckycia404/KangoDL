from __future__ import annotations

import json
import re
from urllib.parse import urlencode

import requests

BASE = "https://www.pinterest.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def _session() -> tuple[requests.Session, str]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    home = session.get(BASE + "/", timeout=30)
    home.raise_for_status()
    csrf = session.cookies.get("csrftoken", "")
    app_match = re.search(r'"appVersion"\s*:\s*"([^"]+)"', home.text)
    if not csrf:
        raise RuntimeError("Pinterest did not issue a guest-session token; public search is temporarily unavailable.")
    session.headers.update({
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-Appstate": "active",
        "X-CSRFToken": csrf,
        "X-Pinterest-Pws-Handler": "www/search/[scope].js",
        "X-App-Version": app_match.group(1) if app_match else "",
        "Referer": BASE + "/",
    })
    return session, csrf


def _search_page(session: requests.Session, query: str, bookmark: str | None) -> dict:
    source_url = "/search/pins/?q=" + requests.utils.quote(query, safe="")
    options = {"query": query, "scope": "pins", "rs": "typed", "bookmarks": [bookmark or ""]}
    payload = {"options": options, "context": {}}
    endpoint = BASE + "/resource/BaseSearchResource/get/?" + urlencode({
        "source_url": source_url,
        "data": json.dumps(payload, separators=(",", ":")),
    })
    response = session.get(endpoint, timeout=35)
    response.raise_for_status()
    try:
        envelope = response.json().get("resource_response", {})
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("Pinterest returned an unreadable search response.") from exc
    if envelope.get("status") != "success":
        message = envelope.get("message") or "Pinterest did not complete the search."
        raise RuntimeError(str(message)[:220])
    data = envelope.get("data") or {}
    return {"items": data.get("results") or [], "bookmark": envelope.get("bookmark") or None}


def search_pinterest(query: str, limit: int = 20, page: int = 1, cursor: str | None = None) -> dict:
    query = re.sub(r"\s+", " ", (query or "")).strip()
    if not query:
        raise ValueError("Search query is required.")
    if len(query) > 160:
        raise ValueError("Search query is too long.")
    limit = max(1, min(int(limit), 50))
    page = max(1, min(int(page), 20))
    session, _ = _session()
    bookmark = cursor or None
    if not bookmark:
        for _ in range(page - 1):
            previous = _search_page(session, query, bookmark)
            bookmark = previous["bookmark"]
            if not bookmark:
                return {"ok": True, "source": "pinterest", "query": query, "page": page, "count": 0, "next_cursor": None, "results": []}
    data = _search_page(session, query, bookmark)
    results = []
    seen = set()
    for pin in data["items"]:
        pin_id = str(pin.get("id") or "").strip()
        if not pin_id or pin_id in seen:
            continue
        seen.add(pin_id)
        images = pin.get("images") or {}
        image = images.get("orig") or images.get("736x") or images.get("474x") or images.get("236x") or {}
        thumbnail = image.get("url") if isinstance(image, dict) else None
        story = pin.get("story_pin_data") or {}
        video_seconds = story.get("total_video_duration") or 0
        video_flag = bool(video_seconds or pin.get("videos") or pin.get("video"))
        username = (pin.get("pinner") or {}).get("username")
        results.append({
            "id": pin_id,
            "title": (pin.get("title") or pin.get("grid_title") or pin.get("seo_alt_text") or "Pinterest pin").strip(),
            "url": f"{BASE}/pin/{pin_id}/",
            "thumbnail": thumbnail,
            "media_type": "video" if video_flag else "image",
            "creator": username,
            "source_url": pin.get("link") or None,
        })
        if len(results) >= limit:
            break
    return {
        "ok": True,
        "source": "pinterest",
        "query": query,
        "page": page,
        "count": len(results),
        "next_cursor": data["bookmark"],
        "results": results,
    }
