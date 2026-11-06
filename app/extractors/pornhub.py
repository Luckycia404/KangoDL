from __future__ import annotations
import json, re, subprocess, html as html_lib, time, tempfile, shutil, concurrent.futures, requests, threading
from pathlib import Path
from urllib.parse import urlparse, urljoin
from curl_cffi import requests as curl_requests
from app.extractors.strategy import strategy_info

PORNHUB_HOSTS = {"pornhub.com", "www.pornhub.com", "m.pornhub.com", "pornhub.org", "www.pornhub.org"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def is_pornhub_url(value: str) -> bool:
    try:
        p=urlparse(value); h=(p.hostname or "").lower()
        return p.scheme == "https" and any(h == d or h.endswith("."+d) for d in PORNHUB_HOSTS)
    except Exception: return False


def _walk(obj, out):
    if isinstance(obj, dict):
        if any(k in obj for k in ("videoUrl", "video_url", "quality", "defaultQuality")): out.append(obj)
        for v in obj.values(): _walk(v,out)
    elif isinstance(obj,list):
        for v in obj: _walk(v,out)



def _media_definitions_candidates(page_html: str) -> list:
    """Parse mediaDefinitions arrays from this page's own inline scripts."""
    found=[]
    for match in re.finditer(r"[\"']?mediaDefinitions[\"']?\s*:\s*\[", page_html):
        begin=match.end()-1; depth=0; quoted=False; escaped=False; end=None
        for pos in range(begin,len(page_html)):
            ch=page_html[pos]
            if quoted:
                if escaped: escaped=False
                elif ch == "\\": escaped=True
                elif ch == '\"': quoted=False
                continue
            if ch == '\"': quoted=True
            elif ch == '[': depth+=1
            elif ch == ']':
                depth-=1
                if depth == 0: end=pos+1; break
        if end:
            try: _walk(json.loads(page_html[begin:end]), found)
            except Exception: pass
    return found


def _is_public_media_url(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        return False
    return urlparse(value).path.lower().endswith((".m3u8", ".mp4", ".webm", ".m4v"))


def _meta_content(page_html: str, wanted: str):
    for tag in re.findall(r"<meta\b[^>]*>", page_html, re.I):
        attrs={k.lower():html_lib.unescape(v) for k,v in re.findall(r"([\w:-]+)\s*=\s*['\"](.*?)['\"]", tag, re.S)}
        if attrs.get("property", "").lower() == wanted or attrs.get("name", "").lower() == wanted:
            return attrs.get("content")
    return None


def _duration_seconds(page_html: str) -> int:
    m=re.search(r"['\"]duration['\"]\s*:\s*['\"]PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?['\"]", page_html, re.I)
    if m:
        h,mi,se=(int(x or 0) for x in m.groups())
        return h*3600+mi*60+se
    m=re.search(r"['\"]duration['\"]\s*:\s*['\"]?(\d+)", page_html, re.I)
    return int(m.group(1)) if m else 0



def _fetch_public_html(session, page_url: str, referer: str | None = None, attempts: int = 2):
    """Fetch public player HTML with bounded Chrome and standard-client retries."""
    last_error = None
    headers = {"Referer": referer} if referer else {}
    for attempt in range(attempts):
        try:
            response = session.get(page_url, headers=headers, timeout=(8, 30), allow_redirects=True)
            response.raise_for_status()
            if len(response.content) > 20_000_000:
                raise RuntimeError("Pornhub page exceeded metadata size limit.")
            return response.text, None
        except Exception as exc:
            last_error = exc
        # The public watch page sometimes fails with the Chrome TLS client but
        # is served by the standard HTTP stack. This is a normal client fallback,
        # not an authentication or access-control bypass.
        try:
            standard_headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*", "Accept-Language": "en-US,en;q=0.9", "Cache-Control": "no-cache", "Pragma": "no-cache"}
            if referer:
                standard_headers["Referer"] = referer
            response = requests.get(page_url, headers=standard_headers, timeout=(8, 30), allow_redirects=True)
            response.raise_for_status()
            if len(response.content) > 20_000_000:
                raise RuntimeError("Pornhub page exceeded metadata size limit.")
            return response.text, None
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.8 * (attempt + 1))
    return "", last_error


def _extract_uncached(url: str):
    if not is_pornhub_url(url): raise RuntimeError("Provide a valid HTTPS Pornhub video URL.")
    # Use a browser-impersonating TLS/client profile for the primary page too.
    # Plain requests intermittently received 4xx responses for pages that load
    # successfully with a Chrome-like client.
    s=curl_requests.Session(impersonate="chrome")
    s.headers.update({"User-Agent":UA,"Accept":"text/html,application/xhtml+xml,*/*","Accept-Language":"en-US,en;q=0.9","Cache-Control":"no-cache","Pragma":"no-cache"})
    html, page_error = _fetch_public_html(s, url, attempts=3)
    # A failed watch-page response must not prevent trying the independent
    # public embed page (and vice versa).
    title=_meta_content(html,"og:title") or _meta_content(html,"twitter:title")
    if not title:
        title_match=re.search(r'<title>(.*?)</title>',html,re.I|re.S)
        title=title_match.group(1) if title_match else "Pornhub video"
    title=re.sub(r"\s+", " ", html_lib.unescape(title)).strip()
    thumbnail=_meta_content(html,"og:image") or _meta_content(html,"twitter:image")
    duration_seconds=_duration_seconds(html)
    candidates=[]
    # Parse embedded JSON blocks, then inspect player config objects.
    for m in re.finditer(r'<script[^>]*>(.*?)</script>',html,re.S|re.I):
        raw=m.group(1).strip()
        if not raw or len(raw)>8_000_000: continue
        if raw[:1] in "[{":
            try: _walk(json.loads(raw),candidates)
            except Exception: pass
    # Player config is usually a JS variable (flashvars_<id> = {...});
    # mediaDefinitions itself is JSON and can be parsed independently.
    candidates.extend(_media_definitions_candidates(html))
    streams=[]
    for item in candidates:
        u=item.get("videoUrl") or item.get("video_url") or item.get("url")
        if not isinstance(u,str): continue
        u=html_lib.unescape(u.replace('\\/','/'))
        if not _is_public_media_url(u): continue
        quality=item.get("quality") or item.get("defaultQuality") or item.get("text") or ""
        q=str(quality)
        height=int(item.get("height") or (re.search(r'(\d{3,4})',q).group(1) if re.search(r'(\d{3,4})',q) else (re.search(r'/(\d{3,4})P_',u).group(1) if re.search(r'/(\d{3,4})P_',u) else 0))) or 0
        height=height or None
        # Do not synchronously fetch HLS playlists during metadata extraction.
        # Some CDN playlist endpoints keep the connection open and stall the
        # entire download job. FFmpeg can resolve the master playlist itself.
        streams.append({"url":u,"quality":q or (f"{height}p" if height else ""),"height":height,"width":item.get("width"),"format_id":str(height or item.get("format") or q or len(streams))})
    # Fallback for JSON-escaped URLs attached to quality labels in inline player code.
    if not streams:
        for m in re.finditer(r'["\'](?:videoUrl|video_url|url)["\']\s*:\s*["\'](https?:\\?/\\?/[^"\']+)["\']',html):
            u=m.group(1).replace('\\/','/')
            if any(x in u.lower() for x in ('.mp4','.m3u8','.webm')):
                hm=re.search(r'/(\d{3,4})P_',u)
                h=int(hm.group(1)) if hm else None
                streams.append({"url":u,"quality":f"{h}p" if h else "","height":h,"width":None,"format_id":str(h or len(streams))})
    # Deduplicate signed URL variants by resolution, keeping one stream per quality.
    seen=set(); unique=[]
    for item in streams:
        key=item.get('height') or item['url'].split('?',1)[0]
        if key not in seen:
            seen.add(key); unique.append(item)
    streams=unique
    extraction_method = "pornhub-embed-player-json" if "/embed/" in urlparse(url).path else "pornhub-watch-page-player-json"
    if not streams:
        # Retry the other public page type independently. If the watch page was
        # rejected or empty, try the embed; if the caller supplied an embed URL,
        # try the corresponding watch page instead.
        viewkey = (re.search(r"viewkey=([A-Za-z0-9]+)", url) or re.search(r"/embed/([A-Za-z0-9]+)", url))
        if viewkey:
            key = viewkey.group(1)
            use_embed = "/embed/" not in urlparse(url).path
            fallback_url = ("https://www.pornhub.com/embed/" + key) if use_embed else ("https://www.pornhub.com/view_video.php?viewkey=" + key)
            fallback_session = curl_requests.Session(impersonate="chrome")
            fallback_session.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": url})
            fallback_html, fallback_error = _fetch_public_html(fallback_session, fallback_url, referer=url, attempts=3)
            fallback_candidates=[]
            for script_match in re.finditer(r'<script[^>]*>(.*?)</script>', fallback_html, re.S|re.I):
                raw=script_match.group(1).strip()
                if raw[:1] in "[{" and len(raw) <= 8_000_000:
                    try: _walk(json.loads(raw), fallback_candidates)
                    except Exception: pass
            fallback_candidates.extend(_media_definitions_candidates(fallback_html))
            fallback_streams=[]
            for item in fallback_candidates:
                media_url=item.get("videoUrl") or item.get("video_url") or item.get("url")
                if not isinstance(media_url,str): continue
                media_url=html_lib.unescape(media_url.replace('\\/','/'))
                if not _is_public_media_url(media_url): continue
                quality=str(item.get("quality") or item.get("defaultQuality") or item.get("text") or "")
                hm=re.search(r"(\d{3,4})",quality)
                height=int(item.get("height") or (hm.group(1) if hm else 0)) or None
                fallback_streams.append({"url":media_url,"quality":quality or (f"{height}p" if height else ""),"height":height,"width":item.get("width"),"format_id":str(height or item.get("format") or quality or len(fallback_streams))})
            if fallback_streams:
                streams=fallback_streams
                s=fallback_session
                extraction_method="pornhub-embed-player-json" if use_embed else "pornhub-watch-page-player-json"
                # Use fallback metadata only when primary page metadata was absent.
                if not title or title == "Pornhub video":
                    title=_meta_content(fallback_html,"og:title") or _meta_content(fallback_html,"twitter:title") or title
                if not thumbnail:
                    thumbnail=_meta_content(fallback_html,"og:image") or _meta_content(fallback_html,"twitter:image")
                if not duration_seconds:
                    duration_seconds=_duration_seconds(fallback_html)
    if not streams:
        detail = f" Primary page: {type(page_error).__name__}: {page_error}." if page_error else ""
        raise RuntimeError("Pornhub public watch/embed pages did not expose playable media URLs." + detail + " Restricted or unavailable content is not bypassed.")
    return s,{"title":title,"thumbnail":thumbnail,"duration_seconds":duration_seconds,"streams":streams,"_extraction_method":extraction_method,"_page_error":(f"{type(page_error).__name__}: {str(page_error)[:180]}" if page_error else None)}


_EXTRACT_CACHE = {}
_EXTRACT_CACHE_LOCK = threading.Lock()
_EXTRACT_CACHE_TTL = 180

def _extract(url: str, force_refresh: bool = False):
    now = time.monotonic()
    if not force_refresh:
        with _EXTRACT_CACHE_LOCK:
            cached = _EXTRACT_CACHE.get(url)
            if cached and now - cached[0] < _EXTRACT_CACHE_TTL:
                return cached[1], cached[2]
    session, data = _extract_uncached(url)
    title = (data.get("title") or "").strip().lower()
    complete_metadata = bool(data.get("duration_seconds")) and title not in {"", "pornhub", "pornhub video"}
    if complete_metadata:
        with _EXTRACT_CACHE_LOCK:
            _EXTRACT_CACHE[url] = (now, session, data)
            if len(_EXTRACT_CACHE) > 128:
                oldest = sorted(_EXTRACT_CACHE.items(), key=lambda item: item[1][0])[:32]
                for key, _ in oldest:
                    _EXTRACT_CACHE.pop(key, None)
    return session, data


def extract_pornhub_info(url: str) -> dict:
    _,d=_extract(url)
    title = (d.get("title") or "").strip().lower()
    if not d.get("duration_seconds") or title in {"", "pornhub", "pornhub video"}:
        detail = f" Watch-page response: {d.get('_page_error')}." if d.get("_page_error") else ""
        raise RuntimeError("Pornhub exposed only incomplete embed/preview metadata. The full public watch-page details were not available; retry later rather than downloading a partial preview." + detail)
    formats=[{"format_id":x['format_id'],"ext":"mp4" if ".mp4" in x['url'].lower() or ".m3u8" in x['url'].lower() else "webm","width":x.get('width'),"height":x['height'],"quality":x['quality'],"has_video":True,"has_audio":True} for x in d['streams']]
    return {"ok":True,"extractor":"kangodl-pornhub","title":d['title'],"duration_seconds":d['duration_seconds'],"thumbnail":d['thumbnail'],"formats":formats,"format_count":len(formats),"source":"KangoDL dual-path Pornhub player resolver","extraction_strategy":strategy_info("pornhub-watch-page-player-json", "pornhub-embed-player-json", d.get("_extraction_method", "pornhub-watch-page-player-json"))}



def _resolve_hls_uri(base_url: str, reference: str) -> str:
    resolved = urljoin(base_url, reference)
    base_parts = urlparse(base_url)
    ref_parts = urlparse(reference)
    resolved_parts = urlparse(resolved)
    # Signed CDN query parameters are often attached only to the master URL;
    # relative variant/segment paths must inherit them when they omit a query.
    if not ref_parts.scheme and not ref_parts.netloc and not ref_parts.query and base_parts.query and not resolved_parts.query:
        from urllib.parse import urlunparse
        resolved = urlunparse(resolved_parts._replace(query=base_parts.query))
    return resolved


def _download_hls_playlist(master_url: str, work_dir: Path, referer: str, progress_hook=None) -> Path:
    """Download a public HLS playlist and its media segments using Python HTTP.

    FFmpeg's TLS/HLS client intermittently fails on the CDN's segment
    connections. Downloading the manifest/segments with requests first avoids
    that transport failure; FFmpeg then muxes the local playlist.
    """
    headers = {"User-Agent": UA, "Referer": referer, "Origin": "https://www.pornhub.com", "Accept": "*/*"}
    session = requests.Session()
    session.headers.update(headers)

    def get_text(url: str) -> str:
        response = session.get(url, timeout=(15, 60))
        response.raise_for_status()
        if len(response.content) > 8 * 1024 * 1024:
            raise RuntimeError("HLS playlist exceeded the 8 MiB safety limit.")
        text = response.text
        if "#EXTM3U" not in text[:100]:
            raise RuntimeError("CDN did not return a valid HLS playlist.")
        return text

    playlist_url = master_url
    playlist_text = get_text(playlist_url)
    # Resolve a master playlist to its best advertised variant.
    lines = playlist_text.splitlines()
    variants = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            uri = next((x.strip() for x in lines[i + 1:] if x.strip() and not x.startswith("#")), None)
            if uri:
                resolution = re.search(r"RESOLUTION=\d+x(\d+)", line)
                bandwidth = re.search(r"BANDWIDTH=(\d+)", line)
                variants.append((int(resolution.group(1)) if resolution else 0, int(bandwidth.group(1)) if bandwidth else 0, _resolve_hls_uri(playlist_url, uri)))
    if variants:
        playlist_url = max(variants)[2]
        playlist_text = get_text(playlist_url)

    if "#EXT-X-STREAM-INF:" in playlist_text:
        raise RuntimeError("Nested HLS master playlists are not supported by this extractor.")

    work_dir.mkdir(parents=True, exist_ok=True)
    local_lines = []
    downloads = []
    segment_index = 0
    byte_range = None
    previous_range_end = 0

    def schedule_resource(remote_url: str, filename: str, range_header: str | None = None):
        local_path = work_dir / filename
        downloads.append((remote_url, local_path, range_header))
        return filename

    for line in playlist_text.splitlines():
        stripped = line.strip()
        if not stripped:
            local_lines.append(line)
            continue
        if stripped.startswith("#"):
            if stripped.startswith("#EXT-X-BYTERANGE:"):
                # Each range is downloaded into its own local segment file, so
                # remove the original offset tag from the rewritten playlist.
                byte_range = stripped.split(":", 1)[1]
                continue
            if stripped.startswith("#EXT-X-KEY:") and "URI=" in stripped:
                method = re.search(r"METHOD=([^,]+)", stripped)
                if method and method.group(1).upper() not in {"AES-128", "NONE"}:
                    raise RuntimeError("This HLS encryption method is not supported.")
                uri_match = re.search(r'URI="([^"]+)"', stripped)
                if uri_match and method and method.group(1).upper() == "AES-128":
                    key_name = "hls-key.bin"
                    schedule_resource(_resolve_hls_uri(playlist_url, uri_match.group(1)), key_name)
                    line = line.replace(uri_match.group(0), f'URI="{key_name}"')
            if stripped.startswith("#EXT-X-MAP:"):
                uri_match = re.search(r'URI="([^"]+)"', stripped)
                if uri_match:
                    init_name = "hls-init.mp4"
                    schedule_resource(_resolve_hls_uri(playlist_url, uri_match.group(1)), init_name)
                    line = line.replace(uri_match.group(0), f'URI="{init_name}"')
            local_lines.append(line)
            continue

        segment_index += 1
        suffix = Path(urlparse(stripped).path).suffix or ".bin"
        filename = f"segment-{segment_index:06d}{suffix}"
        range_header = None
        if byte_range:
            match = re.fullmatch(r"(\d+)(?:@(\d+))?", byte_range)
            if not match:
                raise RuntimeError("Invalid HLS byte-range declaration.")
            length = int(match.group(1))
            start = int(match.group(2)) if match.group(2) else previous_range_end
            end = start + length - 1
            range_header = f"bytes={start}-{end}"
            previous_range_end = end + 1
            byte_range = None
        schedule_resource(_resolve_hls_uri(playlist_url, stripped), filename, range_header)
        local_lines.append(filename)

    if not segment_index:
        raise RuntimeError("HLS playlist did not contain any media segments.")
    if len(downloads) > 20000:
        raise RuntimeError("HLS playlist contains too many media resources.")

    total_bytes = 0
    completed_resources = 0
    progress_lock = threading.Lock()
    manifest_cookies = session.cookies.copy()
    def fetch_one(item):
        remote_url, local_path, range_header = item
        request_headers = dict(headers)
        if range_header:
            request_headers["Range"] = range_header
        last_error = None
        for attempt in range(2):
            try:
                worker_session = requests.Session()
                worker_session.headers.update(request_headers)
                worker_session.cookies.update(manifest_cookies)
                response = worker_session.get(remote_url, timeout=(15, 90), stream=True)
                response.raise_for_status()
                expected_status = 206 if range_header else None
                if expected_status and response.status_code != expected_status:
                    raise RuntimeError("CDN did not honor the HLS byte-range request.")
                written = 0
                with local_path.open("wb") as output_file:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > 128 * 1024 * 1024:
                            raise RuntimeError("An HLS segment exceeded the 128 MiB safety limit.")
                        output_file.write(chunk)
                response.close()
                worker_session.close()
                if not written:
                    raise RuntimeError("CDN returned an empty HLS segment.")
                return written
            except Exception as exc:
                last_error = exc
                local_path.unlink(missing_ok=True)
                if attempt == 0:
                    time.sleep(0.5)
        raise RuntimeError("HLS segment request failed after retry: " + str(last_error)[:220])

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_one, item) for item in downloads]
        for future in concurrent.futures.as_completed(futures):
            written = future.result()
            with progress_lock:
                total_bytes += written
                completed_resources += 1
                if total_bytes > 2 * 1024 * 1024 * 1024:
                    raise RuntimeError("Pornhub HLS media exceeds the 2 GiB safety limit.")
                if progress_hook:
                    progress_hook({"status": "downloading", "downloaded_bytes": total_bytes, "progress": round(2 + 92 * completed_resources / len(downloads), 1)})

    local_playlist = work_dir / "local.m3u8"
    local_playlist.write_text("\n".join(local_lines) + "\n", encoding="utf-8")
    return local_playlist


