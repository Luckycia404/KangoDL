# KangoDL Engine

KangoDL is an independently developed media extraction and processing API. Platform-specific extraction logic is written in KangoDL modules. yt-dlp is not installed or used by the runtime.

## API

- `GET /health` — public health check.
- `GET /info?url=...` — metadata and normalized public stream formats.
- `POST /jobs` — start a download job. JSON: `{"url":"https://youtu.be/VIDEO_ID","format_id":"18","mode":"video","output":"mp4"}`.
- `GET /jobs/{job_id}` — job status and progress.
- `GET /jobs/{job_id}/file` — retrieve a completed file.
- `POST /media`, `GET /media/{id}/probe`, `POST /media/{id}/convert?format=mp3|mp4|webm`, `GET /media/{id}` — local authorized-file processing.

Send the API key in `X-API-Key` or `Authorization: Bearer <key>`. Docker images require `KANGODL_API_KEY` to be set before protected routes become available. The health route remains public.

## Extractor architecture and current verification

- `app/extractors/youtube.py` implements KangoDL's YouTube URL parsing, public-page configuration reader, player API client, and format normalizer.
- Direct muxed MP4 download and HTTP file delivery were verified (360p, 18.5 MB, 10m51s). WebM video and audio output were verified using VP8/Opus transcoding (video output 23.6 MB, 10m51s).
- MP3 extraction and HTTP file delivery were verified for five exposed audio itags (139, 140, 249, 251, 600); each produced a valid 192 kbps MP3 (15.6 MB, 10m51s). YouTube blocked the standalone audio URLs, so KangoDL used the available muxed stream and extracted audio with FFmpeg.
- /info now labels each format as muxed, video_only, or audio_only, and exposes ready_for_direct_video_download so clients can distinguish a complete A/V stream from a video-only adaptive stream. The default video job prefers a muxed stream. If an explicitly selected adaptive video or audio stream is blocked, the job falls back to the best available muxed stream and reports requested_format separately from selected_format, rather than silently claiming the requested resolution succeeded. On the test video, the 1080p video stream returned HTTP 403 on a normal full-object request. A diagnostic byte-range test received an initial partial response, then HTTP 403 on subsequent ranges. This matches the documented GVS PO Token enforcement pattern; a PO Token may be required for the selected player client. The tested Android/iOS player clients are not interchangeable with Web PO Tokens, and the Web/Mobile-Web player profiles tested from this VPS did not return playable streams. KangoDL therefore falls back to the 360p muxed stream and reports requested_format=137 and selected_format=18. 720p/1080p delivery at the requested resolution is not yet supported or verified. Cookies are not automatically required for ordinary public videos; account-gated content may require authorized authentication.
- Formats requiring signature deciphering or other unavailable player data are not supported yet.
- This engine does not bypass DRM, private-video restrictions, paid access, or other access controls. Use it only for content the caller owns or is authorized to download.

## Local run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Docker / Northflank

Build with `docker build -t kangodl-engine .`. Set a strong `KANGODL_API_KEY`, configure `PORT` if needed, and mount persistent storage at `/app/storage`. The image uses Python 3.12, FFmpeg, a non-root user, and a health check. Store the API key as a secret, not in source control.

The current download worker is in-process and single-concurrency. Persisted job records survive restarts, but active downloads are marked interrupted instead of resumed. For multi-replica production, move jobs and media state to shared durable storage and a queue.
