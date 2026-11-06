from __future__ import annotations

import html
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests
from app.extractors.strategy import strategy_info

HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
MAX_PAGE_BYTES = 25_000_000
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024


def is_facebook_url(value: str) -> bool:
    try:
        p = urlparse(value)
        host = (p.hostname or "").lower()
        return p.scheme in {"http", "https"} and (host in HOSTS or any(host.endswith("." + d) for d in HOSTS))
    except Exception:
        return False


def _meta(page: str, key: str):
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        attr = re.search(r"(?:property|name)=[\"']" + re.escape(key) + r"[\"']", tag, re.I)
        content = re.search(r"content=[\"']([^\"']*)[\"']", tag, re.I)
        if attr and content:
            return html.unescape(content.group(1))
    return None


def _clean_media_url(value: str | None):
    if not isinstance(value, str):
        return None
    value = html.unescape(value).replace("\\u0026", "&").replace("\\/", "/").replace("\\\\", "").strip()
    if not value.startswith("https://"):
        return None
    host = (urlparse(value).hostname or "").lower()
    if not (host.endswith("fbcdn.net") or host.endswith("fbsbx.com") or host in {"facebook.com", "www.facebook.com"}):
        return None
    if ".m3u8" in urlparse(value).path.lower():
        return None
    return value


def _json_values(page: str):
    values = []
    for pattern in (
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        r"<script[^>]+type=[\"']application/json[\"'][^>]*>(.*?)</script>",
        r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    ):
        for raw in re.findall(pattern, page, re.I | re.S):
            try:
                values.append(json.loads(html.unescape(raw)))
            except Exception:
                pass
    return values


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _get_public_video(url: str):
    if not is_facebook_url(url):
        raise RuntimeError("Not a Facebook URL.")
    parsed = urlparse(url)
    if not any(x in parsed.path.lower() for x in ("/videos/", "/reel/", "/watch", "/share/v/", "/share/r/")) and parsed.hostname != "fb.watch":
        raise RuntimeError("Use a direct public Facebook video, Reel, Watch, or fb.watch share URL. Profile and private-group pages are not supported.")
    session = curl_requests.Session(impersonate="chrome")
    session.headers.update(HEADERS)
    try:
        response = session.get(url, allow_redirects=True, timeout=45)
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"Facebook public page request failed: {exc}") from exc
    if len(response.content) > MAX_PAGE_BYTES:
        raise RuntimeError("Facebook page exceeded the metadata size limit.")
    page = response.text
    title = _meta(page, "og:title") or _meta(page, "twitter:title") or "Facebook video"
    description = _meta(page, "og:description") or _meta(page, "description") or ""
    thumbnail = _meta(page, "og:image") or _meta(page, "twitter:image")
    canonical = _meta(page, "og:url") or response.url
    candidates = []
    # Public share pages sometimes expose the playable URL directly in OG metadata.
    for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        found = _clean_media_url(_meta(page, key))
        if found:
            candidates.append((found, "facebook-public-og-video"))
    # Meta's public player state commonly contains one of these direct CDN fields.
    patterns = (
        r'"playable_url_quality_hd"\s*:\s*"(https?:\\?/\\?/[^"\\]+)',
        r'"browser_native_hd_url"\s*:\s*"(https?:\\?/\\?/[^"\\]+)',
        r'"playable_url"\s*:\s*"(https?:\\?/\\?/[^"\\]+)',
        r'"browser_native_sd_url"\s*:\s*"(https?:\\?/\\?/[^"\\]+)',
        r'"video_url"\s*:\s*"(https?:\\?/\\?/[^"\\]+)',
    )
    for pattern in patterns:
        for raw in re.findall(pattern, page, re.I):
            found = _clean_media_url(raw)
            if found:
                candidates.append((found, "facebook-player-json"))
    for root in _json_values(page):
        for obj in _walk(root):
            for key in ("playable_url_quality_hd", "browser_native_hd_url", "playable_url", "browser_native_sd_url", "video_url", "contentUrl"):
                found = _clean_media_url(obj.get(key))
                if found:
                    candidates.append((found, "facebook-structured-json"))
    # Prefer HD direct MP4 if Facebook exposed it; do not synthesize or guess CDN URLs.
    unique = []
    seen = set()
    for item in candidates:
        if item[0] not in seen:
            seen.add(item[0]); unique.append(item)
    if not unique:
        lowered = page.lower()
        if any(x in lowered for x in ("login_form", "login_required", "content isn't available", "this content isn't available", "you must log in")):
            raise RuntimeError("Facebook requires login or does not make this post publicly accessible. KangoDL will not bypass that restriction.")
        raise RuntimeError("Facebook did not expose a public direct MP4 URL for this post. Private, login-only, or unsupported player streams are not downloaded.")
    selected = next((x for x in unique if "quality_hd" in x[0] or "hd" in x[0].lower()), unique[0])
    return session, {
        "title": title[:500], "description": description[:5000], "thumbnail": thumbnail,
        "video_url": selected[0], "url": canonical, "_extraction_method": selected[1],
    }


