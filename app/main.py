from __future__ import annotations
import asyncio, os, re, uuid, threading, subprocess, json, time, hmac, secrets, http.cookiejar
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request as URLRequest, urlopen
from fastapi import FastAPI, File, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from app.extractors.youtube import ExtractorError, extract_youtube_info, video_id_from_url
from app.extractors.tiktok import is_tiktok_url, extract_tiktok_info, download_tiktok, download_tiktok_photos
from app.extractors.pornhub import is_pornhub_url, extract_pornhub_info, download_pornhub
from app.extractors.pornhub_search import search_pornhub
from app.extractors.pinterest import is_pinterest_url, extract_pinterest_info, download_pinterest
from app.extractors.pinterest_search import search_pinterest
from app.extractors.audiomack import is_audiomack_url, extract_audiomack_info, search_audiomack, download_audiomack
from app.extractors.instagram import is_instagram_url, extract_instagram_info, download_instagram
from app.extractors.snapchat import is_snapchat_url, extract_snapchat_info, download_snapchat
from app.extractors.facebook import is_facebook_url, extract_facebook_info, download_facebook
from app.extractors.x import is_x_url, extract_x_info, download_x
from app.extractors.reddit import is_reddit_url, extract_reddit_info, download_reddit
from app.extractors.strategy import STRATEGIES

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = Path(os.getenv("KANGODL_STORAGE", str(BASE_DIR / "storage"))).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("KANGODL_MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))
MAX_DOWNLOAD_BYTES = int(os.getenv("KANGODL_MAX_DOWNLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_SEMAPHORE = asyncio.Semaphore(1)
DOWNLOAD_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kangodl-download")
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOBS_FILE = STORAGE_DIR / "jobs.json"
try:
    if JOBS_FILE.exists():
        loaded = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            JOBS.update({k: v for k, v in loaded.items() if isinstance(v, dict)})
except Exception:
    JOBS.clear()

# A job running when the process died cannot resume safely; report it honestly.
for _job in JOBS.values():
    if _job.get("status") in {"queued", "extracting", "downloading", "processing"}:
        _job.update(status="failed", stage="Interrupted by service restart", error="The service restarted before this job finished.")
        _job.pop("download_path", None)

def _persist_jobs_locked():
    temporary = JOBS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(JOBS), encoding="utf-8")
    temporary.replace(JOBS_FILE)
app = FastAPI(title="KangoDL Engine", description="Independent media extraction and processing engine.", version="0.8.0")
API_KEY = os.getenv("KANGODL_API_KEY", "").strip()
REQUIRE_API_KEY = os.getenv("KANGODL_REQUIRE_API_KEY", "0").lower() in {"1", "true", "yes"}

@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if REQUIRE_API_KEY and not API_KEY:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": "API key is not configured on this service."})
    if API_KEY:
        supplied = request.headers.get("x-api-key", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not hmac.compare_digest(supplied, API_KEY):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "A valid API key is required."})
    return await call_next(request)


AUTH_DIR = (STORAGE_DIR / "auth").resolve()
AUTH_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(AUTH_DIR, 0o700)
YOUTUBE_COOKIE_PATH = AUTH_DIR / "youtube.cookies"
AUTH_FIELDS = {
    "reddit": {"REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"},
    "pinterest": {"PINTEREST_ACCESS_TOKEN"},
    "audiomack": {"AUDIOMACK_CONSUMER_KEY", "AUDIOMACK_CONSUMER_SECRET"},
}
AUTH_STATUS_FIELDS = {
    "reddit": ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
    "pinterest": ("PINTEREST_ACCESS_TOKEN",),
    "audiomack": ("AUDIOMACK_CONSUMER_KEY", "AUDIOMACK_CONSUMER_SECRET"),
}

def _auth_admin(request: Request):
    if not API_KEY:
        raise HTTPException(status_code=503, detail="Set KANGODL_API_KEY in the service .env before using authentication settings.")
    supplied = request.headers.get("x-api-key", "")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, API_KEY):
        raise HTTPException(status_code=401, detail="A valid API key is required.")

def _write_env_values(values: dict[str, str]):
    env_path = BASE_DIR / ".env"
    current = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updates = {k: json.dumps(v, ensure_ascii=True) for k, v in values.items()}
    seen = set()
    output = []
    for line in current:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in updates:
            if key not in seen:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    temporary = env_path.with_suffix(".env.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(env_path)
    os.chmod(env_path, 0o600)

@app.get("/auth/status")
def auth_status(request: Request):
    _auth_admin(request)
    platforms = {}
    for platform, fields in AUTH_STATUS_FIELDS.items():
        platforms[platform] = {"configured": all(bool(os.getenv(field, "").strip()) for field in fields), "fields": list(fields)}
    platforms["youtube"] = {"configured": YOUTUBE_COOKIE_PATH.is_file() and YOUTUBE_COOKIE_PATH.stat().st_size > 0, "method": "Netscape cookie file"}
    for platform in ("tiktok", "instagram", "facebook", "x", "snapchat", "pornhub"):
        platforms[platform] = {"configured": False, "method": "No supported account-auth integration; public extraction only"}
    return {"ok": True, "platforms": platforms, "restart_required_after_changes": True}

@app.put("/auth/credentials/{platform}")
def save_platform_credentials(platform: str, payload: dict, request: Request):
    _auth_admin(request)
    allowed = AUTH_FIELDS.get(platform.lower())
    if not allowed:
        raise HTTPException(status_code=400, detail="Supported credential profiles: reddit, pinterest, audiomack.")
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="Provide at least one supported credential field.")
    if set(payload) - allowed:
        raise HTTPException(status_code=400, detail="One or more credential fields are not supported for this platform.")
    values = {}
    for key, value in payload.items():
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            raise HTTPException(status_code=400, detail=f"{key} must be a non-empty single-line string.")
        values[key] = value.strip()
    _write_env_values(values)
    return {"ok": True, "platform": platform.lower(), "saved_fields": sorted(values), "secrets_returned": False, "restart_required": True}

