from __future__ import annotations
import asyncio, os, re, uuid, threading, subprocess, json, time, hmac
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request as URLRequest, urlopen
from fastapi import FastAPI, File, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from app.extractors.youtube import ExtractorError, extract_youtube_info, video_id_from_url

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
app = FastAPI(title="KangoDL Engine", description="Independent media extraction and processing engine.", version="0.7.0")
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

OUTPUT_FORMATS = {"mp3": ".mp3", "mp4": ".mp4", "webm": ".webm"}

class DownloadRequest(BaseModel):
    url: str
    format_id: str | None = None
    mode: str = Field(default="video", pattern="^(video|audio)$")
    output: str = Field(default="mp4", pattern="^(mp4|mp3|webm)$")

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

def _download_url(url: str, destination: Path, job_id: str, label: str):
    user_agents = [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "com.google.android.youtube/20.10.38 (Linux; U; Android 15) gzip",
        "com.google.ios.youtube/20.10.4",
    ]
    last_error = None
    for user_agent in user_agents:
        total = 0
        destination.unlink(missing_ok=True)
        request = URLRequest(url, headers={"User-Agent": user_agent, "Origin": "https://www.youtube.com", "Referer": "https://www.youtube.com/"})
        try:
            with urlopen(request, timeout=35) as response, destination.open("wb") as out:
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
    raise RuntimeError(f"All direct stream request profiles failed ({type(last_error).__name__}: {last_error}).")

def _run_download_job(job_id: str, request_data: dict):
    work_id = job_id
    video_path = STORAGE_DIR / f"{work_id}.video.part"
    audio_path = STORAGE_DIR / f"{work_id}.audio.part"
    final_path = STORAGE_DIR / f"{work_id}{OUTPUT_FORMATS[request_data['output']]}"
    try:
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
                _download_url(selected["_direct_url"], audio_path, job_id, "Downloading audio")
            except Exception:
                # Some public player audio URLs reject full-object reads. Fall back
                # to a muxed stream when one is available, then extract its audio.
                audio_path.unlink(missing_ok=True)
                muxed = [f for f in formats if f.get("has_audio") and f.get("has_video") and f.get("_direct_url")]
                if not muxed:
                    raise
                fallback = max(muxed, key=lambda f: (f.get("height") or 0, f.get("bitrate") or 0))
                _job_update(job_id, stage="Audio stream unavailable; downloading combined stream", selected_format=fallback["format_id"], progress=2)
                _download_url(fallback["_direct_url"], video_path, job_id, "Downloading combined stream")
                audio_source = video_path
        else:
            _download_url(selected["_direct_url"], video_path, job_id, "Downloading video")
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
            if request_data["output"] != "mp4":
                raise RuntimeError("Adaptive video-only streams currently support MP4 output only; choose a combined stream for WebM.")
            audio_candidates = [f for f in formats if f.get("has_audio") and not f.get("has_video") and f.get("_direct_url")]
            if not audio_candidates:
                raise RuntimeError("Video stream was downloaded, but YouTube did not provide a direct audio stream for merging.")
            audio = max(audio_candidates, key=lambda f: f.get("bitrate") or 0)
            _job_update(job_id, stage="Downloading audio stream for merge", progress=55)
            try:
                _download_url(audio["_direct_url"], audio_path, job_id, "Downloading audio stream for merge")
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
                _download_url(fallback["_direct_url"], video_path, job_id, "Downloading combined fallback")
                _job_update(job_id, status="processing", stage="Finalising combined MP4", progress=95)
                subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path), "-c", "copy", "-movflags", "+faststart", str(final_path)], check=True, timeout=900)
            else:
                _job_update(job_id, status="processing", stage="Merging video and audio", progress=92)
                subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path), "-i", str(audio_path), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(final_path)], check=True, timeout=1800)
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

@app.get("/health")
def health():
    return {"ok": True, "service": "KangoDL Engine", "version": app.version, "extractors": ["kangodl-youtube"], "download_workers": 1}

@app.get("/info")
def info(url: str):
    try:
        data = extract_youtube_info(url)
        data.pop("_internal_formats", None)
        return data
    except ExtractorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

@app.post("/jobs", status_code=202)
def create_download_job(payload: DownloadRequest):
    try:
        video_id_from_url(payload.url)
    except ExtractorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
