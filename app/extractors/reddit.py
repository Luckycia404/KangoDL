from __future__ import annotations

import html
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from curl_cffi import requests as curl_requests
from app.extractors.strategy import strategy_info

HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "np.reddit.com", "redd.it", "www.redd.it"}
UA = os.getenv("REDDIT_USER_AGENT", "KangoDL/0.8.0 (public media extractor; contact: admin@officialkango.online)")
MAX_PAGE_BYTES = 20 * 1024 * 1024
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
_TOKEN = None
_TOKEN_EXPIRES = 0.0
_TOKEN_LOCK = threading.Lock()


def is_reddit_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in HOSTS
    except Exception:
        return False


def _session():
    return curl_requests.Session(impersonate="chrome")


def _oauth_token(session):
    global _TOKEN, _TOKEN_EXPIRES
    client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Reddit requires OAuth for requests from this hosted VPS. Configure REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET in /opt/kangodl-engine/.env, then restart KangoDL.")
    with _TOKEN_LOCK:
        if _TOKEN and time.time() < _TOKEN_EXPIRES - 60:
            return _TOKEN
        response = session.post("https://www.reddit.com/api/v1/access_token", auth=(client_id, client_secret), data={"grant_type": "client_credentials"}, headers={"User-Agent": UA}, timeout=25)
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError("Reddit OAuth did not return an access token. Check the Reddit app credentials and app type.")
        _TOKEN = token
        _TOKEN_EXPIRES = time.time() + int(data.get("expires_in") or 3600)
        return _TOKEN


def _canonical_post_url(url: str, session):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in {"redd.it", "www.redd.it"}:
        response = session.get(url, allow_redirects=True, headers={"User-Agent": UA}, timeout=25)
        response.raise_for_status()
        url = response.url
        parsed = urlparse(url)
    if parsed.hostname not in {"reddit.com", "www.reddit.com", "old.reddit.com", "np.reddit.com"}:
        raise RuntimeError("This Reddit link did not resolve to a Reddit post.")
    match = re.search(r"/comments/([a-z0-9]+)(?:/([^/?#]+))?", parsed.path, re.I)
    if not match:
        # Share URLs can resolve through Reddit's public oEmbed canonical URL.
        embed = session.get("https://www.reddit.com/oembed", params={"url": url}, headers={"User-Agent": UA}, timeout=20)
        embed.raise_for_status()
        canonical = re.search(r'href="(https://www\.reddit\.com/r/[^\"]+/comments/[^\"]+)', html.unescape(embed.json().get("html", "")))
        if canonical:
            url = canonical.group(1)
            parsed = urlparse(url)
            match = re.search(r"/comments/([a-z0-9]+)(?:/([^/?#]+))?", parsed.path, re.I)
    if not match:
        raise RuntimeError("Use a Reddit post URL containing /comments/{post_id}/. Subreddit pages and user profiles are not supported.")
    post_id = match.group(1)
    path = parsed.path
    return post_id, urlunparse(("https", "www.reddit.com", path.rstrip("/") + "/", "", "", ""))


def _post_data(url: str):
    session = _session()
    try:
        post_id, canonical_url = _canonical_post_url(url, session)
        token = _oauth_token(session)
        api = session.get(f"https://oauth.reddit.com/comments/{post_id}.json", params={"raw_json": 1, "limit": 1}, headers={"Authorization": f"Bearer {token}", "User-Agent": UA}, timeout=30)
        if api.status_code == 401:
            global _TOKEN, _TOKEN_EXPIRES
            _TOKEN = None
            _TOKEN_EXPIRES = 0
            token = _oauth_token(session)
            api = session.get(f"https://oauth.reddit.com/comments/{post_id}.json", params={"raw_json": 1, "limit": 1}, headers={"Authorization": f"Bearer {token}", "User-Agent": UA}, timeout=30)
        api.raise_for_status()
        if len(api.content) > MAX_PAGE_BYTES:
            raise RuntimeError("Reddit post metadata exceeded the response size limit.")
        payload = api.json()
        listing = payload[0].get("data", {}).get("children", []) if isinstance(payload, list) and payload else []
        if not listing:
            raise RuntimeError("Reddit returned no accessible post data.")
        post = listing[0].get("data", {})
        if not post:
            raise RuntimeError("The Reddit post is unavailable or has been removed.")
        return _normalise_post(post, canonical_url)
    finally:
        session.close()