@app.post("/auth/youtube/cookies")
async def upload_youtube_cookies(request: Request, file: UploadFile = File(...)):
    _auth_admin(request)
    raw = await file.read(1_048_577)
    await file.close()
    if not raw or len(raw) > 1_048_576:
        raise HTTPException(status_code=413, detail="Cookie file must be between 1 byte and 1 MiB.")
    temporary = AUTH_DIR / "youtube.cookies.upload"
    try:
        temporary.write_bytes(raw)
        os.chmod(temporary, 0o600)
        jar = http.cookiejar.MozillaCookieJar(str(temporary))
        jar.load(ignore_discard=True, ignore_expires=False)
        if not list(jar):
            raise ValueError("No valid, unexpired cookies were found.")
        os.replace(temporary, YOUTUBE_COOKIE_PATH)
        os.chmod(YOUTUBE_COOKIE_PATH, 0o600)
        _write_env_values({"KANGODL_YOUTUBE_COOKIE_FILE": str(YOUTUBE_COOKIE_PATH)})
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Invalid Netscape/Mozilla cookie file or no usable cookies.") from exc
    return {"ok": True, "platform": "youtube", "cookie_count": len(list(jar)), "secrets_returned": False, "restart_required": True}

@app.delete("/auth/youtube/cookies")
def delete_youtube_cookies(request: Request):
    _auth_admin(request)
    YOUTUBE_COOKIE_PATH.unlink(missing_ok=True)
    _write_env_values({"KANGODL_YOUTUBE_COOKIE_FILE": ""})
    return {"ok": True, "platform": "youtube", "cookies_removed": True, "restart_required": True}

OUTPUT_FORMATS = {"mp3": ".mp3", "mp4": ".mp4", "webm": ".webm", "zip": ".zip", "jpg": ".jpg"}

class DownloadRequest(BaseModel):
    url: str
    format_id: str | None = None
    mode: str = Field(default="video", pattern="^(video|audio|photos|image)$")
    output: str = Field(default="mp4", pattern="^(mp4|mp3|webm|zip|jpg)$")

def resolve_media(file_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", file_id):
        raise HTTPException(status_code=404, detail="Media not found.")
    matches = list(STORAGE_DIR.glob(file_id + ".*"))
    if len(matches) != 1 or not matches[0].is_file():
        raise HTTPException(status_code=404, detail="Media not found.")
    return matches[0]

def _job_update(job_id: str, **values):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)
            _persist_jobs_locked()

def _download_url(url: str, destination: Path, job_id: str, label: str, user_agent: str | None = None):
    user_agents = ([user_agent] if user_agent else []) + [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "com.google.android.youtube/20.10.38 (Linux; U; Android 15) gzip",
        "com.google.ios.youtube/20.10.4",
    ]
    last_error = None
    chunk_size = 4 * 1024 * 1024
    range_workers = 4

    for current_ua in dict.fromkeys(user_agents):
        destination.unlink(missing_ok=True)
        base_headers = {
            "User-Agent": current_ua, "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/", "Accept-Encoding": "identity",
        }
        try:
            # Probe byte-range support and learn the exact object size. YouTube's
            # GVS endpoints often throttle a single long response, while bounded
            # range requests are reliable and can be safely retried independently.
            probe = URLRequest(url, headers={**base_headers, "Range": "bytes=0-0"})
            with urlopen(probe, timeout=35) as response:
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes 0-0/(\d+)", content_range)
                if response.status == 206 and match:
                    total_size = int(match.group(1))
                    response.read(1)
                else:
                    total_size = 0
            if total_size:
                if total_size > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("Download exceeds the configured 2 GiB safety limit.")
                with destination.open("wb") as output:
                    output.truncate(total_size)
                fd = os.open(destination, os.O_WRONLY)
                completed = 0
                try:
                    def fetch_range(start: int, end: int):
                        request = URLRequest(url, headers={**base_headers, "Range": f"bytes={start}-{end}"})
                        with urlopen(request, timeout=60) as response:
                            expected_range = f"bytes {start}-{end}/{total_size}"
                            if response.status != 206 or response.headers.get("Content-Range") != expected_range:
                                raise RuntimeError(f"Media server returned an unexpected range for bytes {start}-{end}.")
                            data = response.read(end - start + 1)
                            if len(data) != end - start + 1:
                                raise RuntimeError(f"Media range was incomplete at byte {start}.")
                            return start, data

                    ranges = [(start, min(start + chunk_size - 1, total_size - 1)) for start in range(0, total_size, chunk_size)]
                    with ThreadPoolExecutor(max_workers=range_workers, thread_name_prefix="kangodl-range") as pool:
                        futures = [pool.submit(fetch_range, start, end) for start, end in ranges]
                        for future in as_completed(futures):
                            start, data = future.result()
                            written = os.pwrite(fd, data, start)
                            if written != len(data):
                                raise RuntimeError(f"Could not write the complete media range at byte {start}.")
                            completed += written
                            _job_update(job_id, stage=label, bytes_downloaded=completed, progress=round(min(99, completed * 100 / total_size), 1))
                finally:
                    os.close(fd)
                if completed != total_size or destination.stat().st_size != total_size:
                    raise RuntimeError("The ranged download did not produce the complete media object.")
                return total_size

            # Some origins do not support byte ranges; use a normal full response.
            request = URLRequest(url, headers=base_headers)
            total = 0
            with urlopen(request, timeout=60) as response, destination.open("wb") as out:
                expected = int(response.headers.get("Content-Length", "0") or 0)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError("Download exceeds the configured 2 GiB safety limit.")
                    out.write(chunk)
                    if expected:
                        _job_update(job_id, stage=label, bytes_downloaded=total, progress=round(min(99, total * 100 / expected), 1))
            if total:
                return total
            last_error = RuntimeError("The source returned an empty media stream.")
        except Exception as exc:
            last_error = exc
            destination.unlink(missing_ok=True)
    raise RuntimeError(f"All direct stream request profiles failed ({type(last_error).__name__}: {last_error}).")

