from __future__ import annotations
import json, re, subprocess, zipfile
from curl_cffi import requests as curl_requests
from app.extractors.strategy import strategy_info
from pathlib import Path
from urllib.parse import urlparse
import requests

TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
MAX_BYTES = 2 * 1024 * 1024 * 1024


def is_tiktok_url(value: str) -> bool:
    try:
        p = urlparse(value); host = (p.hostname or "").lower()
        return p.scheme in {"http", "https"} and any(host == d or host.endswith("." + d) for d in TIKTOK_HOSTS)
    except Exception: return False


def _get_page(url: str):
    if not is_tiktok_url(url): raise RuntimeError("Not a TikTok URL.")
    errors = []
    try:
        s = requests.Session(); s.headers.update(HEADERS)
        r = s.get(url, timeout=30, allow_redirects=True); r.raise_for_status()
        if len(r.content) > 12_000_000: raise RuntimeError("TikTok page exceeded the metadata size limit.")
        m = re.search(r'<script[^>]+id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>', r.text, re.S)
        if not m: m = re.search(r'<script[^>]+id=["\']SIGI_STATE["\'][^>]*>(.*?)</script>', r.text, re.S)
        if not m: raise RuntimeError("TikTok page did not include public post metadata.")
        data = json.loads(m.group(1)); found = []
        def walk(x):
            if isinstance(x, dict):
                if str(x.get("id", "")).isdigit() and ("video" in x or "images" in x or "imagePost" in x): found.append(x)
                for v in x.values(): walk(v)
            elif isinstance(x, list):
                for v in x: walk(v)
        walk(data)
        post = next((x for x in found if x.get("video", {}).get("playAddr") or x.get("video", {}).get("downloadAddr") or x.get("images") or x.get("imagePost")), None)
        if not post: raise RuntimeError("TikTok page JSON did not contain a media post.")
        return s, post, "tiktok-page-json"
    except Exception as exc:
        errors.append(f"page-json: {type(exc).__name__}: {exc}")
    try:
        return _get_embed_post(url)
    except Exception as exc:
        errors.append(f"embed-player-json: {type(exc).__name__}: {exc}")
    try:
        return _get_api_post(url)
    except Exception as exc:
        errors.append(f"item-detail-api: {type(exc).__name__}: {exc}")
    raise RuntimeError("TikTok primary and fallback resolvers failed: " + " | ".join(errors))



def _get_api_post(url: str):
    """Fallback resolver: TikTok's public web item-detail endpoint (not the Display API)."""
    if not is_tiktok_url(url):
        raise RuntimeError("Not a TikTok URL.")
    session = curl_requests.Session(impersonate="chrome")
    session.headers.update({**HEADERS, "Accept": "application/json, text/plain, */*", "Referer": "https://www.tiktok.com/"})
    page = session.get(url, timeout=35, allow_redirects=True)
    page.raise_for_status()
    match = re.search(r"/video/(\d+)", page.url)
    if not match:
        # Photo posts can still have a numeric item id in their canonical URL.
        match = re.search(r"/(?:photo|video)/(\d+)", page.url)
    if not match:
        raise RuntimeError("TikTok item-detail fallback could not resolve a numeric post ID.")
    item_id = match.group(1)
    endpoint = "https://www.tiktok.com/api/item/detail/"
    response = session.get(endpoint, params={"aid": "1988", "app_name": "tiktok_web", "itemId": item_id}, timeout=30)
    response.raise_for_status()
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError("TikTok item-detail endpoint returned no usable JSON.") from exc
    post = ((payload.get("itemInfo") or {}).get("itemStruct") or payload.get("itemStruct") or (payload.get("aweme_detail") or {}))
    if not isinstance(post, dict) or not (post.get("video") or post.get("images") or post.get("imagePost")):
        raise RuntimeError("TikTok item-detail API did not expose downloadable media for this post.")
    return session, post, "tiktok-public-item-detail-api"


