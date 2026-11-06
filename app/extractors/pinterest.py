from __future__ import annotations
import json, re, subprocess
from pathlib import Path
from urllib.parse import urlparse
import requests
import os
from app.extractors.strategy import strategy_info

HOSTS = {"pinterest.com", "www.pinterest.com", "m.pinterest.com", "pin.it"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
MAX_BYTES = 2 * 1024 * 1024 * 1024


def is_pinterest_url(value: str) -> bool:
    try:
        p = urlparse(value); host = (p.hostname or "").lower()
        return p.scheme in {"http", "https"} and any(host == d or host.endswith("." + d) for d in HOSTS)
    except Exception: return False


def _object_after(text: str, marker: str):
    pos = text.find(marker)
    if pos < 0: return None
    start = text.find("{", pos + len(marker))
    if start < 0: return None
    depth = 0; quoted = False; escaped = False
    for i in range(start, min(len(text), start + 300000)):
        c = text[i]
        if quoted:
            if escaped: escaped = False
            elif c == "\\": escaped = True
            elif c == '"': quoted = False
        else:
            if c == '"': quoted = True
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try: return json.loads(text[start:i+1])
                    except Exception: return None
    return None


def _meta(text: str, key: str):
    for tag in re.findall(r"<meta\b[^>]*>", text, re.I):
        attr = re.search(r"(?:property|name)=[\"']" + re.escape(key) + r"[\"']", tag, re.I)
        content = re.search(r"content=[\"']([^\"']*)", tag, re.I)
        if attr and content: return content.group(1)
    return None


def _get_pin(url: str):
    if not is_pinterest_url(url): raise RuntimeError("Not a Pinterest URL.")
    session = requests.Session(); session.headers.update(HEADERS)
    response = session.get(url, allow_redirects=True, timeout=35); response.raise_for_status()
    if len(response.content) > 15_000_000: raise RuntimeError("Pinterest page exceeded the metadata size limit.")
    page = response.text
    # Pinterest embeds the public pin entity in its server-rendered page state.
    entity = _object_after(page, '"entityId"')
    # entityId occurs inside the pin object; locate the matching enclosing data by scanning
    # the pin's stable markers, then fall back to the story-video object.
    video = _object_after(page, '"videoList"')
    video_url = None; video_thumb = None; duration = 0; width = height = None
    if video:
        for key in ("vHLSV4", "vHLSV3MOBILE"):
            item = video.get(key)
            if isinstance(item, dict) and item.get("url"):
                video_url = item["url"]; video_thumb = item.get("thumbnail"); duration = int(item.get("duration") or 0); duration = round(duration / 1000) if duration > 1000 else duration; width = item.get("width"); height = item.get("height"); break
    # Original pin image URL is present in the pin entity's images_orig field.
    image_obj = _object_after(page, '"images_orig"')
    image_url = image_obj.get("url") if isinstance(image_obj, dict) else None
    if not image_url: image_url = _meta(page, "og:image")
    title = _meta(page, "og:title") or "Pinterest media"
    description = _meta(page, "og:description") or ""
    pin_id = re.search(r'"entityId"\s*:\s*"(\d+)"', page)
    pin_id = pin_id.group(1) if pin_id else (re.search(r'/pin/(\d+)', response.url).group(1) if re.search(r'/pin/(\d+)', response.url) else "")
    extraction_method = "pinterest-public-page-json" if (image_url or video_url) else ""
    if not image_url and not video_url:
        token = os.getenv("PINTEREST_ACCESS_TOKEN", "").strip()
        if token and pin_id:
            api = requests.get(f"https://api.pinterest.com/v5/pins/{pin_id}", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=(15,35))
            api.raise_for_status(); obj = api.json(); media = obj.get("media") or {}
            images = media.get("images") or {}
            for variant in ("1200x", "600x", "400x300", "150x150"):
                if isinstance(images.get(variant), dict) and images[variant].get("url"):
                    image_url = images[variant]["url"]; break
            if media.get("media_type") == "video":
                video_obj = media.get("video") or {}
                video_url = video_obj.get("url") or video_obj.get("hls_url")
            title = obj.get("title") or title; description = obj.get("description") or description
            extraction_method = "pinterest-official-v5-api" if (image_url or video_url) else ""
    if not image_url and not video_url: raise RuntimeError("Pinterest page resolver and optional official API did not expose public media. The official API requires a Pinterest OAuth token with access to the Pin.")
    return session, {"id":pin_id, "title":title, "description":description, "image_url":image_url, "video_url":video_url, "thumbnail":video_thumb or image_url, "duration_seconds":duration, "width":width, "height":height, "page_url":response.url, "_extraction_method":extraction_method}


def extract_pinterest_info(url: str) -> dict:
    _, pin = _get_pin(url); formats=[]
    if pin["image_url"] and not pin["video_url"]: formats.append({"format_id":"pinterest-image-original", "ext":"jpg", "has_video":False, "has_audio":False, "width":pin.get("width"), "height":pin.get("height")})
    if pin["video_url"]: formats.append({"format_id":"pinterest-video-hls", "ext":"mp4", "has_video":True, "has_audio":True, "width":pin.get("width"), "height":pin.get("height")})
    return {"ok":True,"extractor":"kangodl-pinterest-native","video_id":pin["id"],"title":pin["title"],"description":pin["description"][:5000],"thumbnail":pin["thumbnail"],"duration_seconds":pin["duration_seconds"],"formats":formats,"format_count":len(formats),"image_available":bool(pin["image_url"] and not pin["video_url"]),"video_available":bool(pin["video_url"]),"source":"KangoDL dual-path Pinterest resolver","extraction_strategy":strategy_info("pinterest-public-page-json", "pinterest-official-v5-api", pin.get("_extraction_method", "pinterest-public-page-json"))}


def download_pinterest(url: str, destination: Path, mode: str, output: str, progress_hook=None) -> dict:
    session, pin = _get_pin(url); destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "image":
        if not pin["image_url"] or pin["video_url"]: raise RuntimeError("This Pinterest pin does not expose a public image URL.")
        with session.get(pin["image_url"], headers={"Referer":pin["page_url"]}, stream=True, timeout=(20,90)) as r:
            r.raise_for_status(); total=0
            with destination.open("wb") as f:
                for chunk in r.iter_content(256*1024):
                    if not chunk: continue
                    total += len(chunk)
                    if total > MAX_BYTES: raise RuntimeError("Pinterest image exceeds the 2 GiB safety limit.")
                    f.write(chunk)
                    if progress_hook: progress_hook({"status":"downloading","downloaded_bytes":total,"total_bytes":int(r.headers.get("Content-Length") or 0)})
        if not total: raise RuntimeError("Pinterest returned an empty image.")
        return {"bytes":total,"title":pin["title"],"thumbnail":pin["thumbnail"],"format_id":"pinterest-image-original","media_type":"image","extraction_method":pin.get("_extraction_method")}
    if not pin["video_url"]: raise RuntimeError("This Pinterest pin does not expose a public video stream.")
    headers = f"User-Agent: {UA}\\r\\nReferer: {pin['page_url']}\\r\\n"
    args=["ffmpeg","-nostdin","-hide_banner","-loglevel","error","-y","-headers",headers,"-i",pin["video_url"]]
    if mode == "audio": args += ["-vn","-c:a","libmp3lame","-b:a","192k"]
    elif output == "mp4": args += ["-c","copy","-movflags","+faststart"]
    else: args += ["-c:v","libvpx","-deadline","realtime","-cpu-used","8","-crf","32","-b:v","0","-c:a","libopus"]
    args += [str(destination)]
    p=subprocess.run(args,capture_output=True,timeout=1800)
    if p.returncode or not destination.exists() or not destination.stat().st_size:
        raise RuntimeError("Pinterest HLS download failed: "+p.stderr.decode(errors="ignore")[-300:])
    return {"bytes":destination.stat().st_size,"title":pin["title"],"thumbnail":pin["thumbnail"],"duration_seconds":pin["duration_seconds"],"format_id":"pinterest-video-hls","media_type":"video","extraction_method":pin.get("_extraction_method")}