def _run_download_job(job_id: str, request_data: dict):
    work_id = job_id
    video_path = STORAGE_DIR / f"{work_id}.video.part"
    audio_path = STORAGE_DIR / f"{work_id}.audio.part"
    final_path = STORAGE_DIR / f"{work_id}{OUTPUT_FORMATS[request_data['output']]}"
    try:
        if is_reddit_url(request_data["url"]):
            _job_update(job_id, status="extracting", stage="Resolving public Reddit post", progress=1)
            def reddit_progress(data):
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or 0
                    downloaded = data.get("downloaded_bytes") or 0
                    _job_update(job_id, status="downloading", stage="Downloading Reddit media", bytes_downloaded=downloaded, progress=round(min(94, downloaded * 94 / total), 1) if total else 5)
            result = download_reddit(request_data["url"], final_path, request_data["mode"], request_data["output"], reddit_progress, request_data.get("format_id"))
            _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=result["bytes"], selected_format=result["format_id"], title=result.get("title"), author=result.get("author"), thumbnail=result.get("thumbnail"), duration_seconds=result.get("duration_seconds"), media_type=result.get("media_type", "video"), item_count=result.get("item_count", 1), download_path=f"/jobs/{job_id}/file", filename=(result.get("title") or "kangodl-reddit") + final_path.suffix, extraction_method=result.get("extraction_method"))
            return
        if is_x_url(request_data["url"]):
            _job_update(job_id, status="extracting", stage="Resolving public X video", progress=1)
            def x_progress(data):
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or 0
                    downloaded = data.get("downloaded_bytes") or 0
                    _job_update(job_id, status="downloading", stage="Downloading X video", bytes_downloaded=downloaded, progress=round(min(94, downloaded * 94 / total), 1) if total else 5)
            _job_update(job_id, status="downloading", stage="Downloading X video", progress=2)
            result = download_x(request_data["url"], final_path, request_data["mode"], request_data["output"], x_progress, request_data.get("format_id"))
            _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=result["bytes"], selected_format=result["format_id"], title=result.get("title"), author=result.get("author"), thumbnail=result.get("thumbnail"), duration_seconds=result.get("duration_seconds"), media_type="video", download_path=f"/jobs/{job_id}/file", filename=(result.get("title") or "kangodl-x") + final_path.suffix, extraction_method=result.get("extraction_method"))
            return
        if is_facebook_url(request_data["url"]):
            _job_update(job_id, status="extracting", stage="Resolving public Facebook video", progress=1)
            def facebook_progress(data):
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or 0
                    downloaded = data.get("downloaded_bytes") or 0
                    _job_update(job_id, status="downloading", stage="Downloading Facebook video", bytes_downloaded=downloaded, progress=round(min(94, downloaded * 94 / total), 1) if total else 5)
            _job_update(job_id, status="downloading", stage="Downloading Facebook video", progress=2)
            result = download_facebook(request_data["url"], final_path, request_data["mode"], request_data["output"], facebook_progress)
            _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=result["bytes"], selected_format=result["format_id"], title=result.get("title"), thumbnail=result.get("thumbnail"), media_type="video", download_path=f"/jobs/{job_id}/file", filename=(result.get("title") or "kangodl-facebook") + final_path.suffix, extraction_method=result.get("extraction_method"))
            return
        if is_snapchat_url(request_data["url"]):
            _job_update(job_id, status="extracting", stage="Resolving public Snapchat Spotlight", progress=1)
            def snapchat_progress(data):
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or 0
                    downloaded = data.get("downloaded_bytes") or 0
                    _job_update(job_id, status="downloading", stage="Downloading Snapchat Spotlight", bytes_downloaded=downloaded, progress=round(min(90, downloaded * 90 / total), 1) if total else 5)
            _job_update(job_id, status="downloading", stage="Downloading Snapchat Spotlight", progress=2)
            result = download_snapchat(request_data["url"], final_path, request_data["mode"], request_data["output"], snapchat_progress)
            _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=result["bytes"], selected_format=result["format_id"], title=result.get("title"), thumbnail=result.get("thumbnail"), media_type="video", download_path=f"/jobs/{job_id}/file", filename=(result.get("title") or "kangodl-snapchat") + final_path.suffix, extraction_method=result.get("extraction_method"))
            return
        if is_instagram_url(request_data["url"]):
            _job_update(job_id, status="extracting", stage="Resolving public Instagram media", progress=1)
            def instagram_progress(data):
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or 0
                    downloaded = data.get("downloaded_bytes") or 0
                    _job_update(job_id, status="downloading", stage="Downloading Instagram media", bytes_downloaded=downloaded, progress=round(min(90, downloaded * 90 / total), 1) if total else 5)
            _job_update(job_id, status="downloading", stage="Downloading Instagram media", progress=2)
            result = download_instagram(request_data["url"], final_path, request_data["mode"], request_data["output"], instagram_progress)
            _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=result["bytes"], selected_format=result["format_id"], title=result.get("title"), thumbnail=result.get("thumbnail"), media_type=result.get("media_type"), item_count=result.get("item_count"), download_path=f"/jobs/{job_id}/file", filename=(result.get("title") or "kangodl-instagram") + final_path.suffix, extraction_method=result.get("extraction_method"))
            return
        if is_pinterest_url(request_data["url"]):
            _job_update(job_id, status="extracting", stage="Resolving Pinterest pin", progress=1)
            def pinterest_progress(data):
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or 0; downloaded = data.get("downloaded_bytes") or 0
                    _job_update(job_id, progress=round(min(94, downloaded * 94 / total), 1) if total else 5, bytes_downloaded=downloaded)
            _job_update(job_id, status="downloading", stage="Downloading Pinterest media", progress=2)
            result = download_pinterest(request_data["url"], final_path, request_data["mode"], request_data["output"], pinterest_progress)
            _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=result["bytes"], selected_format=result["format_id"], title=result.get("title"), thumbnail=result.get("thumbnail"), duration_seconds=result.get("duration_seconds"), media_type=result.get("media_type"), download_path=f"/jobs/{job_id}/file", filename=(result.get("title") or "kangodl-pinterest") + final_path.suffix, extraction_method=result.get("extraction_method"))
            return
        if is_pornhub_url(request_data["url"]):
            def pornhub_progress(data):
                if data.get("status") == "resolved":
                    _job_update(job_id, status="downloading", stage="Downloading resolved Pornhub stream", progress=2, title=data.get("title"))
                elif data.get("status") == "downloading":
                    total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                    downloaded = data.get("downloaded_bytes") or 0
                    percent = data.get("progress")
                    if percent is None:
                        percent = round(min(94, downloaded * 94 / total), 1) if total else 5
                    _job_update(job_id, progress=max(2, percent), bytes_downloaded=downloaded)
            _job_update(job_id, status="extracting", stage="Resolving public Pornhub stream", progress=1)
            result = download_pornhub(request_data["url"], final_path, request_data["mode"], request_data["output"], pornhub_progress, request_data.get("format_id"))
            _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=result["bytes"], selected_format=result["format_id"], title=result.get("title"), author=result.get("author"), thumbnail=result.get("thumbnail"), duration_seconds=result.get("duration_seconds"), download_path=f"/jobs/{job_id}/file", filename=(result.get("title") or "kangodl-pornhub") + final_path.suffix, extraction_method=result.get("extraction_method"))
            return
        if is_audiomack_url(request_data["url"]):
            _job_update(job_id, status="extracting", stage="Resolving Audiomack track", progress=1)
            def audiomack_progress(data):
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or 0
                    downloaded = data.get("downloaded_bytes") or 0
                    _job_update(job_id, status="downloading", stage="Downloading Audiomack audio", bytes_downloaded=downloaded, progress=round(min(90, downloaded * 90 / total), 1) if total else 5)
            _job_update(job_id, status="downloading", stage="Downloading Audiomack audio", progress=2)
            result = download_audiomack(request_data["url"], final_path, request_data["output"], audiomack_progress)
            _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=result["bytes"], selected_format=result["format_id"], title=result.get("title"), author=result.get("author"), thumbnail=result.get("thumbnail"), duration_seconds=result.get("duration_seconds"), media_type="audio", download_path=f"/jobs/{job_id}/file", filename=(result.get("title") or "kangodl-audiomack") + final_path.suffix, extraction_method=result.get("extraction_method"))
            return
        if is_tiktok_url(request_data["url"]):
            if request_data["mode"] == "photos":
                if request_data["output"] != "zip":
                    raise RuntimeError("TikTok photo mode requires ZIP output.")
                _job_update(job_id, status="extracting", stage="Resolving TikTok photo slideshow", progress=1)
                result = download_tiktok_photos(request_data["url"], final_path)
                _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=result["bytes"], photo_count=result["photo_count"], selected_format=result["format_id"], download_path=f"/jobs/{job_id}/file", filename=f"tiktok-photos-{job_id[:8]}.zip", media_type="photo_slideshow", extraction_method=result.get("extraction_method"))
                return
            _job_update(job_id, status="extracting", stage="Resolving TikTok video", progress=1)
            info = extract_tiktok_info(request_data["url"])
            _job_update(job_id, title=info.get("title"), author=info.get("author"), thumbnail=info.get("thumbnail"), duration_seconds=info.get("duration_seconds"), extraction_method=(info.get("extraction_strategy") or {}).get("selected"), status="downloading", stage="Downloading watermark-free TikTok media" if request_data["mode"] == "video" else "Downloading TikTok audio", progress=2)
            def tiktok_progress(data):
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                    downloaded = data.get("downloaded_bytes") or 0
                    percent = round(min(94, downloaded * 94 / total), 1) if total else 5
                    _job_update(job_id, progress=max(2, percent), bytes_downloaded=downloaded)
            result = download_tiktok(request_data["url"], final_path, request_data["mode"], tiktok_progress, request_data.get("format_id"))
            _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=result["bytes"], selected_format=result["format_id"], height=result.get("height"), width=result.get("width"), download_path=f"/jobs/{job_id}/file", filename=(result.get("title") or "kangodl-tiktok") + final_path.suffix, watermark_free=(request_data["mode"] == "video"), extraction_method=result.get("extraction_method"))
            return
        _job_update(job_id, status="extracting", stage="Refreshing YouTube stream information", progress=1)
        info = extract_youtube_info(request_data["url"])
        formats = info["_internal_formats"]
        selected = next((f for f in formats if f["format_id"] == str(request_data.get("format_id")) and f.get("_direct_url")), None) if request_data.get("format_id") else None
        if request_data["mode"] == "audio":
            audio_candidates = [f for f in formats if f.get("has_audio") and not f.get("has_video") and f.get("_direct_url")]
            if not audio_candidates:
                audio_candidates = [f for f in formats if f.get("has_audio") and f.get("_direct_url")]
            if not audio_candidates:
                raise RuntimeError("No directly downloadable audio stream was returned.")
            selected = next((f for f in audio_candidates if f["format_id"] == str(request_data.get("format_id"))), None) if request_data.get("format_id") else None
            if selected is None:
                selected = max(audio_candidates, key=lambda f: f.get("bitrate") or 0)
        elif selected is None:
            video_candidates = [f for f in formats if f.get("has_video") and f.get("height") and f.get("_direct_url")]
            if not video_candidates:
                raise RuntimeError("No directly downloadable video format was returned. Signature deciphering may be required.")
            # Prefer a muxed stream for the default choice. It can be delivered
            # without depending on a separate adaptive audio URL.
            muxed_candidates = [f for f in video_candidates if f.get("has_audio")]
            selected = max(muxed_candidates or video_candidates, key=lambda f: f.get("height") or 0)
        if not selected or not selected.get("_direct_url"):
            raise RuntimeError("That format is not directly downloadable at this time. Choose a format with a direct stream URL.")
        if request_data["mode"] == "audio" and not selected.get("has_audio"):
            raise RuntimeError("The selected format does not contain audio.")
        if request_data["mode"] == "video" and not selected.get("has_video"):
            raise RuntimeError("The selected format does not contain video.")
        _job_update(job_id, title=info.get("title"), selected_format=selected["format_id"], status="downloading", stage="Downloading selected stream", progress=2)
        audio_source = audio_path
        if request_data["mode"] == "audio":
            try:
                _download_url(selected["_direct_url"], audio_path, job_id, "Downloading audio", selected.get("_user_agent"))
            except Exception:
                # Some public player audio URLs reject full-object reads. Fall back
                # to a muxed stream when one is available, then extract its audio.
                audio_path.unlink(missing_ok=True)
                muxed = [f for f in formats if f.get("has_audio") and f.get("has_video") and f.get("_direct_url")]
                if not muxed:
                    raise
                fallback = max(muxed, key=lambda f: (f.get("height") or 0, f.get("bitrate") or 0))
                _job_update(job_id, stage="Audio stream unavailable; downloading combined stream", selected_format=fallback["format_id"], progress=2)
                _download_url(fallback["_direct_url"], video_path, job_id, "Downloading combined stream", fallback.get("_user_agent"))
                audio_source = video_path
        else:
            try:
                _download_url(selected["_direct_url"], video_path, job_id, "Downloading video", selected.get("_user_agent"))
            except Exception as stream_error:
                if selected.get("has_audio"):
                    raise
                fallback_candidates = [f for f in formats if f.get("has_video") and f.get("has_audio") and f.get("_direct_url")]
                if not fallback_candidates:
                    raise RuntimeError("The selected video stream was blocked and no combined audio/video fallback is available.")
                fallback = max((f for f in fallback_candidates if (f.get("height") or 0) <= (selected.get("height") or 0)), key=lambda f: f.get("height") or 0, default=max(fallback_candidates, key=lambda f: f.get("height") or 0))
                fallback_reason = (
                    "YouTube returned HTTP 403 for the adaptive stream; a GVS PO Token may be required"
                    if getattr(stream_error, "code", None) == 403 or "HTTP Error 403" in str(stream_error)
                    else f"Requested adaptive stream failed ({type(stream_error).__name__})"
                )
                _job_update(job_id, status="downloading", stage="Adaptive video unavailable; switching to combined stream", selected_format=fallback["format_id"], fallback_reason=fallback_reason)
                selected = fallback
                _download_url(selected["_direct_url"], video_path, job_id, "Downloading combined fallback", selected.get("_user_agent"))
        if request_data["mode"] == "audio":
            _job_update(job_id, status="processing", stage="Extracting audio and converting to " + request_data["output"].upper(), progress=95)
            if request_data["output"] == "mp3":
                audio_args = ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
            elif request_data["output"] == "webm":
                audio_args = ["-vn", "-c:a", "libopus", "-b:a", "128k"]
            else:
                audio_args = ["-vn", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio_source)] + audio_args + [str(final_path)], check=True, timeout=900)
        elif selected.get("has_audio"):
            _job_update(job_id, status="processing", stage="Finalising " + request_data["output"].upper(), progress=95)
            if request_data["output"] == "mp4":
                output_args = ["-c", "copy", "-movflags", "+faststart"]
            elif request_data["output"] == "webm":
                output_args = ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-crf", "32", "-b:v", "0", "-c:a", "libopus"]
            else:
                raise RuntimeError("MP3 output requires audio mode.")
            subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path)] + output_args + [str(final_path)], check=True, timeout=1800)
        else:
            if request_data["output"] not in {"mp4", "webm"}:
                raise RuntimeError("Video downloads support MP4 or WebM output; use audio mode for MP3.")
            audio_candidates = [f for f in formats if f.get("has_audio") and not f.get("has_video") and f.get("_direct_url")]
            if not audio_candidates:
                raise RuntimeError("Video stream was downloaded, but YouTube did not provide a direct audio stream for merging.")
            preferred_audio_mime = "audio/mp4" if request_data["output"] == "mp4" else "audio/webm"
            audio = max(audio_candidates, key=lambda f: (f.get("mime_type") == preferred_audio_mime, f.get("bitrate") or 0))
            _job_update(job_id, stage="Downloading audio stream for merge", progress=55)
            try:
                _download_url(audio["_direct_url"], audio_path, job_id, "Downloading audio stream for merge", audio.get("_user_agent"))
            except Exception:
                # If the adaptive audio URL is denied by the CDN, fall back
                # to the best directly downloadable muxed format at or below
                # the selected video's height.
                audio_path.unlink(missing_ok=True)
                fallback_candidates = [f for f in formats if f.get("has_video") and f.get("has_audio") and f.get("_direct_url")]
                if not fallback_candidates:
                    raise RuntimeError("The separate audio stream was blocked and no combined audio/video fallback is available.")
                fallback = max((f for f in fallback_candidates if (f.get("height") or 0) <= (selected.get("height") or 0)), key=lambda f: f.get("height") or 0, default=max(fallback_candidates, key=lambda f: f.get("height") or 0))
                _job_update(job_id, status="downloading", stage="Adaptive audio unavailable; switching to combined stream", selected_format=fallback["format_id"], progress=5)
                _download_url(fallback["_direct_url"], video_path, job_id, "Downloading combined fallback", fallback.get("_user_agent"))
                _job_update(job_id, status="processing", stage="Finalising combined MP4", progress=95)
                subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path), "-c", "copy", "-movflags", "+faststart", str(final_path)], check=True, timeout=900)
            else:
                _job_update(job_id, status="processing", stage="Merging video and audio", progress=92)
                if request_data["output"] == "mp4":
                    merge_args = ["-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart"]
                elif selected.get("mime_type") == "video/webm" and audio.get("mime_type") == "audio/webm":
                    # Keep native VP9/Opus streams in their original WebM container.
                    merge_args = ["-c", "copy"]
                elif "opus" in (audio.get("codecs") or "").lower():
                    merge_args = ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-crf", "32", "-b:v", "0", "-c:a", "copy"]
                else:
                    merge_args = ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-crf", "32", "-b:v", "0", "-c:a", "libopus", "-b:a", "128k"]
                subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path), "-i", str(audio_path)] + merge_args + [str(final_path)], check=True, timeout=1800)
        if not final_path.exists() or final_path.stat().st_size == 0:
            raise RuntimeError("The media processor did not produce a valid output file.")
        _job_update(job_id, status="completed", stage="Ready", progress=100, bytes=final_path.stat().st_size, download_path=f"/jobs/{job_id}/file", filename=(info.get("title") or "kangodl") + final_path.suffix)
    except Exception as exc:
        _job_update(job_id, status="failed", stage="Failed", error=f"{type(exc).__name__}: {str(exc)[:350]}")
        final_path.unlink(missing_ok=True)
    finally:
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)