def _download_direct_file(media_url: str, destination: Path, referer: str) -> None:
    headers = {"User-Agent": UA, "Referer": referer, "Origin": "https://www.pornhub.com", "Accept": "*/*"}
    response = requests.get(media_url, headers=headers, timeout=(15, 90), stream=True)
    response.raise_for_status()
    total = 0
    with destination.open("wb") as output_file:
        for chunk in response.iter_content(chunk_size=256 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > 2 * 1024 * 1024 * 1024:
                raise RuntimeError("Pornhub media exceeds the 2 GiB safety limit.")
            output_file.write(chunk)
    response.close()
    if not total:
        raise RuntimeError("CDN returned an empty media file.")



def _validate_pornhub_output(path: Path, mode: str, expected_duration: int | None = None) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=45
    )
    if probe.returncode:
        raise RuntimeError("FFprobe could not validate the downloaded media file.")
    metadata = json.loads(probe.stdout)
    stream_types = {x.get("codec_type") for x in metadata.get("streams", [])}
    required = "audio" if mode == "audio" else "video"
    if required not in stream_types:
        raise RuntimeError(f"Downloaded file does not contain the required {required} stream.")
    actual_duration = float(metadata.get("format", {}).get("duration") or 0)
    if expected_duration and actual_duration < expected_duration * 0.85:
        raise RuntimeError(f"CDN returned only a partial preview ({actual_duration:.1f}s of about {expected_duration}s); refusing to mark it as a full download.")
    return actual_duration