def _get_embed_post(url: str):
    """Third resolver: read the public TikTok embed page's server-rendered Frontity player state."""
    if not is_tiktok_url(url):
        raise RuntimeError("Not a TikTok URL.")
    session = curl_requests.Session(impersonate="chrome")
    session.headers.update({**HEADERS, "Accept": "text/html,application/xhtml+xml,*/*", "Referer": "https://www.tiktok.com/"})
    landing = session.get(url, timeout=35, allow_redirects=True)
    landing.raise_for_status()
    if len(landing.content) > 12_000_000:
        raise RuntimeError("TikTok landing page exceeded the metadata size limit.")
    match = re.search(r"/(?:photo|video)/(\d+)", landing.url)
    if not match:
        raise RuntimeError("TikTok embed fallback could not resolve a numeric public post ID.")
    item_id = match.group(1)
    embed_url = f"https://www.tiktok.com/embed/{item_id}"
    response = session.get(embed_url, headers={"Referer": landing.url}, timeout=35)
    response.raise_for_status()
    if len(response.content) > 12_000_000:
        raise RuntimeError("TikTok embed page exceeded the metadata size limit.")
    match = re.search(r'<script[^>]+id=["\']__FRONTITY_CONNECT_STATE__["\'][^>]*>(.*?)</script>', response.text, re.S)
    if not match:
        raise RuntimeError("TikTok embed page did not include public player state.")
    try:
        state = json.loads(match.group(1))
    except Exception as exc:
        raise RuntimeError("TikTok embed player state was not valid JSON.") from exc
    source = state.get("source") if isinstance(state, dict) else None
    data = source.get("data") if isinstance(source, dict) else None
    video_data = None
    if isinstance(data, dict):
        route = data.get(f"/embed/{item_id}") or data.get(f"/embed/v2/{item_id}")
        if isinstance(route, dict):
            video_data = route.get("videoData")
        if not isinstance(video_data, dict):
            # Route keys can include a trailing slash or query; identify the exact item in the embed state.
            for route_value in data.values():
                candidate = route_value.get("videoData") if isinstance(route_value, dict) else None
                item = candidate.get("itemInfos") if isinstance(candidate, dict) else None
                if isinstance(item, dict) and str(item.get("id")) == item_id:
                    video_data = candidate
                    break
    if not isinstance(video_data, dict):
        raise RuntimeError("TikTok embed state did not contain the requested post.")
    item = video_data.get("itemInfos") or {}
    if not isinstance(item, dict) or str(item.get("id")) != item_id:
        raise RuntimeError("TikTok embed state did not match the requested post ID.")
    images = (video_data.get("imagePostInfo") or {}).get("displayImages") or []
    author = video_data.get("authorInfos") or {}
    stats = {"playCount": item.get("playCount")}
    normalized = {
        "id": item_id,
        "desc": item.get("text") or "TikTok media",
        "author": {"uniqueId": author.get("uniqueId"), "nickname": author.get("nickName")},
        "stats": stats,
        "video": item.get("video") or {},
        "images": images,
        "music": video_data.get("musicInfos") or {},
        "_embed_player": True,
    }
    if not normalized["images"] and not _urls(normalized["video"].get("urls")) and not _urls(normalized["video"].get("playAddr")):
        raise RuntimeError("TikTok embed player state did not expose public photo or video media URLs.")
    return session, normalized, "tiktok-public-embed-player-json"


def _urls(value):
    if isinstance(value, str): return [value] if value.startswith("http") else []
    if isinstance(value, list): return [u for x in value for u in _urls(x)]
    if isinstance(value, dict):
        for k in ("urlList", "url_list", "url", "UrlList"):
            if k in value:
                result = _urls(value[k])
                if result: return result
    return []


def _image_candidates(post):
    images = post.get("images") or (post.get("imagePost") or {}).get("images") or (post.get("imagePostInfo") or {}).get("displayImages") or []
    result = []
    for image in images:
        if not isinstance(image, dict):
            candidates = _urls(image)
        else:
            candidates = (_urls(image.get("displayImage")) or _urls(image.get("imageURL"))
                          or _urls(image.get("image_url")) or _urls(image.get("urlList")))
        # TikTok often returns two CDN mirrors for one slide. Keep one slide,
        # not two duplicate ZIP entries; the downloader tries mirrors on failure.
        if candidates:
            result.append(list(dict.fromkeys(candidates)))
    return result


def _post_parts(post):
    video = post.get("video") or {}
    video_urls = _urls(video.get("playAddr")) or _urls(video.get("PlayAddrStruct")) or _urls(video.get("downloadAddr")) or _urls(video.get("urls"))
    music = post.get("music") or {}
    audio_urls = _urls(music.get("playUrl")) or _urls(music.get("PlayUrl"))
    image_urls = [candidates[0] for candidates in _image_candidates(post) if candidates]
    return video, video_urls, audio_urls, image_urls


def extract_tiktok_info(url: str) -> dict:
    _, post, method = _get_page(url); video, vurls, aurls, imgs = _post_parts(post)
    formats = []
    if vurls: formats.append({"format_id": "tiktok-play", "ext": "mp4", "width": video.get("width"), "height": video.get("height"), "has_video": True, "has_audio": True})
    if aurls: formats.append({"format_id": "tiktok-audio", "ext": "mp3", "has_video": False, "has_audio": True})
    if imgs: formats.append({"format_id": "tiktok-photos", "ext": "zip", "has_video": False, "has_audio": False, "photo_count": len(imgs)})
    author = post.get("author") or {}
    return {"ok": True, "extractor": "kangodl-tiktok", "video_id": str(post.get("id") or ""), "title": post.get("desc") or "TikTok media", "author": author.get("uniqueId") or author.get("nickname"), "duration_seconds": int(video.get("duration") or 0), "view_count": (post.get("stats") or {}).get("playCount"), "thumbnail": (_urls(video.get("cover")) or imgs or [None])[0], "description": (post.get("desc") or "")[:5000], "formats": formats, "format_count": len(formats), "watermark_free_available": bool(vurls), "photo_count": len(imgs), "source": "KangoDL dual-path TikTok resolver", "extraction_strategy": strategy_info("tiktok-page-json", "tiktok-public-embed-player-json", method)}