async def run_media_tool(*args: str, timeout: int = 900):
    async with MEDIA_SEMAPHORE:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill(); await process.communicate()
            raise HTTPException(status_code=504, detail="Media processing timed out.")
        return process.returncode or 0, stdout, stderr

@app.get("/strategies")
def extraction_strategies():
    """Expose the configured primary and fallback resolver for each platform."""
    return {"ok": True, "strategies": STRATEGIES}


@app.get("/health")
def health():
    return {"ok": True, "service": "KangoDL Engine", "version": app.version, "extractors": ["kangodl-youtube", "kangodl-tiktok-native", "kangodl-pornhub-native", "kangodl-pinterest-native", "kangodl-audiomack-native", "kangodl-instagram-native", "kangodl-snapchat-native", "kangodl-facebook-native", "kangodl-x-native", "kangodl-reddit-native"], "download_workers": 1}


@app.get("/search/audiomack")
def search_audiomack_route(query: str, limit: int = 20, page: int = 1, type: str = "songs"):
    try:
        return search_audiomack(query, limit=limit, page=page, kind=type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:350]) from exc


@app.get("/search/pinterest")
def search_pinterest_route(query: str, limit: int = 20, page: int = 1, cursor: str | None = None):
    try:
        return search_pinterest(query, limit=limit, page=page, cursor=cursor)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:350]) from exc