def _normalise_post(post: dict, canonical_url: str):
    # Crossposts often retain the original media in crosspost_parent_list.
    media_post = post
    if not (post.get("secure_media") or post.get("media") or post.get("media_metadata")):
        parents = post.get("crosspost_parent_list") or []
        if parents:
            media_post = parents[0]
    secure = media_post.get("secure_media") or media_post.get("media") or {}
    reddit_video = secure.get("reddit_video") or (media_post.get("preview") or {}).get("reddit_video_preview") or {}
    video = None
    if reddit_video.get("fallback_url") or reddit_video.get("hls_url") or reddit_video.get("dash_url"):
        video = {
            "fallback_url": html.unescape(reddit_video.get("fallback_url") or "").replace("&amp;", "&"),
            "hls_url": html.unescape(reddit_video.get("hls_url") or "").replace("&amp;", "&"),
            "dash_url": html.unescape(reddit_video.get("dash_url") or "").replace("&amp;", "&"),
            "duration": reddit_video.get("duration"),
            "width": reddit_video.get("width"),
            "height": reddit_video.get("height"),
            "is_gif": bool(reddit_video.get("is_gif")),
        }
    images = []
    gallery = post.get("gallery_data") or media_post.get("gallery_data") or {}
    metadata = post.get("media_metadata") or media_post.get("media_metadata") or {}
    for item in gallery.get("items") or []:
        entry = metadata.get(item.get("media_id"), {})
        source = entry.get("s") or {}
        image_url = source.get("u") or source.get("gif") or source.get("mp4")
        if image_url:
            images.append(html.unescape(image_url).replace("&amp;", "&"))
    if not images:
        preview = media_post.get("preview") or {}
        for item in preview.get("images") or []:
            source = item.get("source") or {}
            if source.get("url"):
                images.append(html.unescape(source["url"]).replace("&amp;", "&"))
    if not images and not video:
        candidate = post.get("url_overridden_by_dest") or post.get("url")
        if isinstance(candidate, str) and urlparse(candidate).scheme in {"http", "https"}:
            if urlparse(candidate).hostname in {"i.redd.it", "preview.redd.it"}:
                images.append(candidate)
    images = list(dict.fromkeys(x for x in images if x.startswith("https://")))
    return {
        "id": post.get("id"),
        "title": html.unescape(str(post.get("title") or "Reddit media"))[:500],
        "author": post.get("author"),
        "subreddit": post.get("subreddit"),
        "permalink": canonical_url,
        "thumbnail": html.unescape(post.get("thumbnail") or "") if str(post.get("thumbnail") or "").startswith("https://") else (images[0] if images else None),
        "duration_seconds": video.get("duration") if video else None,
        "video": video,
        "images": images,
        "score": post.get("score"),
        "_extraction_method": "reddit-oauth-public-post-json",
    }


def extract_reddit_info(url: str) -> dict:
    post = _post_data(url)
    formats = []
    if post["video"]:
        formats.append({"format_id": "reddit-video", "ext": "mp4", "has_video": True, "has_audio": bool(post["video"].get("hls_url")), "height": post["video"].get("height"), "width": post["video"].get("width")})
        if post["video"].get("hls_url"):
            formats.append({"format_id": "reddit-audio", "ext": "mp3", "has_video": False, "has_audio": True})
    if post["images"]:
        formats.append({"format_id": "reddit-images", "ext": "zip" if len(post["images"]) > 1 else "jpg", "has_video": False, "has_audio": False})
    if not formats:
        raise RuntimeError("This Reddit post does not contain supported public video or image media. External links and text-only posts are not downloadable.")
    return {
        "ok": True, "extractor": "kangodl-reddit-native", "id": post["id"], "title": post["title"],
        "author": post["author"], "subreddit": post["subreddit"], "thumbnail": post["thumbnail"],
        "duration_seconds": post["duration_seconds"], "media_type": "video" if post["video"] else "image",
        "item_count": len(post["images"]) if post["images"] else 1, "formats": formats, "format_count": len(formats),
        "source": "Reddit OAuth public post JSON", "extraction_strategy": strategy_info("reddit-oauth-public-post-json", "reddit-public-oembed-metadata", post["_extraction_method"]),
    }


