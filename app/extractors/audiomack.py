from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from curl_cffi import requests as curl_requests
from app.extractors.strategy import strategy_info

API_BASE = "https://api.audiomack.com/v1"
SITE_BASE = "https://audiomack.com"
CONSUMER_KEY = os.getenv("AUDIOMACK_CONSUMER_KEY", "audiomack-web")
# Audiomack's public web client uses OAuth 1.0a. Allow operators to override
# these public-client credentials without changing the extractor source.
CONSUMER_SECRET = os.getenv("AUDIOMACK_CONSUMER_SECRET", "bd8a07e9f23fbe9d808646b730f89b8e")
USER_AGENT = "KangoDL-Engine/0.8 (Audiomack native extractor)"
MAX_BYTES = 2 * 1024 * 1024 * 1024


def is_audiomack_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in {"http", "https"} and (host == "audiomack.com" or host.endswith(".audiomack.com"))
    except Exception:
        return False


def _oauth_params(method: str, url: str, params: dict, body: dict | None = None) -> dict:
    if not CONSUMER_KEY or not CONSUMER_SECRET:
        raise RuntimeError("Audiomack API credentials are not configured.")
    oauth = {
        "oauth_consumer_key": CONSUMER_KEY,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_nonce": secrets.token_urlsafe(18),
        "oauth_version": "1.0",
    }
    pairs = list(params.items()) + list((body or {}).items()) + list(oauth.items())
    encoded = sorted((quote(str(k), safe="~"), quote(str(v), safe="~")) for k, v in pairs)
    normalized = "&".join(f"{key}={value}" for key, value in encoded)
    base_string = "&".join((method.upper(), quote(url, safe="~"), quote(normalized, safe="~")))
    signing_key = quote(CONSUMER_SECRET, safe="~") + "&"
    digest = hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    return oauth


def _api_request(method: str, endpoint: str, params: dict | None = None, body: dict | None = None):
    params = params or {}
    body = body or {}
    url = API_BASE + "/" + endpoint.lstrip("/")
    oauth = _oauth_params(method, url, params, body)
    try:
        response = requests.request(
            method.upper(), url,
            params={**params, **oauth},
            data=body if method.upper() != "GET" else None,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=(15, 45),
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Audiomack API request failed: {exc}") from exc
    if response.status_code >= 400:
        try:
            message = response.json().get("message") or response.text[:180]
        except Exception:
            message = response.text[:180]
        raise RuntimeError(f"Audiomack API returned HTTP {response.status_code}: {message}")
    return response


def _track_from_url(url: str) -> dict:
    if not is_audiomack_url(url):
        raise RuntimeError("Not an Audiomack URL.")
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", parts[0]):
        parts = parts[1:]
    if len(parts) < 3 or parts[1].lower() != "song":
        raise RuntimeError("Use a direct Audiomack song URL in the form audiomack.com/artist/song/track-name.")
    artist_slug, track_slug = parts[0], parts[2]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", artist_slug) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", track_slug):
        raise RuntimeError("Audiomack song URL contains an unsupported artist or track slug.")
    response = _api_request("GET", f"music/song/{artist_slug}/{track_slug}")
    try:
        data = response.json().get("results")
    except Exception as exc:
        raise RuntimeError("Audiomack returned invalid track metadata.") from exc
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError("Audiomack did not return track metadata for this URL.")
    return data