@app.get("/search")
@app.get("/search/pornhub")
def search(query: str, limit: int = 20, page: int = 1):
    try:
        return search_pornhub(query, limit=limit, page=page)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:350]) from exc


@app.get("/info")
def info(url: str):
    try:
        if is_reddit_url(url):
            return extract_reddit_info(url)
        if is_x_url(url):
            return extract_x_info(url)
        if is_facebook_url(url):
            return extract_facebook_info(url)
        if is_snapchat_url(url):
            return extract_snapchat_info(url)
        if is_instagram_url(url):
            return extract_instagram_info(url)
        if is_pornhub_url(url):
            return extract_pornhub_info(url)
        if is_audiomack_url(url):
            return extract_audiomack_info(url)
        if is_pinterest_url(url):
            return extract_pinterest_info(url)
        if is_tiktok_url(url):
            return extract_tiktok_info(url)
        data = extract_youtube_info(url)
        data.pop("_internal_formats", None)
        return data
    except (ExtractorError, Exception) as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:350]) from exc

@app.post("/jobs", status_code=202)
def create_download_job(payload: DownloadRequest):
    if is_reddit_url(payload.url):
        if payload.mode == "audio" and payload.output != "mp3":
            raise HTTPException(status_code=422, detail="Reddit audio downloads require MP3 output.")
        if payload.mode == "video" and payload.output not in {"mp4", "webm"}:
            raise HTTPException(status_code=422, detail="Reddit video downloads require MP4 or WebM output.")
        if payload.mode == "image" and payload.output != "jpg":
            raise HTTPException(status_code=422, detail="Reddit single-image downloads require JPG output.")
        if payload.mode == "photos" and payload.output != "zip":
            raise HTTPException(status_code=422, detail="Reddit gallery downloads require ZIP output.")
        if payload.mode not in {"audio", "video", "image", "photos"}:
            raise HTTPException(status_code=422, detail="Unsupported Reddit download mode.")
    if is_x_url(payload.url):
        if payload.mode == "audio" and payload.output != "mp3":
            raise HTTPException(status_code=422, detail="X audio downloads require MP3 output.")
        if payload.mode == "video" and payload.output not in {"mp4", "webm"}:
            raise HTTPException(status_code=422, detail="X video downloads require MP4 or WebM output.")
        if payload.mode not in {"audio", "video"}:
            raise HTTPException(status_code=422, detail="X currently supports public video and audio only.")
    if is_facebook_url(payload.url):
        if payload.mode == "audio" and payload.output != "mp3":
            raise HTTPException(status_code=422, detail="Facebook audio downloads require MP3 output.")
        if payload.mode == "video" and payload.output not in {"mp4", "webm"}:
            raise HTTPException(status_code=422, detail="Facebook video downloads require MP4 or WebM output.")
        if payload.mode not in {"audio", "video"}:
            raise HTTPException(status_code=422, detail="Facebook currently supports public video and audio only.")
    if is_audiomack_url(payload.url):
        if payload.mode != "audio" or payload.output != "mp3":
            raise HTTPException(status_code=422, detail="Audiomack currently supports audio mode with MP3 output only.")
    if is_snapchat_url(payload.url):
        if payload.mode == "audio" and payload.output != "mp3":
            raise HTTPException(status_code=422, detail="Snapchat audio downloads require MP3 output.")
        if payload.mode == "video" and payload.output not in {"mp4", "webm"}:
            raise HTTPException(status_code=422, detail="Snapchat video downloads require MP4 or WebM output.")
        if payload.mode not in {"audio", "video"}:
            raise HTTPException(status_code=422, detail="Snapchat currently supports public Spotlight video and audio only.")
    if is_instagram_url(payload.url):
        if payload.mode == "audio" and payload.output != "mp3":
            raise HTTPException(status_code=422, detail="Instagram audio downloads require MP3 output.")
        if payload.mode == "image" and payload.output != "jpg":
            raise HTTPException(status_code=422, detail="Instagram image downloads require JPG output.")
        if payload.mode == "video" and payload.output not in {"mp4", "webm"}:
            raise HTTPException(status_code=422, detail="Instagram video downloads require MP4 or WebM output.")
        if payload.mode == "photos" and payload.output != "zip":
            raise HTTPException(status_code=422, detail="Instagram carousel downloads require ZIP output.")
        if payload.mode not in {"audio", "image", "video", "photos"}:
            raise HTTPException(status_code=422, detail="Unsupported Instagram download mode.")
    if not (is_tiktok_url(payload.url) or is_pornhub_url(payload.url) or is_pinterest_url(payload.url) or is_audiomack_url(payload.url) or is_instagram_url(payload.url) or is_snapchat_url(payload.url) or is_facebook_url(payload.url) or is_x_url(payload.url) or is_reddit_url(payload.url)):
        if payload.mode == "photos" or payload.output == "zip":
            if not is_pinterest_url(payload.url):
                raise HTTPException(status_code=422, detail="Photo slideshow mode is only available for TikTok URLs.")
        try:
            video_id_from_url(payload.url)
        except ExtractorError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if is_pornhub_url(payload.url) and (payload.mode == "photos" or payload.output == "zip"):
        raise HTTPException(status_code=422, detail="Pornhub supports video and audio downloads, not photo slideshow mode.")
    if is_pornhub_url(payload.url):
        if payload.mode == "audio" and payload.output != "mp3":
            raise HTTPException(status_code=422, detail="Pornhub audio downloads require MP3 output.")
        if payload.mode == "video" and payload.output not in {"mp4", "webm"}:
            raise HTTPException(status_code=422, detail="Pornhub video downloads require MP4 or WebM output.")
    if is_pinterest_url(payload.url):
        if payload.mode == "photos" or payload.output == "zip":
            raise HTTPException(status_code=422, detail="Pinterest supports single-image and video pins, not TikTok-style photo slideshow ZIP mode.")
        if payload.mode == "image" and payload.output != "jpg":
            raise HTTPException(status_code=422, detail="Pinterest image downloads require JPG output.")
        if payload.mode == "video" and payload.output not in {"mp4", "webm"}:
            raise HTTPException(status_code=422, detail="Pinterest video downloads require MP4 or WebM output.")
        if payload.mode == "audio" and payload.output != "mp3":
            raise HTTPException(status_code=422, detail="Pinterest audio downloads require MP3 output.")
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        if sum(1 for item in JOBS.values() if item.get("status") in {"queued", "extracting", "downloading", "processing"}) >= 6:
            raise HTTPException(status_code=429, detail="The download queue is full. Try again shortly.")
        JOBS[job_id] = {"id": job_id, "status": "queued", "stage": "Waiting for download worker", "progress": 0, "requested_format": payload.format_id, "requested_output": payload.output, "created_at": time.time()}
        _persist_jobs_locked()
    DOWNLOAD_POOL.submit(_run_download_job, job_id, payload.model_dump())
    return {"ok": True, "job_id": job_id, "status_path": f"/jobs/{job_id}", "download_path": f"/jobs/{job_id}/file"}

