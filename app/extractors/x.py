from __future__ import annotations

import html
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests
from app.extractors.strategy import strategy_info

HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
MAX_PAGE_BYTES = 20_000_000
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024


def is_x_url(value: str) -> bool:
    try:
        p = urlparse(value)
        host = (p.hostname or "").lower()
        return p.scheme in {"http", "https"} and host in HOSTS and bool(re.search(r"/status/\d+", p.path))
    except Exception:
        return False


def _post_id(value: str) -> str:
    match = re.search(r"/status/(\d+)", urlparse(value).path)
    if not match:
        raise RuntimeError("Use a public X post URL containing /status/ followed by its numeric post ID.")
    return match.group(1)


def _safe_media_url(value):
    if not isinstance(value, str):
        return None
    value = html.unescape(value).replace("\\u0026", "&").replace("\\/", "/").strip()
    if not value.startswith("https://"):
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host != "video.twimg.com" or not parsed.path.lower().endswith(".mp4"):
        return None
    return value


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _from_syndication(post_id: str):
    url = "https://cdn.syndication.twimg.com/tweet-result"
    response = curl_requests.get(url, params={"id": post_id, "lang": "en", "token": "123"}, impersonate="chrome", headers={"User-Agent": UA, "Accept": "application/json"}, timeout=35)
    response.raise_for_status()
    if len(response.content) > MAX_PAGE_BYTES:
        raise RuntimeError("X syndication response exceeded the metadata size limit.")
    data = response.json()
    if str(data.get("id_str") or data.get("id") or "") != post_id:
        raise RuntimeError("X returned a different post ID than requested.")
    candidates = []
    thumbnails = []
    durations = []
    for obj in _walk(data):
        thumb = obj.get("media_url_https") or obj.get("poster") or obj.get("thumbnail_url")
        if isinstance(thumb, str) and thumb.startswith("https://pbs.twimg.com/"):
            thumbnails.append(thumb)
        variants = obj.get("variants")
        if isinstance(variants, list):
            for item in variants:
                if not isinstance(item, dict):
                    continue
                content_type = item.get("content_type") or item.get("type")
                if content_type == "video/mp4" or content_type == "video/mp4; codecs=avc1":
                    media_url = _safe_media_url(item.get("url") or item.get("src"))
                    if media_url:
                        candidates.append((int(item.get("bitrate") or 0), media_url))
        duration = obj.get("duration_millis") or obj.get("durationMs")
        if isinstance(duration, (int, float)) and duration > 0:
            durations.append(float(duration) / 1000)
    # X's legacy mediaDetails structure also carries video_info.variants.
    for item in data.get("mediaDetails") or []:
        if isinstance(item, dict):
            thumb = item.get("media_url_https")
            if isinstance(thumb, str):
                thumbnails.append(thumb)
            info = item.get("video_info") or {}
            if info.get("duration_millis"):
                durations.append(float(info["duration_millis"]) / 1000)
            for variant in info.get("variants") or []:
                if variant.get("content_type") == "video/mp4":
                    media_url = _safe_media_url(variant.get("url"))
                    if media_url:
                        candidates.append((int(variant.get("bitrate") or 0), media_url))
    if not candidates:
        raise RuntimeError("This public X post does not expose a downloadable MP4 video. It may contain photos only, be restricted, or no longer be available.")
    unique = {}
    for bitrate, media_url in candidates:
        unique[media_url] = max(int(bitrate or 0), unique.get(media_url, 0))
    candidates = sorted(((bitrate, media_url) for media_url, bitrate in unique.items()), key=lambda x: x[0], reverse=True)
    user = data.get("user") or {}
    text = data.get("full_text") or data.get("text") or "X video"
    return {
        "id": post_id,
        "title": re.sub(r"\s+", " ", html.unescape(str(text))).strip()[:500] or "X video",
        "author": user.get("screen_name") or user.get("name"),
        "thumbnail": next((x for x in thumbnails if x), None),
        "duration_seconds": max(durations) if durations else None,
        "variants": [{"url": item[1], "bitrate": item[0]} for item in candidates],
        "url": f"https://x.com/{user.get('screen_name') or 'i'}/status/{post_id}",
        "_extraction_method": "x-public-syndication-json",
    }