def _download_url(url: str, destination: Path, referer: str):
    if not url.startswith("https://"):
        raise RuntimeError("Reddit returned a non-HTTPS media URL; refusing the download.")
    session = _session()
    total = 0
    try:
        response = session.get(url, headers={"User-Agent": UA, "Referer": referer}, stream=True, timeout=60)
        response.raise_for_status()
        expected = int(response.headers.get("Content-Length") or 0)
        if expected > MAX_MEDIA_BYTES:
            raise RuntimeError("Reddit media exceeds the 2 GiB safety limit.")
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise RuntimeError("Reddit media exceeds the 2 GiB safety limit.")
                output.write(chunk)
    finally:
        session.close()
    if total < 512:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Reddit returned an empty media response.")
    return total


def _download_hls(url: str, folder: Path, referer: str):
    session = _session()
    headers = {"User-Agent": UA, "Referer": referer}
    total_bytes = 0
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        text = response.text
        if "#EXT-X-STREAM-INF" in text:
            lines = text.splitlines()
            choices = []
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    bandwidth = int(re.search(r"BANDWIDTH=(\d+)", line).group(1)) if re.search(r"BANDWIDTH=(\d+)", line) else 0
                    next_line = next((x.strip() for x in lines[i + 1:] if x.strip() and not x.startswith("#")), None)
                    if next_line:
                        from urllib.parse import urljoin
                        choices.append((bandwidth, urljoin(url, next_line)))
            if not choices:
                raise RuntimeError("Reddit HLS master playlist contained no media variants.")
            return _download_hls(max(choices)[1], folder, referer)
        from urllib.parse import urljoin
        local_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#EXT-X-KEY:"):
                uri_match = re.search(r'URI="([^"]+)"', stripped)
                if uri_match:
                    key_url = urljoin(url, uri_match.group(1))
                    key_response = session.get(key_url, headers=headers, timeout=30)
                    key_response.raise_for_status()
                    key_path = folder / "hls-key.bin"
                    key_path.write_bytes(key_response.content)
                    local_lines.append(stripped.replace(uri_match.group(1), key_path.name))
                else:
                    local_lines.append(stripped)
            elif stripped.startswith("#EXT-X-MAP:"):
                uri_match = re.search(r'URI="([^"]+)"', stripped)
                if uri_match:
                    init_url = urljoin(url, uri_match.group(1))
                    init_response = session.get(init_url, headers=headers, timeout=30)
                    init_response.raise_for_status()
                    init_path = folder / "hls-init.mp4"
                    init_path.write_bytes(init_response.content)
                    local_lines.append(stripped.replace(uri_match.group(1), init_path.name))
                else:
                    local_lines.append(stripped)
            elif stripped.startswith("#"):
                local_lines.append(stripped)
            else:
                segment_url = urljoin(url, stripped)
                segment = session.get(segment_url, headers=headers, timeout=60)
                segment.raise_for_status()
                total_bytes += len(segment.content)
                if total_bytes > MAX_MEDIA_BYTES:
                    raise RuntimeError("Reddit HLS media exceeds the 2 GiB safety limit.")
                segment_path = folder / f"segment-{sum(1 for x in local_lines if x.startswith('segment-')):06d}.ts"
                segment_path.write_bytes(segment.content)
                local_lines.append(segment_path.name)
        if not any(not x.startswith("#") for x in local_lines):
            raise RuntimeError("Reddit HLS playlist contained no media segments.")
        playlist = folder / "local.m3u8"
        playlist.write_text("\n".join(["#EXTM3U"] + local_lines) + "\n", encoding="utf-8")
        return playlist
    finally:
        session.close()