@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return dict(job)

@app.get("/jobs/{job_id}/file")
def get_job_file(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("status") != "completed":
            raise HTTPException(status_code=409, detail="The download is not ready yet.")
        suffix = Path(job.get("filename", "kangodl.mp4")).suffix.lower()
        if suffix not in OUTPUT_FORMATS.values():
            suffix = ".mp4"
        path = STORAGE_DIR / f"{job_id}{suffix}"
        filename = re.sub(r'[^A-Za-z0-9._ -]', '_', job.get("filename", "kangodl" + path.suffix))[:160]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Completed file is no longer available.")
    return FileResponse(path, filename=filename)

@app.post("/media", status_code=201)
async def upload_media(file: UploadFile = File(...)):
    original_name = Path(file.filename or "media.bin").name
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(original_name).suffix)[:12]
    file_id = uuid.uuid4().hex
    destination = (STORAGE_DIR / f"{file_id}{suffix}").resolve()
    total = 0
    try:
        with destination.open("xb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk: break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES: raise HTTPException(status_code=413, detail="Upload exceeds configured size limit.")
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True); raise
    finally:
        await file.close()
    return {"ok": True, "id": file_id, "filename": original_name, "bytes": total, "download_path": f"/media/{file_id}"}

@app.get("/media/{file_id}/probe")
async def probe_media(file_id: str):
    import json
    source = resolve_media(file_id)
    code, stdout, _ = await run_media_tool("ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source), timeout=60)
    if code != 0: raise HTTPException(status_code=422, detail="FFprobe could not inspect this media file.")
    return {"ok": True, "id": file_id, "probe": json.loads(stdout.decode("utf-8"))}

@app.post("/media/{file_id}/convert")
async def convert_media(file_id: str, format: str):
    source = resolve_media(file_id)
    output_format = format.lower()
    if output_format not in {"mp3", "mp4", "webm"}: raise HTTPException(status_code=400, detail="Supported output formats: mp3, mp4, webm.")
    output_id = uuid.uuid4().hex
    destination = STORAGE_DIR / f"{output_id}.{output_format}"
    if output_format == "mp3":
        args = ("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(destination))
    elif output_format == "mp4":
        args = ("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(destination))
    else:
        args = ("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "0", "-crf", "32", "-c:a", "libopus", str(destination))
    code, _, _ = await run_media_tool(*args, timeout=1800)
    if code != 0 or not destination.exists() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True); raise HTTPException(status_code=422, detail="Conversion failed for this input file.")
    return {"ok": True, "source_id": file_id, "id": output_id, "format": output_format, "bytes": destination.stat().st_size, "download_path": f"/media/{output_id}"}

@app.get("/media/{file_id}")
def get_media(file_id: str):
    source = resolve_media(file_id)
    return FileResponse(source, filename=source.name)