def _from_public_page(url: str, post_id: str):
    response = curl_requests.get(url, impersonate="chrome", headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=35)
    response.raise_for_status()
    if len(response.content) > MAX_PAGE_BYTES:
        raise RuntimeError("X post page exceeded the metadata size limit.")
    page = response.text
    title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)', page, re.I)
    image_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']*)', page, re.I)
    found = set()
    for raw in re.findall(r'https?:\\?/\\?/video\.twimg\.com[^"\s<>]+?\.mp4(?:\?[^"\s<>\\]*)?', page, re.I):
        media_url = _safe_media_url(raw)
        if media_url:
            found.add(media_url)
    for raw in re.findall(r'https://video\.twimg\.com[^"\s<>]+?\.mp4(?:\?[^"\s<>]*)?', page, re.I):
        media_url = _safe_media_url(raw)
        if media_url:
            found.add(media_url)
    if not found:
        raise RuntimeError("X's public post page did not expose a direct MP4 URL; the post may require login or be restricted.")
    return {
        "id": post_id,
        "title": html.unescape(title_match.group(1))[:500] if title_match else "X video",
        "author": None,
        "thumbnail": html.unescape(image_match.group(1)) if image_match else None,
        "duration_seconds": None,
        "variants": [{"url": x, "bitrate": 0} for x in sorted(found)],
        "url": url,
        "_extraction_method": "x-public-post-html",
    }


def extract_x_post(url: str) -> dict:
    if not is_x_url(url):
        raise RuntimeError("Not a supported X/Twitter status URL.")
    post_id = _post_id(url)
    errors = []
    try:
        post = _from_syndication(post_id)
    except Exception as exc:
        errors.append(f"syndication: {type(exc).__name__}: {str(exc)[:140]}")
        try:
            post = _from_public_page(url, post_id)
        except Exception as fallback_exc:
            errors.append(f"public page: {type(fallback_exc).__name__}: {str(fallback_exc)[:140]}")
            raise RuntimeError("X could not provide public video metadata. " + " | ".join(errors)) from fallback_exc
    post["url"] = url
    return post


def extract_x_info(url: str) -> dict:
    post = extract_x_post(url)
    formats = []
    for index, item in enumerate(post["variants"], 1):
        formats.append({"format_id": f"x-mp4-{index}", "ext": "mp4", "has_video": True, "has_audio": True, "bitrate": item["bitrate"] or None})
    formats.append({"format_id": "x-audio", "ext": "mp3", "has_video": False, "has_audio": True})
    return {
        "ok": True, "extractor": "kangodl-x-native", "id": post["id"], "title": post["title"],
        "author": post.get("author"), "thumbnail": post.get("thumbnail"), "duration_seconds": post.get("duration_seconds"),
        "media_type": "video", "item_count": 1, "formats": formats, "format_count": len(formats),
        "source": "KangoDL native X public syndication/post resolver",
        "extraction_strategy": strategy_info("x-public-syndication-json", "x-public-post-html", post.get("_extraction_method", "x-public-syndication-json")),
    }