def download_reddit(url: str, destination: Path, mode: str, output: str, progress_hook=None, format_id: str | None = None) -> dict:
    post = _post_data(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode in {"video", "audio"}:
        video = post["video"]
        if not video:
            raise RuntimeError("This Reddit post does not contain a Reddit-hosted video.")
        if mode == "audio" and output != "mp3":
            raise RuntimeError("Reddit audio output must be MP3.")
        if mode == "video" and output not in {"mp4", "webm"}:
            raise RuntimeError("Reddit video output must be MP4 or WebM.")
        with tempfile.TemporaryDirectory(prefix="kangodl-reddit-") as temp:
            folder = Path(temp)
            source = folder / "reddit-source.mp4"
            if video.get("hls_url"):
                playlist = _download_hls(video["hls_url"], folder, post["permalink"])
                subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-allowed_extensions", "ALL", "-i", str(playlist), "-c", "copy", str(source)], check=True, timeout=1800)
            elif video.get("fallback_url") and mode == "video":
                _download_url(video["fallback_url"], source, post["permalink"])
            else:
                raise RuntimeError("Reddit did not expose a public audio/video HLS stream for this post.")
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(source)], check=True, capture_output=True, text=True, timeout=60)
            streams = json.loads(probe.stdout).get("streams", [])
            has_video = any(x.get("codec_type") == "video" for x in streams)
            has_audio = any(x.get("codec_type") == "audio" for x in streams)
            if mode == "audio" and not has_audio:
                raise RuntimeError("The Reddit video stream does not contain a downloadable audio track.")
            if mode == "video" and not has_video:
                raise RuntimeError("The Reddit response did not contain a valid video stream.")
            if mode == "audio":
                args = ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
            elif output == "webm":
                args = ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-crf", "32", "-b:v", "0", "-c:a", "libopus"] if has_audio else ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-crf", "32", "-b:v", "0"]
            else:
                args = ["-c", "copy", "-movflags", "+faststart"]
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)] + args + [str(destination)], check=True, timeout=1800)
        if not destination.is_file() or destination.stat().st_size < 512:
            raise RuntimeError("Reddit media processing did not produce a valid output file.")
        return {"bytes": destination.stat().st_size, "format_id": "reddit-hls" if video.get("hls_url") else "reddit-fallback-mp4", "title": post["title"], "author": post["author"], "thumbnail": post["thumbnail"], "duration_seconds": post["duration_seconds"], "extraction_method": "reddit-oauth-public-post-json"}
    if mode in {"image", "photos"}:
        images = post["images"]
        if not images:
            raise RuntimeError("This Reddit post does not contain downloadable public images.")
        if mode == "image" and len(images) != 1:
            raise RuntimeError("This Reddit post is a gallery; use photos mode with ZIP output.")
        if mode == "image":
            _download_url(images[0], destination, post["permalink"])
            return {"bytes": destination.stat().st_size, "format_id": "reddit-image", "title": post["title"], "thumbnail": post["thumbnail"], "media_type": "image", "item_count": 1, "extraction_method": "reddit-oauth-public-post-json"}
        if output != "zip":
            raise RuntimeError("Reddit gallery downloads require ZIP output.")
        with tempfile.TemporaryDirectory(prefix="kangodl-reddit-gallery-") as temp:
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                for index, image_url in enumerate(images, 1):
                    suffix = Path(urlparse(image_url).path).suffix or ".jpg"
                    item = Path(temp) / f"image-{index:03d}{suffix}"
                    _download_url(image_url, item, post["permalink"])
                    archive.write(item, item.name)
        return {"bytes": destination.stat().st_size, "format_id": "reddit-gallery-zip", "title": post["title"], "thumbnail": post["thumbnail"], "media_type": "photos", "item_count": len(images), "extraction_method": "reddit-oauth-public-post-json"}
    raise RuntimeError("Reddit supports public video, audio, single image, and image gallery downloads only.")
