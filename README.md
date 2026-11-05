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

- `app/extractors/youtube.py` implements KangoDL's YouTube URL parsing, VISIONOS visitor-data bootstrap, player API request, format normalizer, and Android/iOS fallback clients. The primary client follows the current NewPipe Extractor approach: VISIONOS client ID 101, version 1.04, and a visitor ID minted for the same client context. NewPipe documents this client strategy and its current PO-token limitations in its source/Javadocs.
- KangoDL downloads supported Google Video Server streams with verified HTTP byte ranges. It probes the object size, requests 4 MiB ranges using four workers, validates every `Content-Range`, writes each range at its exact offset, and checks the final byte count. If the origin does not support byte ranges, it falls back to a normal streamed response.
- **1080p MP4 verified end to end** on the test video: format 137, H.264 1080x1920 plus AAC, 651.32 seconds, 74,928,322 bytes; the API job completed and `/jobs/{id}/file` returned HTTP 200 with the full file. **720p MP4 verified**: format 136, H.264 720x1280 plus AAC, 651.32 seconds, 48,286,911 bytes. Adaptive MP4 merging stream-copies compatible H.264/AAC tracks instead of needlessly re-encoding them.
- **MP3 verified** through the VISIONOS audio stream (AAC itag 140): 192 kbps, 15,633,482 bytes, 651-second duration. **WebM verified end to end** using the native VP9 video itag 243 plus Opus audio: 360p, 651.281 seconds, 18,295,746 bytes; the API file endpoint returned HTTP 200. Native WebM streams are stream-copied when both source tracks are WebM/Opus; other combinations are transcoded to VP8/Opus.
- `/info` labels formats as muxed, video_only, or audio_only and includes the selected `player_client`. If a requested adaptive stream fails, KangoDL reports the actual selected fallback format rather than claiming the requested resolution succeeded. Cookies are not automatically required for ordinary public videos; account-gated content may require authorized authentication.
- The optional BgUtils sidecar remains separate. Web, Android, and iOS PO tokens are platform-specific and must not be interchanged. The main extractor currently prefers VISIONOS and does not attach a Web token to a different client.

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


## Optional Web PO Token sidecar

A standalone Node.js service is available in `token-provider/`. It uses `bgutils-js` to mint video-bound Web GVS PO tokens and is intentionally separate from the Python extractor. It is not yt-dlp and does not add yt-dlp to the engine.

The provider is an optional Web-client research component and is not required by the current extractor. KangoDL now uses the VISIONOS player flow for verified high-resolution delivery; Web tokens must not be treated as Android, iOS, or VISIONOS tokens. See `token-provider/README.md` for setup and deployment.