def download_pornhub(url: str, destination: Path, mode: str, output: str, progress_hook=None, format_id: str | None = None) -> dict:
    if mode not in {"video", "audio"}:
        raise RuntimeError("Pornhub mode must be video or audio.")
    if mode == "audio" and output != "mp3":
        raise RuntimeError("Pornhub audio downloads require MP3 output.")
    if mode == "video" and output not in {"mp4", "webm"}:
        raise RuntimeError("Pornhub video output must be MP4 or WebM.")

    session, data = _extract(url)
    title_check = (data.get("title") or "").strip().lower()
    if not data.get("duration_seconds") or title_check in {"", "pornhub", "pornhub video"}:
        session, data = _extract(url, force_refresh=True)
        title_check = (data.get("title") or "").strip().lower()
    if not data.get("duration_seconds") or title_check in {"", "pornhub", "pornhub video"}:
        detail = f" Watch-page response: {data.get('_page_error')}." if data.get("_page_error") else ""
        raise RuntimeError("Only an incomplete Pornhub embed/preview was exposed. The full watch-page stream is unavailable right now; retry later." + detail)
    streams = list(data["streams"])
    if not streams:
        raise RuntimeError("Pornhub did not expose any public media streams.")
    selected = next((x for x in streams if x["format_id"] == str(format_id)), None) if format_id else None
    if format_id and selected is None:
        raise RuntimeError("The selected Pornhub quality is no longer available. Refresh the format list.")
    if selected is None:
        selected = max(streams, key=lambda x: x.get("height") or 0)

    if progress_hook:
        progress_hook({"status": "resolved", "title": data.get("title"), "format_count": len(streams)})

    # If the preferred HLS URL has expired or its CDN returns 4xx, try the
    # other public qualities before giving up. Refresh player metadata once
    # after the first pass so short-lived signed URLs can be renewed.
    def candidate_order(items, preferred, explicit):
        same = [x for x in items if x.get("height") == preferred.get("height") and x.get("url") != preferred.get("url")]
        lower = [] if explicit else sorted(
            [x for x in items if (x.get("height") or 0) < (preferred.get("height") or 0)],
            key=lambda x: x.get("height") or 0, reverse=True
        )
        higher = [] if explicit else sorted(
            [x for x in items if (x.get("height") or 0) > (preferred.get("height") or 0)],
            key=lambda x: x.get("height") or 0, reverse=True
        )
        return [preferred] + same + lower + higher

    attempts = candidate_order(streams, selected, bool(format_id))
    attempted_urls = set()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    headers = f"User-Agent: {UA}\r\nReferer: {url}\r\nOrigin: https://www.pornhub.com\r\nAccept: */*\r\n"
    last_error = None
    success = None

    for refresh_pass in range(2):
        for candidate in attempts:
            media_url = candidate.get("url")
            if not media_url or media_url in attempted_urls:
                continue
            attempted_urls.add(media_url)
            destination.unlink(missing_ok=True)
            temp_root = Path(tempfile.mkdtemp(prefix="kangodl-pornhub-", dir=str(destination.parent)))
            try:
                if ".m3u8" in urlparse(media_url).path.lower():
                    local_input = _download_hls_playlist(media_url, temp_root, url, progress_hook)
                else:
                    local_input = temp_root / ("source" + (Path(urlparse(media_url).path).suffix or ".mp4"))
                    _download_direct_file(media_url, local_input, url)
                args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(local_input)]
                if mode == "audio":
                    args += ["-vn", "-codec:a", "libmp3lame", "-b:a", "192k", str(destination)]
                elif output == "mp4":
                    args += ["-c", "copy", "-movflags", "+faststart", str(destination)]
                else:
                    args += ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-crf", "32", "-b:v", "0", "-c:a", "libopus", str(destination)]
                process = subprocess.run(args, capture_output=True, text=True, timeout=900)
                if process.returncode:
                    raise RuntimeError((process.stderr or "FFmpeg failed")[-700:])
                if not destination.exists() or destination.stat().st_size == 0:
                    raise RuntimeError("FFmpeg produced an empty output file.")
                if destination.stat().st_size > 2 * 1024 * 1024 * 1024:
                    raise RuntimeError("Pornhub media exceeds the 2 GiB safety limit.")
                actual_duration = _validate_pornhub_output(destination, mode, data.get("duration_seconds"))
                success = candidate
                break
            except Exception as exc:
                last_error = exc
                destination.unlink(missing_ok=True)
                continue
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)
        if success:
            break
        if refresh_pass == 0:
            try:
                refreshed_session, refreshed = _extract(url, force_refresh=True)
                data = refreshed
                fresh_streams = list(refreshed.get("streams", []))
                fresh_selected = next((x for x in fresh_streams if x.get("format_id") == str(format_id)), None) if format_id else None
                if not format_id and fresh_streams:
                    fresh_selected = max(fresh_streams, key=lambda x: x.get("height") or 0)
                attempts = candidate_order(fresh_streams, fresh_selected, bool(format_id)) if fresh_selected else []
            except Exception as exc:
                last_error = exc
                attempts = []

    if not success:
        destination.unlink(missing_ok=True)
        raise RuntimeError("All available public Pornhub stream URLs failed, including refreshed URLs. Last error: " + str(last_error or "no usable stream")[-500:])

    size = destination.stat().st_size
    return {
        "title": data.get("title"),
        "duration_seconds": data.get("duration_seconds"),
        "thumbnail": data.get("thumbnail"),
        "format_id": success.get("format_id"),
        "height": success.get("height"),
        "bytes": size,
        "actual_duration_seconds": actual_duration,
    }
