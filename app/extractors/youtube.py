from __future__ import annotations
import json, re
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"}
WEB_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
ANDROID_UA = "com.google.android.youtube/20.10.38 (Linux; U; Android 15) gzip"

class ExtractorError(Exception):
    pass

def video_id_from_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in YOUTUBE_HOSTS:
        raise ExtractorError("Provide a valid HTTPS YouTube URL.")
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    else:
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            video_id = parts[1]
        else:
            video_id = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        raise ExtractorError("The YouTube URL does not contain a valid video ID.")
    return video_id

def _innertube_player(video_id: str, api_key: str, client_name: str, client_version: str, user_agent: str, extra: dict) -> dict:
    context_client = {"clientName": client_name, "clientVersion": client_version, "hl": "en", "gl": "US", **extra}
    body = {"videoId": video_id, "context": {"client": context_client}, "contentCheckOk": True, "racyCheckOk": True}
    endpoint = "https://www.youtube.com/youtubei/v1/player?key=" + api_key
    request = Request(endpoint, data=json.dumps(body).encode("utf-8"), headers={
        "User-Agent": user_agent, "Content-Type": "application/json",
        "Origin": "https://www.youtube.com", "Referer": "https://www.youtube.com/",
    })
    with urlopen(request, timeout=25) as response:
        return json.loads(response.read(4_000_000).decode("utf-8"))

def _normalise_format(item: dict) -> dict:
    mime = item.get("mimeType", "")
    match = re.search(r'codecs="([^"]+)', mime)
    codecs = match.group(1) if match else ""
    has_video = mime.startswith("video/") or bool(item.get("width") and item.get("height"))
    has_audio = mime.startswith("audio/") or "audio" in codecs.lower() or bool(item.get("audioQuality"))
    cipher = item.get("signatureCipher") or item.get("cipher")
    return {
        "format_id": str(item.get("itag", "")), "mime_type": mime.split(";")[0],
        "codecs": codecs, "quality": item.get("qualityLabel") or item.get("audioQuality") or item.get("quality"),
        "width": item.get("width"), "height": item.get("height"), "bitrate": item.get("bitrate"),
        "filesize": int(item["contentLength"]) if str(item.get("contentLength", "")).isdigit() else None,
        "has_video": has_video, "has_audio": has_audio, "has_direct_url": bool(item.get("url")),
        "requires_signature_decipher": bool(cipher and not item.get("url")),
        "stream_url_available": bool(item.get("url")),
        "download_path_type": ("muxed" if has_video and has_audio else "video_only" if has_video else "audio_only" if has_audio else "unknown"),
        "ready_for_direct_video_download": bool(item.get("url")) and has_video and has_audio,
        "_direct_url": item.get("url"),
    }

def extract_youtube_info(value: str) -> dict:
    video_id = video_id_from_url(value)
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    request = Request(watch_url, headers={"User-Agent": WEB_UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urlopen(request, timeout=25) as response:
            page = response.read(5_000_000).decode("utf-8", "replace")
    except Exception as exc:
        raise ExtractorError(f"Could not load the public YouTube page ({type(exc).__name__}).") from exc
    key_match = re.search(r'INNERTUBE_API_KEY\\?":\\?"([^"\\]+)', page)
    if not key_match:
        key_match = re.search(r'INNERTUBE_API_KEY\s*["\']?\s*:\s*["\']([^"\']+)', page)
    if not key_match:
        raise ExtractorError("YouTube did not provide the public player API configuration.")
    api_key = key_match.group(1)
    player = None
    last_error = None
    clients = [
        ("ANDROID", "20.10.38", ANDROID_UA, {"androidSdkVersion": 35, "osName": "Android", "osVersion": "15", "platform": "MOBILE"}),
        ("IOS", "20.10.4", "com.google.ios.youtube/20.10.4", {"deviceMake": "Apple", "deviceModel": "iPhone16,2", "osName": "iPhone", "osVersion": "18.1.0", "platform": "MOBILE"}),
    ]
    for name, version, ua, extra in clients:
        try:
            candidate = _innertube_player(video_id, api_key, name, version, ua, extra)
            streams = candidate.get("streamingData", {})
            if candidate.get("playabilityStatus", {}).get("status") == "OK" and (streams.get("formats") or streams.get("adaptiveFormats")):
                player = candidate
                break
            last_error = candidate.get("playabilityStatus", {}).get("reason") or candidate.get("playabilityStatus", {}).get("status")
        except Exception as exc:
            last_error = type(exc).__name__
    if player is None:
        raise ExtractorError(f"YouTube did not return playable public streams ({last_error or 'unknown player error'}).")
    details, streaming = player.get("videoDetails", {}), player.get("streamingData", {})
    raw_formats = streaming.get("formats", []) + streaming.get("adaptiveFormats", [])
    formats = [_normalise_format(item) for item in raw_formats if item.get("itag") is not None]
    seen, unique = set(), []
    for item in formats:
        key = (item["format_id"], item["mime_type"], item["quality"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    public_formats = [{k: v for k, v in item.items() if not k.startswith("_")} for item in unique]
    return {
        "ok": True, "extractor": "kangodl-youtube", "video_id": video_id,
        "title": details.get("title"), "author": details.get("author"),
        "channel_id": details.get("channelId"), "duration_seconds": int(details.get("lengthSeconds", 0) or 0),
        "view_count": int(details.get("viewCount", 0) or 0),
        "thumbnail": (details.get("thumbnail", {}).get("thumbnails") or [{}])[-1].get("url"),
        "description": details.get("shortDescription", "")[:5000],
        "formats": public_formats, "format_count": len(public_formats),
        "direct_format_count": sum(1 for item in unique if item["has_direct_url"]),
        "signature_decipher_needed": any(item["requires_signature_decipher"] for item in unique),
        "expires_in_seconds": streaming.get("expiresInSeconds"),
        "source": "YouTube public player API via KangoDL extractor",
        "_internal_formats": unique,
    }