def _track_from_page(url: str) -> dict:
    """Fallback metadata resolver: parse Audiomack's public Next.js RSC page state."""
    session = curl_requests.Session(impersonate="chrome")
    response = session.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36", "Accept": "text/html,*/*"}, timeout=35)
    response.raise_for_status(); page = response.text
    if len(page) > 15_000_000: raise RuntimeError("Audiomack page exceeded metadata size limit.")
    target_slug = urlparse(url).path.rstrip("/").split("/")[-1]
    def balanced_object(text: str, start: int):
        depth = 0; quoted = False; escaped = False
        for index in range(start, min(len(text), start + 1_000_000)):
            char = text[index]
            if quoted:
                if escaped: escaped = False
                elif char == "\\": escaped = True
                elif char == '"': quoted = False
                continue
            if char == '"': quoted = True
            elif char == "{": depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        return None
    for raw in re.findall(r'<script[^>]*>(.*?)</script>', page, re.I | re.S):
        if "self.__next_f.push" not in raw or len(raw) > 8_000_000: continue
        for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)', raw, re.S):
            try: packet = __import__("json").loads(match.group(1))
            except Exception: continue
            if not isinstance(packet, list) or len(packet) < 2 or not isinstance(packet[1], str): continue
            payload = packet[1]
            slug_marker = '"urlSlug":"' + target_slug + '"'
            slug_pos = payload.find(slug_marker)
            if slug_pos < 0: continue
            music_marker = '"music":{'
            music_pos = payload.rfind(music_marker, 0, slug_pos)
            if music_pos < 0: continue
            object_start = payload.find("{", music_pos + len('"music":'))
            fragment = balanced_object(payload, object_start)
            if not fragment: continue
            try: music = __import__("json").loads(fragment)
            except Exception: continue
            if music.get("urlSlug") != target_slug: continue
            uploader = music.get("uploadedBy") if isinstance(music.get("uploadedBy"), dict) else {}
            return {
                "id": str(music.get("id") or music.get("songId") or target_slug),
                "title": music.get("title") or "Audiomack track",
                "artist": music.get("artistName") or uploader.get("name") or "",
                "uploader": {"id": uploader.get("id"), "name": uploader.get("name") or music.get("artistName"), "url_slug": uploader.get("slug")},
                "url_slug": music.get("urlSlug"),
                "image": music.get("coverUrl") or music.get("image"),
                "duration": music.get("duration") or 0,
                "description": music.get("description") or "",
                "genre": music.get("genre") or "",
                "private": "yes" if music.get("isPrivate") else "no",
                "stream_only": "no",
                "premium_user_only": "yes" if music.get("isPremiumOnly") else "no",
                "premium_download": "yes" if music.get("isPremiumOnly") else "no",
                "geo_restricted": bool(music.get("isBlockedInUserCountry") or music.get("isGeoLocked")),
                "geo_lock": "yes" if music.get("isGeoLocked") else "no",
                "status": music.get("status") or "complete",
            }
    raise RuntimeError("Audiomack public page did not expose a parseable track object in its Next.js page state.")


def _public_track(data: dict):
    if str(data.get("private", "no")).lower() == "yes":
        raise RuntimeError("This Audiomack track is private.")
    if str(data.get("stream_only", "no")).lower() == "yes":
        raise RuntimeError("This track is marked stream-only and is not available for download.")
    if str(data.get("premium_user_only", "no")).lower() == "yes" or str(data.get("premium_download", "no")).lower() == "yes":
        raise RuntimeError("This track requires Audiomack Premium access; KangoDL will not bypass that restriction.")
    if data.get("geo_restricted") is True or str(data.get("geo_lock", "no")).lower() not in {"", "no", "false", "0"}:
        raise RuntimeError("This Audiomack track is geographically restricted and will not be bypassed.")
    if str(data.get("status", "complete")).lower() not in {"complete", "active", "published"}:
        raise RuntimeError("This Audiomack track is not publicly available.")


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return default


def _public_info(data: dict) -> dict:
    uploader = data.get("uploader") if isinstance(data.get("uploader"), dict) else {}
    duration = data.get("duration") or 0
    duration = _safe_int(duration)
    artist_slug = uploader.get("url_slug") or ""
    track_slug = data.get("url_slug") or ""
    track_url = f"{SITE_BASE}/{artist_slug}/song/{track_slug}" if artist_slug and track_slug else None
    restricted = (
        str(data.get("private", "no")).lower() == "yes"
        or str(data.get("stream_only", "no")).lower() == "yes"
        or str(data.get("premium_user_only", "no")).lower() == "yes"
        or str(data.get("premium_download", "no")).lower() == "yes"
        or data.get("geo_restricted") is True
        or str(data.get("geo_lock", "no")).lower() not in {"", "no", "false", "0"}
        or str(data.get("status", "complete")).lower() not in {"complete", "active", "published"}
    )
    return {
        "ok": True,
        "extractor": "kangodl-audiomack-native",
        "id": str(data.get("id")),
        "title": data.get("title") or "Audiomack track",
        "artist": data.get("artist") or uploader.get("name") or "",
        "author": uploader.get("name") or data.get("artist") or "",
        "album": data.get("album") or "",
        "description": (data.get("description") or "")[:4000],
        "thumbnail": data.get("image") or data.get("image_base") or uploader.get("image"),
        "duration_seconds": duration,
        "genre": data.get("genre") or "",
        "url": track_url,
        "source_url": track_url,
        "stream_only": str(data.get("stream_only", "no")).lower() == "yes",
        "geo_restricted": bool(data.get("geo_restricted")),
        "premium_required": str(data.get("premium_user_only", "no")).lower() == "yes" or str(data.get("premium_download", "no")).lower() == "yes",
        "download_available": not restricted,
        "formats": ([{"format_id": "audiomack-audio", "ext": "mp3", "has_audio": True, "has_video": False}] if not restricted else []),
        "format_count": 0 if restricted else 1,
        "source": "Audiomack public catalog and playback API; KangoDL native OAuth 1.0a client",
    }


def extract_audiomack_info(url: str) -> dict:
    try:
        data = _track_from_url(url); method = "audiomack-documented-oauth-api"
    except Exception as primary_error:
        try: data = _track_from_page(url); method = "audiomack-public-page-json"
        except Exception as fallback_error: raise RuntimeError(f"Audiomack API and page resolvers failed: {primary_error}; {fallback_error}") from fallback_error
    result = _public_info(data)
    result["source"] = "KangoDL dual-path Audiomack resolver"
    result["extraction_strategy"] = strategy_info("audiomack-documented-oauth-api", "audiomack-public-page-json", method)
    return result


def search_audiomack(query: str, limit: int = 20, page: int = 1, kind: str = "songs") -> dict:
    query = re.sub(r"\s+", " ", (query or "")).strip()
    if not query:
        raise ValueError("Search query is required.")
    if len(query) > 160:
        raise ValueError("Search query is too long.")
    limit = max(1, min(int(limit), 50))
    page = max(1, min(int(page), 100))
    show = kind.lower().strip()
    if show not in {"songs", "albums", "artists", "music"}:
        raise ValueError("type must be songs, albums, artists, or music.")
    response = _api_request("GET", "search", {"q": query, "show": show, "sort": "relevance", "page": page, "limit": limit})
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError("Audiomack returned an invalid search response.") from exc
    results = []
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        uploader = item.get("uploader") if isinstance(item.get("uploader"), dict) else {}
        item_type = item.get("type") or "song"
        slug = item.get("url_slug") or ""
        artist_slug = uploader.get("url_slug") or ""
        if item_type == "artist":
            item_url = f"{SITE_BASE}/{slug}" if slug else None
        else:
            path_type = "song" if item_type == "song" else item_type
            item_url = f"{SITE_BASE}/{artist_slug}/{path_type}/{slug}" if artist_slug and slug else None
        results.append({
            "id": str(item.get("id") or ""),
            "title": item.get("title") or item.get("name") or "",
            "artist": item.get("artist") or uploader.get("name") or "",
            "creator": uploader.get("name") or item.get("artist") or "",
            "type": item_type,
            "url": item_url,
            "thumbnail": item.get("image") or item.get("image_base") or uploader.get("image"),
            "duration_seconds": _safe_int(item.get("duration")),
            "genre": item.get("genre") or "",
            "stream_only": str(item.get("stream_only", "no")).lower() == "yes",
            "geo_restricted": bool(item.get("geo_restricted")),
            "premium_required": str(item.get("premium_user_only", "no")).lower() == "yes" or str(item.get("premium_download", "no")).lower() == "yes",
        })
    count_key = {"songs": "song_count", "albums": "album_count", "artists": "artist_count", "music": "music_count"}[show]
    reported_count = _safe_int(payload.get(count_key) or payload.get("count"))
    return {"ok": True, "source": "audiomack", "query": query, "type": show, "page": page, "count": reported_count or len(results), "results_count": len(results), "results": results}


def download_audiomack(url: str, destination: Path, output: str = "mp3", progress_hook=None) -> dict:
    if output != "mp3":
        raise RuntimeError("Audiomack audio downloads currently require MP3 output.")
    try:
        data = _track_from_url(url)
        extraction_method = "audiomack-documented-oauth-api"
    except Exception as primary_error:
        try:
            data = _track_from_page(url)
            extraction_method = "audiomack-public-page-json"
        except Exception as fallback_error:
            raise RuntimeError(f"Audiomack API and page resolvers failed: {primary_error}; {fallback_error}") from fallback_error
    _public_track(data)
    track_id = str(data["id"])
    # Audiomack's documented play endpoint issues a short-lived URL for the
    # currently playable public stream. This is not a private/premium bypass.
    play = _api_request("POST", f"music/{track_id}/play", body={"session": "kangodl-" + secrets.token_hex(12)})
    try:
        stream_url = play.json()
    except Exception as exc:
        raise RuntimeError("Audiomack did not return a playable stream URL.") from exc
    if isinstance(stream_url, dict):
        stream_url = stream_url.get("url") or stream_url.get("stream_url")
    if not isinstance(stream_url, str):
        raise RuntimeError("Audiomack did not return a valid public stream URL.")
    parsed = urlparse(stream_url)
    if parsed.scheme != "https" or parsed.hostname not in {"music.audiomack.com"}:
        raise RuntimeError("Audiomack returned an unexpected media host; refusing the download.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = destination.with_suffix(".audiomack-source.m4a")
    total = 0
    try:
        with requests.get(stream_url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=(20, 90)) as response:
            response.raise_for_status()
            expected = int(response.headers.get("Content-Length") or 0)
            with source.open("wb") as output_file:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise RuntimeError("Audiomack media exceeds the 2 GiB safety limit.")
                    output_file.write(chunk)
                    if progress_hook:
                        progress_hook({"status": "downloading", "downloaded_bytes": total, "total_bytes": expected})
        if not total:
            raise RuntimeError("Audiomack returned an empty audio stream.")
        subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(destination),
        ], check=True, timeout=900)
        if not destination.exists() or not destination.stat().st_size:
            raise RuntimeError("MP3 conversion produced an empty file.")
    finally:
        source.unlink(missing_ok=True)
    return {"bytes": destination.stat().st_size, "title": data.get("title"), "author": (data.get("uploader") or {}).get("name") or data.get("artist"), "thumbnail": data.get("image"), "duration_seconds": _safe_int(data.get("duration")), "format_id": "audiomack-audio", "media_type": "audio", "extraction_method": extraction_method}