def _download(session, url, destination, referer, progress_hook=None):
    destination.parent.mkdir(parents=True, exist_ok=True); total = 0
    headers = {"Referer": referer, "User-Agent": UA, "Accept": "*/*"}
    with session.get(url, headers=headers, stream=True, timeout=(20, 90)) as r:
        r.raise_for_status()
        expected = int(r.headers.get("Content-Length") or 0)
        with destination.open("wb") as out:
            for chunk in r.iter_content(256 * 1024):
                if not chunk: continue
                total += len(chunk)
                if total > MAX_BYTES: raise RuntimeError("TikTok media exceeds the 2 GiB safety limit.")
                out.write(chunk)
                if progress_hook: progress_hook({"status":"downloading", "downloaded_bytes":total, "total_bytes":expected})
    if not total: raise RuntimeError("TikTok returned an empty media stream.")
    return total


def download_tiktok(url: str, destination: Path, mode: str, progress_hook=None, format_id: str | None = None) -> dict:
    if mode not in {"video", "audio"}: raise RuntimeError("TikTok mode must be video or audio.")
    session, post, method = _get_page(url); video, vurls, aurls, _ = _post_parts(post)
    if mode == "video":
        if not vurls: raise RuntimeError("This TikTok post has no public video stream; it may be a photo slideshow.")
        source = vurls[0]
        size = _download(session, source, destination, url, progress_hook)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if aurls:
            tmp = destination.with_suffix(".source"); size = _download(session, aurls[0], tmp, url, progress_hook)
            tmp.replace(destination)
        elif vurls:
            tmp = destination.with_suffix(".source.mp4"); _download(session, vurls[0], tmp, url, progress_hook)
            p = subprocess.run(["ffmpeg", "-y", "-i", str(tmp), "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", str(destination)], capture_output=True, timeout=300)
            tmp.unlink(missing_ok=True)
            if p.returncode: raise RuntimeError("Could not convert TikTok audio to MP3: " + p.stderr.decode(errors="ignore")[-250:])
            size = destination.stat().st_size
        else: raise RuntimeError("This TikTok post does not expose a public audio or video stream.")
    if not destination.exists() or not destination.stat().st_size: raise RuntimeError("TikTok download produced no file.")
    author = post.get("author") or {}; title = post.get("desc") or "TikTok media"
    return {"title":title, "author":author.get("uniqueId") or author.get("nickname"), "duration_seconds":int(video.get("duration") or 0), "thumbnail":(_urls(video.get("cover")) or [None])[0], "format_id":format_id or ("tiktok-play" if mode == "video" else "tiktok-audio"), "height":video.get("height"), "width":video.get("width"), "bytes":size, "extraction_method":method}


def download_tiktok_photos(url: str, destination: Path) -> dict:
    session, post, method = _get_page(url); candidates_by_image = _image_candidates(post)
    if not candidates_by_image: raise RuntimeError("This TikTok post does not expose a public photo slideshow.")
    if len(candidates_by_image) > 35: raise RuntimeError("TikTok slideshow exceeds the 35-image safety limit.")
    destination.parent.mkdir(parents=True, exist_ok=True); total = 0
    image_session = requests.Session()
    image_session.headers.update({"Referer": url, "User-Agent": UA, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"})
    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
            for i, candidates in enumerate(candidates_by_image, 1):
                last_error = None; content = None; content_type = "image/jpeg"
                for image_url in candidates:
                    try:
                        # TikTok's image CDN behaves more reliably with the standard
                        # HTTP session than with the curl-impersonation session used
                        # to fetch the public player page.
                        response = image_session.get(image_url, timeout=(15, 120))
                        response.raise_for_status()
                        if not response.content:
                            raise RuntimeError("TikTok returned an empty photo image.")
                        content = response.content; content_type = response.headers.get("Content-Type", "image/jpeg").lower()
                        break
                    except Exception as exc:
                        last_error = exc
                if content is None:
                    raise RuntimeError(f"Could not download TikTok slide {i}: {last_error}")
                total += len(content)
                if total > 500 * 1024 * 1024: raise RuntimeError("TikTok slideshow exceeds the 500 MiB safety limit.")
                if content.startswith(b"\xff\xd8\xff"):
                    ext = ".jpg"
                elif content.startswith(b"\x89PNG\r\n\x1a\n"):
                    ext = ".png"
                elif content[:4] == b"RIFF" and content[8:12] == b"WEBP":
                    ext = ".webp"
                else:
                    raise RuntimeError(f"TikTok slide {i} did not return a supported JPEG, PNG or WebP image.")
                archive.writestr(f"image_{i:02d}{ext}", content)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {"photo_count":len(candidates_by_image), "bytes":destination.stat().st_size, "format_id":"tiktok-photo-carousel", "title":post.get("desc") or "TikTok photo slideshow", "extraction_method":method}