def download_x(url: str, destination: Path, mode: str, output: str, progress_hook=None, format_id: str | None = None) -> dict:
    post = extract_x_post(url)
    if mode not in {"video", "audio"}:
        raise RuntimeError("X supports public video/GIF MP4/WebM or audio MP3 downloads only.")
    if mode == "video" and output not in {"mp4", "webm"}:
        raise RuntimeError("X video output must be MP4 or WebM.")
    if mode == "audio" and output != "mp3":
        raise RuntimeError("X audio output must be MP3.")
    variants = post["variants"]
    selected = None
    if format_id and format_id != "x-audio":
        try:
            selected = variants[int(format_id.rsplit("-", 1)[-1]) - 1]
        except Exception:
            raise RuntimeError("The requested X format is not available for this post.")
    if selected is None:
        selected = variants[0]
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = destination.with_name(destination.stem + ".x-source.part.mp4")
    base_headers = {"User-Agent": UA, "Referer": "https://x.com/", "Accept": "*/*", "Accept-Encoding": "identity"}
    candidates = [selected] if format_id else variants
    last_error = None
    chosen = None

    def fetch_variant(candidate):
        source.unlink(missing_ok=True)
        total = 0
        probe_session = curl_requests.Session(impersonate="chrome")
        try:
            probe = probe_session.get(candidate["url"], headers={**base_headers, "Range": "bytes=0-0"}, timeout=20)
            content_range = probe.headers.get("Content-Range", "")
            match = re.fullmatch(r"bytes 0-0/(\d+)", content_range)
            total_size = int(match.group(1)) if probe.status_code == 206 and match else 0
            probe.close()
        finally:
            probe_session.close()
        if total_size:
            if total_size > MAX_MEDIA_BYTES:
                raise RuntimeError("X media exceeds the 2 GiB safety limit.")
            with source.open("wb") as out:
                out.truncate(total_size)
            fd = os.open(source, os.O_WRONLY)
            completed = 0
            chunk_size = 256 * 1024
            try:
                for offset in range(0, total_size, chunk_size):
                    end = min(offset + chunk_size - 1, total_size - 1)
                    response = curl_requests.get(candidate["url"], headers={**base_headers, "Range": f"bytes={offset}-{end}"}, impersonate="chrome", timeout=18)
                    try:
                        expected_range = f"bytes {offset}-{end}/{total_size}"
                        if response.status_code != 206 or response.headers.get("Content-Range") != expected_range:
                            raise RuntimeError(f"X CDN returned an unexpected byte range for {offset}-{end}.")
                        data = response.content
                        if len(data) != end - offset + 1:
                            raise RuntimeError(f"X CDN returned an incomplete range at byte {offset}.")
                    finally:
                        response.close()
                    written = os.pwrite(fd, data, offset)
                    if written != len(data):
                        raise RuntimeError(f"Could not write the full X media range at byte {offset}.")
                    completed += written
                    if progress_hook:
                        progress_hook({"status": "downloading", "downloaded_bytes": completed, "total_bytes": total_size})
            finally:
                os.close(fd)
            if completed != total_size or source.stat().st_size != total_size:
                raise RuntimeError("The X ranged download did not produce the complete media object.")
            total = total_size
        else:
            session = curl_requests.Session(impersonate="chrome")
            try:
                response = session.get(candidate["url"], headers=base_headers, stream=True, timeout=(15, 45))
                try:
                    response.raise_for_status()
                    expected = int(response.headers.get("Content-Length") or 0)
                    if expected > MAX_MEDIA_BYTES:
                        raise RuntimeError("X media exceeds the 2 GiB safety limit.")
                    with source.open("wb") as out:
                        for chunk in response.iter_content(chunk_size=256 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > MAX_MEDIA_BYTES:
                                raise RuntimeError("X media exceeds the 2 GiB safety limit.")
                            out.write(chunk)
                            if progress_hook:
                                progress_hook({"status": "downloading", "downloaded_bytes": total, "total_bytes": expected})
                finally:
                    response.close()
            finally:
                session.close()
        if total < 1024:
            raise RuntimeError("X returned an empty or incomplete media response.")
        validation = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(source)], check=True, capture_output=True, text=True, timeout=60)
        streams = json.loads(validation.stdout).get("streams", [])
        if not any(item.get("codec_type") == "video" for item in streams):
            raise RuntimeError("The X response did not contain a valid video stream.")
        return total

    try:
        for candidate in candidates:
            try:
                total = fetch_variant(candidate)
                chosen = candidate
                break
            except Exception as exc:
                last_error = exc
                source.unlink(missing_ok=True)
                if format_id:
                    raise
        if chosen is None:
            raise RuntimeError(f"X's available MP4 variants could not be downloaded ({type(last_error).__name__}: {last_error}).")
        selected = chosen
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
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("X media processing did not produce a valid output file.")
    return {"bytes": destination.stat().st_size, "title": post["title"], "author": post.get("author"), "thumbnail": post.get("thumbnail"), "duration_seconds": post.get("duration_seconds"), "format_id": f"x-mp4-{variants.index(selected) + 1}", "media_type": "video", "extraction_method": post.get("_extraction_method")}