def extract_facebook_info(url: str) -> dict:
    _, post = _get_public_video(url)
    return {
        "ok": True, "extractor": "kangodl-facebook-native", "title": post["title"],
        "description": post["description"], "thumbnail": post["thumbnail"],
        "media_type": "video", "item_count": 1,
        "formats": [
            {"format_id": "facebook-video-original", "ext": "mp4", "has_video": True, "has_audio": True},
            {"format_id": "facebook-video-audio", "ext": "mp3", "has_video": False, "has_audio": True},
        ], "format_count": 2, "source": "KangoDL native Facebook public-page/player resolver",
        "extraction_strategy": strategy_info("public OG/player metadata", "public structured player JSON", post.get("_extraction_method", "public OG/player metadata")),
    }


def download_facebook(url: str, destination: Path, mode: str, output: str, progress_hook=None) -> dict:
    session, post = _get_public_video(url)
    if mode not in {"video", "audio"}:
        raise RuntimeError("Facebook supports public video MP4/WebM or audio MP3 downloads only.")
    if mode == "video" and output not in {"mp4", "webm"}:
        raise RuntimeError("Facebook video output must be MP4 or WebM.")
    if mode == "audio" and output != "mp3":
        raise RuntimeError("Facebook audio output must be MP3.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = destination.with_name(destination.stem + ".source.part.mp4")
    try:
        response = session.get(post["video_url"], headers={"Referer": post["url"], "Accept": "*/*"}, stream=True, timeout=(20, 90))
        try:
            response.raise_for_status()
            expected = int(response.headers.get("Content-Length") or 0)
            if expected > MAX_MEDIA_BYTES:
                raise RuntimeError("Facebook media exceeds the 2 GiB safety limit.")
            total = 0
            with source.open("wb") as out:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_MEDIA_BYTES:
                        raise RuntimeError("Facebook media exceeds the 2 GiB safety limit.")
                    out.write(chunk)
                    if progress_hook:
                        progress_hook({"status": "downloading", "downloaded_bytes": total, "total_bytes": expected})
        finally:
            response.close()
        if total < 1024:
            raise RuntimeError("Facebook returned an empty or incomplete media response.")
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(source)], check=True, capture_output=True, text=True, timeout=60)
        streams = json.loads(probe.stdout).get("streams", [])
        if not any(x.get("codec_type") == "video" for x in streams):
            raise RuntimeError("Facebook response did not contain a valid video stream.")
        if mode == "video" and output == "mp4":
            source.replace(destination)
        else:
            if mode == "audio":
                args = ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
            else:
                args = ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-crf", "32", "-b:v", "0", "-c:a", "libopus"]
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)] + args + [str(destination)], check=True, timeout=1800)
    finally:
        source.unlink(missing_ok=True)
        session.close()
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("Facebook media processing did not produce a valid output file.")
    return {"bytes": destination.stat().st_size, "title": post["title"], "thumbnail": post["thumbnail"], "format_id": "facebook-video-original", "media_type": "video", "extraction_method": post.get("_extraction_method")}
