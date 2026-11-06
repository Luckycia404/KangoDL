from __future__ import annotations

import html
import json
import re
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from curl_cffi import requests as curl_requests
from app.extractors.strategy import strategy_info

HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_ITEMS = 20


def is_instagram_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in {"http", "https"} and (host in HOSTS or any(host.endswith("." + d) for d in HOSTS))
    except Exception:
        return False


def _clean_url(value):
    if not isinstance(value, str):
        return None
    value = html.unescape(value).replace("\\u0026", "&").replace("\\/", "/").strip()
    return value if value.startswith("https://") else None


def _meta(page: str, key: str):
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        prop = re.search(r"(?:property|name)=[\"']" + re.escape(key) + r"[\"']", tag, re.I)
        content = re.search(r"content=[\"']([^\"']*)", tag, re.I)
        if prop and content:
            return html.unescape(content.group(1))
    return None


def _json_scripts(page: str):
    values = []
    patterns = [
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, page, re.I | re.S):
            try:
                values.append(json.loads(html.unescape(match)))
            except Exception:
                pass
    for marker in ("window._sharedData =", "window.__additionalDataLoaded(", '"xdt_shortcode_media"'):
        pos = page.find(marker)
        if pos < 0:
            continue
        start = page.find("{", pos + len(marker))
        if start < 0:
            continue
        depth = 0
        quoted = escaped = False
        for i in range(start, min(len(page), start + 2_000_000)):
            c = page[i]
            if quoted:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    quoted = False
            else:
                if c == '"':
                    quoted = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            values.append(json.loads(page[start:i + 1]))
                        except Exception:
                            pass
                        break
    return values


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_url(value):
    if isinstance(value, str):
        return _clean_url(value)
    if isinstance(value, list):
        for item in value:
            found = _first_url(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("url", "src", "uri"):
            found = _first_url(value.get(key))
            if found:
                return found
    return None


def _media_from_node(node: dict):
    video = bool(node.get("is_video") or node.get("media_type") == 2 or node.get("product_type") in {"clips", "igtv"})
    video_url = None
    if video:
        versions = node.get("video_versions") or node.get("video_resources") or []
        if isinstance(versions, list):
            versions = sorted(versions, key=lambda x: (x.get("width") or 0) * (x.get("height") or 0), reverse=True)
        video_url = _first_url(versions) or _clean_url(node.get("video_url"))
        if not video_url and isinstance(node.get("video_dash_manifest"), str):
            # Some public Instagram pages expose a direct MP4 BaseURL only in the DASH MPD.
            match = re.search(r"<BaseURL>(.*?)</BaseURL>", node["video_dash_manifest"], re.I | re.S)
            if match:
                video_url = _clean_url(html.unescape(match.group(1).strip()))
    image_url = None
    image_data = node.get("image_versions2") or node.get("display_resources") or node.get("thumbnail_resources")
    if isinstance(image_data, dict):
        candidates = image_data.get("candidates") or image_data.get("items") or []
        if isinstance(candidates, list):
            candidates = sorted(candidates, key=lambda x: (x.get("width") or 0) * (x.get("height") or 0), reverse=True)
        image_url = _first_url(candidates)
    if not image_url:
        image_url = _clean_url(node.get("display_url") or node.get("display_uri") or node.get("thumbnail_src"))
    if not image_url and not video_url:
        return None
    return {
        "type": "video" if video_url else "image",
        "video_url": video_url,
        "image_url": image_url,
        "width": node.get("original_width") or node.get("width"),
        "height": node.get("original_height") or node.get("height"),
        "duration_seconds": node.get("video_duration") or node.get("duration"),
    }


def _extract_media(page: str):
    objects = [obj for root in _json_scripts(page) for obj in _walk(root)]
    roots = []
    for obj in objects:
        carousel = obj.get("carousel_media")
        if not isinstance(carousel, list):
            sidecar = obj.get("edge_sidecar_to_children")
            if isinstance(sidecar, dict):
                carousel = [edge.get("node", {}) for edge in sidecar.get("edges", []) if isinstance(edge, dict)]
        if isinstance(carousel, list) and carousel:
            roots.append(carousel[:MAX_ITEMS])
    if roots:
        raw_items = max(roots, key=len)
        items = []
        for node in raw_items:
            if not isinstance(node, dict):
                continue
            media = _media_from_node(node)
            if media:
                items.append(media)
        if items:
            return items
    candidates = []
    for obj in objects:
        media = _media_from_node(obj)
        if media:
            candidates.append(media)
    if candidates:
        # Prefer a video rendition and avoid duplicate nested thumbnail objects.
        videos = [x for x in candidates if x["video_url"]]
        return [videos[0] if videos else candidates[0]]
    video_url = _clean_url(_meta(page, "og:video") or _meta(page, "og:video:secure_url"))
    image_url = _clean_url(_meta(page, "og:image"))
    if video_url or image_url:
        return [{"type": "video" if video_url else "image", "video_url": video_url, "image_url": image_url, "width": None, "height": None, "duration_seconds": None}]
    return []



def _shortcode_to_id(shortcode: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    value = 0
    for char in shortcode:
        value = (value << 6) | alphabet.index(char)
    return str(value)


def _get_post_api(url: str):
    """Fallback resolver using Instagram's public web media-info endpoint."""
    parsed = urlparse(url)
    match = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise RuntimeError("Instagram media API fallback requires a direct post shortcode.")
    media_id = _shortcode_to_id(match.group(1))
    session = curl_requests.Session(impersonate="chrome")
    session.headers.update({**HEADERS, "Accept": "application/json, text/plain, */*", "Referer": "https://www.instagram.com/", "X-IG-App-ID": "936619743392459", "X-ASBD-ID": "129477"})
    response = session.get(f"https://www.instagram.com/api/v1/media/{media_id}/info/", timeout=30)
    response.raise_for_status()
    payload = response.json()
    items = []
    for item in (payload.get("items") or []):
        media = _media_from_node(item)
        if media: items.append(media)
    if not items:
        raise RuntimeError("Instagram media-info API did not expose a public media URL.")
    return session, {"id": match.group(1), "title": "Instagram media", "description": "", "thumbnail": items[0].get("image_url"), "items": items[:MAX_ITEMS], "url": url, "_extraction_method": "instagram-media-info-api"}


def _get_post(url: str):
    if not is_instagram_url(url):
        raise RuntimeError("Not an Instagram URL.")
    parsed = urlparse(url)
    if not re.match(r"^/(?:[A-Za-z0-9._]+/)?(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?$", parsed.path):
        raise RuntimeError("Use a direct Instagram post, Reel, or IGTV URL. Stories and profile pages are not supported.")
    # Instagram serves a reduced logged-out shell to generic HTTP clients.
    # curl_cffi performs a real browser TLS handshake while keeping requests-style APIs.
    session = curl_requests.Session(impersonate="chrome")
    session.headers.update(HEADERS)
    try:
        response = session.get(url, allow_redirects=True, timeout=40)
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"Instagram public page request failed: {exc}") from exc
    if len(response.content) > 20_000_000:
        raise RuntimeError("Instagram page exceeded the metadata size limit.")
    page = response.text
    if "login" in response.url.lower() and "instagram.com" in response.url.lower():
        raise RuntimeError("Instagram redirected this post to a login page. Only publicly accessible posts can be downloaded.")
    items = _extract_media(page)
    if not items:
        lowered = page.lower()
        if any(marker in lowered for marker in ("login_required", "checkpoint_required", "challenge_required", "please log in to instagram", "log in to view")):
            raise RuntimeError("Instagram requires login or verification for this post. KangoDL will not bypass that restriction.")
        try:
            return _get_post_api(url)
        except Exception as exc:
            raise RuntimeError("Instagram page and media-info fallback both failed: " + str(exc)) from exc
    title = _meta(page, "og:title") or "Instagram media"
    description = _meta(page, "og:description") or ""
    canonical = _meta(page, "og:url") or response.url
    shortcode = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", urlparse(canonical).path)
    return session, {
        "_extraction_method": "instagram-public-page-json",
        "id": shortcode.group(1) if shortcode else "",
        "title": title[:500],
        "description": description[:5000],
        "thumbnail": items[0].get("image_url"),
        "items": items[:MAX_ITEMS],
        "url": canonical,
        "_extraction_method": "instagram-public-page-json",
    }


def extract_instagram_info(url: str) -> dict:
    _, post = _get_post(url)
    items = post["items"]
    has_video = any(x["video_url"] for x in items)
    # Video nodes commonly include a poster/thumbnail URL. That is useful for
    # previews, but it must not be advertised as a downloadable photo format.
    has_image = any(x["type"] == "image" for x in items)
    formats = []
    if has_video:
        formats.append({"format_id": "instagram-video-original", "ext": "mp4", "has_video": True, "has_audio": True})
        formats.append({"format_id": "instagram-audio", "ext": "mp3", "has_video": False, "has_audio": True})
    if has_image:
        formats.append({"format_id": "instagram-image-original", "ext": "jpg", "has_video": False, "has_audio": False})
    if len(items) > 1:
        formats.append({"format_id": "instagram-carousel", "ext": "zip", "has_video": False, "has_audio": False, "item_count": len(items)})
    return {
        "ok": True,
        "extractor": "kangodl-instagram-native",
        "id": post["id"],
        "title": post["title"],
        "description": post["description"],
        "thumbnail": post["thumbnail"],
        "media_type": "carousel" if len(items) > 1 else items[0]["type"],
        "item_count": len(items),
        "formats": formats,
        "format_count": len(formats),
        "source": "KangoDL dual-path Instagram resolver",
        "extraction_strategy": strategy_info("instagram-public-page-json", "instagram-media-info-api", post.get("_extraction_method", "instagram-public-page-json")),
    }


def _download_item(session, media_url: str, destination: Path, referer: str, progress_hook=None, total_state=None):
    if not media_url:
        raise RuntimeError("Instagram did not expose a downloadable URL for this carousel item.")
    total_state = total_state if total_state is not None else {"bytes": 0}
    total = 0
    # Instagram CDN responses can stall at the end of a streamed transfer when
    # fetched through curl_cffi. A bounded, complete response avoids partial MP4s.
    response = session.get(media_url, headers={"Referer": referer, "Accept": "*/*"}, timeout=120)
    try:
        response.raise_for_status()
        expected = int(response.headers.get("Content-Length") or 0)
        if expected > MAX_BYTES:
            raise RuntimeError("Instagram media exceeds the 2 GiB safety limit.")
        payload = response.content
        total = len(payload)
        total_state["bytes"] += total
        if total_state["bytes"] > MAX_BYTES:
            raise RuntimeError("Instagram media exceeds the 2 GiB safety limit.")
        if not payload:
            raise RuntimeError("Instagram returned an empty media file.")
        destination.write_bytes(payload)
        if progress_hook:
            progress_hook({"status": "downloading", "downloaded_bytes": total_state["bytes"], "total_bytes": expected or total})
    finally:
        response.close()
    if not total:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Instagram returned an empty media file.")
    return total


def download_instagram(url: str, destination: Path, mode: str, output: str, progress_hook=None) -> dict:
    session, post = _get_post(url)
    items = post["items"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "photos" or output == "zip":
        if len(items) < 2:
            raise RuntimeError("This Instagram post is not a multi-item carousel; use video, image, or audio mode.")
        if len(items) > MAX_ITEMS:
            raise RuntimeError("Instagram carousel exceeds the 20-item safety limit.")
        state = {"bytes": 0}
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, item in enumerate(items, 1):
                media_url = item.get("video_url") or item.get("image_url")
                if not media_url:
                    continue
                suffix = ".mp4" if item.get("video_url") else ".jpg"
                temp = destination.with_name(destination.stem + f".{index:02d}.part" + suffix)
                try:
                    _download_item(session, media_url, temp, post["url"], progress_hook, state)
                    archive.write(temp, f"item_{index:02d}{suffix}")
                finally:
                    temp.unlink(missing_ok=True)
        if not destination.exists() or not destination.stat().st_size:
            raise RuntimeError("Instagram carousel did not contain downloadable media.")
        return {"bytes": destination.stat().st_size, "title": post["title"], "thumbnail": post["thumbnail"], "format_id": "instagram-carousel", "media_type": "carousel", "item_count": len(items), "extraction_method": post.get("_extraction_method")}
    if mode == "image":
        if len(items) != 1 or items[0].get("type") != "image" or not items[0].get("image_url"):
            raise RuntimeError("Use ZIP mode to download an Instagram carousel, or select a single-image post.")
        size = _download_item(session, items[0]["image_url"], destination, post["url"], progress_hook)
        return {"bytes": size, "title": post["title"], "thumbnail": post["thumbnail"], "format_id": "instagram-image-original", "media_type": "image", "extraction_method": post.get("_extraction_method")}
    video = next((item for item in items if item.get("video_url")), None)
    if not video:
        raise RuntimeError("This Instagram post does not expose a public video stream.")
    source = destination.with_name(destination.stem + ".source.mp4")
    try:
        size = _download_item(session, video["video_url"], source, post["url"], progress_hook)
        if mode == "audio":
            args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(destination)]
        elif output == "mp4":
            args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-c", "copy", "-movflags", "+faststart", str(destination)]
        else:
            args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-crf", "32", "-b:v", "0", "-c:a", "libopus", str(destination)]
        process = subprocess.run(args, capture_output=True, timeout=1800)
        if process.returncode or not destination.exists() or not destination.stat().st_size:
            raise RuntimeError("Instagram media conversion failed: " + process.stderr.decode(errors="ignore")[-250:])
        return {"bytes": destination.stat().st_size, "source_bytes": size, "title": post["title"], "thumbnail": post["thumbnail"], "format_id": "instagram-video-original", "media_type": "video", "extraction_method": post.get("_extraction_method")}
    finally:
        source.unlink(missing_ok=True)
