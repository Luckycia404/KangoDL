from __future__ import annotations

import html
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests
from app.extractors.strategy import strategy_info

HOSTS = {"snapchat.com", "www.snapchat.com", "story.snapchat.com"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
MAX_BYTES = 2 * 1024 * 1024 * 1024


def is_snapchat_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in {"http", "https"} and (host in HOSTS or any(host.endswith("." + d) for d in HOSTS))
    except Exception:
        return False


def _meta(page: str, key: str):
    for tag in re.findall(r'<meta[^>]*>', page, re.I):
        prop = re.search(r"""(?:property|name)=["']""" + re.escape(key) + r"""["']""", tag, re.I)
        content = re.search(r"""content=["']([^"']*)["']""", tag, re.I)
        if prop and content:
            return html.unescape(content.group(1))
    return None


def _get_snap(url: str):
    if not is_snapchat_url(url):
        raise RuntimeError("Not a Snapchat URL.")
    parsed = urlparse(url)
    # Spotlight links are public share pages. Private stories, chat attachments,
    # account-only pages and ephemeral content are deliberately not resolved.
    if not re.search(r"/spotlight/[A-Za-z0-9_-]+(?:/|$)", parsed.path):
        raise RuntimeError("KangoDL currently supports public Snapchat Spotlight links only. Private stories and chat media are not supported.")
    session = curl_requests.Session(impersonate="chrome")
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        response = session.get(url, allow_redirects=True, timeout=45)
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"Snapchat public Spotlight page request failed: {exc}") from exc
    if len(response.content) > 25_000_000:
        raise RuntimeError("Snapchat page exceeded the metadata size limit.")
    page = response.text
    video_url = _meta(page, "og:video:secure_url") or _meta(page, "og:video")
    title = _meta(page, "og:title") or _meta(page, "twitter:title") or "Snapchat Spotlight"
    description = _meta(page, "og:description") or _meta(page, "description") or ""
    thumbnail = _meta(page, "og:image") or _meta(page, "twitter:image")
    canonical = _meta(page, "og:url") or response.url
    extraction_method = "snapchat-og-metadata" if video_url else ""
    if not video_url:
        # Secondary resolver: scan structured JSON/player state rather than OG metadata.
        patterns = [r'"videoUrl"\s*:\s*"(https?:\\?/\\?/[^"\\]+)', r'"playbackUrl"\s*:\s*"(https?:\\?/\\?/[^"\\]+)', r'"contentUrl"\s*:\s*"(https?:\\?/\\?/[^"\\]+)']
        for pattern in patterns:
            match = re.search(pattern, page)
            if match:
                video_url = match.group(1).replace("\\\\/", "/").replace("\\u0026", "&")
                extraction_method = "snapchat-player-json"
                break
    if video_url:
        video_url = html.unescape(video_url).replace("\\u0026", "&").replace("\\/", "/")
    if not video_url or not video_url.startswith("https://"):
        lowered = page.lower()
        if any(x in lowered for x in ("login_required", "sign in to view", "private story", "not available")):
            raise RuntimeError("This Snapchat content is unavailable or requires account access. KangoDL does not bypass those restrictions.")
        raise RuntimeError("Snapchat did not expose a public downloadable video URL for this Spotlight.")
    if not any(host in (urlparse(video_url).hostname or "").lower() for host in ("sc-cdn.net", "snapchat.com")):
        raise RuntimeError("Snapchat returned an unexpected media host; refusing the URL.")
    return session, {
        "title": title[:500], "description": description[:5000],
        "thumbnail": thumbnail, "video_url": video_url, "url": canonical, "_extraction_method": extraction_method or "snapchat-player-json",
    }


def extract_snapchat_info(url: str) -> dict:
    _, snap = _get_snap(url)
    return {
        "ok": True, "extractor": "kangodl-snapchat-native",
        "title": snap["title"], "description": snap["description"],
        "thumbnail": snap["thumbnail"], "media_type": "video", "item_count": 1,
        "formats": [
            {"format_id": "snapchat-spotlight-original", "ext": "mp4", "has_video": True, "has_audio": True},
            {"format_id": "snapchat-spotlight-audio", "ext": "mp3", "has_video": False, "has_audio": True},
        ], "format_count": 2,
        "source": "KangoDL dual-path Snapchat public Spotlight resolver",
        "extraction_strategy": strategy_info("snapchat-og-metadata", "snapchat-player-json", snap.get("_extraction_method", "snapchat-player-json")),
    }


def download_snapchat(url: str, destination: Path, mode: str, output: str, progress_hook=None) -> dict:
    session, snap = _get_snap(url)
    if mode not in {"video", "audio"}:
        raise RuntimeError("Snapchat Spotlight supports video MP4/WebM or audio MP3 downloads only.")
    if mode == "video" and output not in {"mp4", "webm"}:
        raise RuntimeError("Snapchat Spotlight video output must be MP4 or WebM.")
    if mode == "audio" and output != "mp3":
        raise RuntimeError("Snapchat Spotlight audio output must be MP3.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = destination.with_name(destination.stem + ".source.part.mp4")
    try:
        response = session.get(snap["video_url"], headers={"Referer": snap["url"], "Accept": "*/*"}, timeout=180)
        response.raise_for_status()
        size = len(response.content)
        if not size:
            raise RuntimeError("Snapchat returned an empty video stream.")
        if size > MAX_BYTES:
            raise RuntimeError("Snapchat media exceeds the 2 GiB safety limit.")
        source.write_bytes(response.content)
        if progress_hook:
            progress_hook({"status": "downloading", "downloaded_bytes": size, "total_bytes": int(response.headers.get("Content-Length") or size)})
        if mode == "video" and output == "mp4":
            # Validate the downloaded container before exposing it as complete.
            subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name", "-of", "default=nw=1:nk=1", str(source)], check=True, capture_output=True, timeout=60)
            source.replace(destination)
        else:
            if mode == "audio":
                args = ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
            else:
                args = ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-crf", "32", "-b:v", "0", "-c:a", "libopus"]
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)] + args + [str(destination)], check=True, timeout=1800)
    finally:
        source.unlink(missing_ok=True)
        try:
            response.close()
        except Exception:
            pass
    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError("Snapchat media processing did not produce a valid output file.")
    return {
        "bytes": destination.stat().st_size,
        "title": snap["title"], "thumbnail": snap["thumbnail"],
        "format_id": "snapchat-spotlight-original", "media_type": "video", "extraction_method": snap.get("_extraction_method"),
    }
