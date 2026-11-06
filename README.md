# KangoDL Engine

KangoDL is an independently developed media extraction and processing API. YouTube, TikTok, Pornhub, Pinterest, Audiomack, Instagram, Snapchat, Facebook, X, and Reddit use KangoDL-owned extraction modules. No yt-dlp or gallery-dl runtime is used by these extractors.

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
- **Second-video regression test (2026-11-05):** `Fiwp4CQm2uk` (“Why Your Stickman Animation Videos Get NO Views! (Fix This)”, 440.32 seconds). VISIONOS exposed 4K60 AV1 (itag 401), 1440p60, 1080p60 H.264 (itag 299), 720p60, and audio-only streams. KangoDL completed a **4K MP4** (AV1 3840x2160 at 60 fps + AAC, 261,383,671 bytes) and a **1080p MP4** (H.264 1920x1080 at 60 fps + AAC, 149,734,812 bytes). It also completed an **MP3** (192 kbps, 10,567,917 bytes). FFprobe validated all three; the API returned HTTP 206 for 1 KiB byte-range reads from all three completed files. The 4K AV1 output is valid MP4, but device playback compatibility depends on AV1 decoder support.
- `/info` labels formats as muxed, video_only, or audio_only and includes the selected `player_client`. If a requested adaptive stream fails, KangoDL reports the actual selected fallback format rather than claiming the requested resolution succeeded. Cookies are not automatically required for ordinary public videos; account-gated content may require authorized authentication.
- The optional BgUtils sidecar remains separate. Web, Android, and iOS PO tokens are platform-specific and must not be interchanged. The main extractor currently prefers VISIONOS and does not attach a Web token to a different client.

### TikTok support

- `app/extractors/tiktok.py` resolves TikTok public post metadata from page hydration JSON, the public embed player's Frontity state, and the public item-detail endpoint. The embed fallback reads `imagePostInfo.displayImages` for public photo slideshows.
- TikTok video jobs download the public play stream directly. Audio jobs use the post's public music URL when available, otherwise they extract audio from the downloaded video using FFmpeg.
- GET /info?url=<TikTok URL> returns TikTok video metadata and available formats. POST /jobs accepts mode=video,output=mp4 or mode=audio,output=mp3 for video posts.
- TikTok photo-mode slideshows are parsed from public post metadata and downloaded image-by-image by KangoDL. Submit POST /jobs with `{"url":"https://www.tiktok.com/@user/photo/POST_ID","mode":"photos","output":"zip"}`. The completed job returns a ZIP containing each slide as a separate JPG/PNG/WebP image; job status includes `photo_count`. Photo jobs are limited to 35 images and 500 MiB.
- Photo mode was tested end-to-end against the supplied short link `https://vm.tiktok.com/ZN8fMFqxs/`: the embed-player fallback resolved two distinct slides, downloaded both original JPEGs (144,663 and 1,413,606 bytes), packaged them into a 1,558,491-byte ZIP, and returned the ZIP through `/jobs/{id}/file` with HTTP 200. FFprobe validated both JPEG streams (1228×2180 and 3876×2180). Duplicate CDN mirrors are not counted as extra slides.
- A public TikTok video post was verified with the native extractor: a 576×1024 MP4 (2,361,388 bytes) and MP3 audio (992,068 bytes) both downloaded successfully. Actual availability and maximum quality depend on the formats TikTok serves for each post.



### Pinterest support

- `app/extractors/pinterest.py` resolves `pin.it` short links and Pinterest pin URLs, then reads public pin metadata embedded in Pinterest's server-rendered page. It extracts original image URLs for image pins and public HLS playlists for video pins. No yt-dlp or gallery-dl is used.
- `GET /info?url=<Pinterest pin URL>` returns the pin title, description, thumbnail, duration, and available image/video formats.
- `GET /search/pinterest?query=<keywords>&limit=20&page=1` searches public Pinterest pins and returns pin IDs, titles, pin URLs, thumbnails, creator names, and image/video classification. It supports page-number pagination and a `cursor` parameter; pass `next_cursor` back as `cursor` to continue from the next result batch.
- Image pins: submit `POST /jobs` with `{"url":"https://pin.it/example","mode":"image","output":"jpg"}`. Video pins: use `mode=video,output=mp4` (or `webm`); audio extraction uses `mode=audio,output=mp3`.
- **Verified end-to-end against the two supplied Pinterest links (2026-11-06):** the image pin downloaded as a 159,799-byte JPEG and the video pin downloaded from its native HLS playlist as a valid 2,567,280-byte MP4. Both job file endpoints returned HTTP 200. The video was 1080×1920 and approximately 18 seconds.
- Pinterest image mode is for single-image pins; video pins expose their poster as a thumbnail, not as a separate image download. Private, removed, age-gated, geo-restricted, or otherwise unavailable pins are not bypassed.

### Pornhub support

- `app/extractors/pornhub.py` parses the public watch-page player `mediaDefinitions` array and discovers HLS quality levels. KangoDL downloads public HLS manifests and media segments with its own HTTP code (including signed-query inheritance, AES-128 key/map handling, byte-range segments, bounded retries, and alternate-quality fallback); FFmpeg only muxes the locally assembled playlist. A short-lived per-URL cache lets `/info` and `/jobs` reuse the same signed player URLs, with a forced metadata refresh after stream failures.
- `GET /info?url=<Pornhub video URL>` returns video metadata and available downloadable formats.
- `GET /search/pornhub?query=<keywords>&limit=20&page=1` searches public Pornhub video listings and returns titles, video URLs, thumbnails, and IDs. `GET /search?query=<keywords>` is an alias. Search results can be passed directly to `GET /info` and then `POST /jobs`.
- `POST /jobs` accepts `mode=video,output=mp4` (or `webm`) and `mode=audio,output=mp3`. Audio is extracted to MP3 with FFmpeg; video is downloaded/merged into the requested container.
- Pornhub photo/ZIP mode is not supported. Access to age-gated, private, removed, region-restricted, or otherwise unavailable posts is not guaranteed; this adapter does not bypass access controls.
- The Pornhub adapter rejects incomplete embed-only preview metadata rather than presenting a short preview as a full video. The source may intermittently return HTTP 403 or omit full watch-page player data for datacenter IPs; the adapter reports that limitation and does not bypass it. TikTok and Pornhub adapters contain no yt-dlp or gallery-dl imports.

### Instagram support

- `app/extractors/instagram.py` is KangoDL's own public-page Instagram resolver. It recognizes direct `/p/`, `/reel/`, `/reels/`, and `/tv/` post URLs and reads media URLs only when Instagram exposes them in public page metadata or embedded public JSON. It uses `curl_cffi` browser TLS impersonation because generic HTTP clients can receive a reduced logged-out shell. No yt-dlp, gallery-dl, Instagram password, or private-account bypass is used.
- `GET /info?url=<Instagram post or Reel URL>` returns the post title, description, media type, item count, and available formats.
- Video/Reel downloads: `POST /jobs` with `{"url":"https://www.instagram.com/reel/SHORTCODE/","mode":"video","output":"mp4"}` (or WebM). Audio extraction: `mode=audio,output=mp3`. A single photo uses `mode=image,output=jpg`. Multi-item carousels use `mode=photos,output=zip` and package each exposed image/video item separately.
- Stories, private posts, login-only pages, deleted content, and posts for which Instagram does not expose a public media URL are not supported. Instagram may rate-limit requests or change its page format; the extractor reports a clear unavailable/login-required error rather than fabricating a file.
- **Verified end-to-end (2026-11-06):** a public Reel was resolved from Instagram's embedded page JSON, its MP4 was downloaded through the KangoDL job API, and the completed file endpoint returned HTTP 200 with a valid H.264/AAC MP4 (1,971,734 bytes, 720×1280, 11.19 seconds). The resolver also reads direct MP4 URLs from public DASH manifests when available. Instagram can still login-gate, rate-limit, or omit media URLs for individual posts; private, deleted, or restricted posts are not bypassed.

### Reddit support

- `app/extractors/reddit.py` resolves public Reddit post links, short `redd.it` links, Reddit-hosted video/GIF posts, single images, and image galleries using Reddit's OAuth-backed public post JSON. It handles crossposts and gallery metadata; no third-party downloader API, yt-dlp, gallery-dl, login cookies, or private-subreddit access is used.
- `GET /info?url=<Reddit post URL>` returns public post metadata and supported media formats. `POST /jobs` supports video MP4/WebM, audio MP3 when Reddit exposes an HLS audio stream, single image JPG, and gallery ZIP.
- **OAuth setup is required on this VPS:** Reddit currently requires a valid OAuth token or logged-in access for Data API requests from hosted-service IP ranges. Set `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` in `/opt/kangodl-engine/.env` and restart the service. The public oEmbed endpoint is reachable without OAuth but provides embed metadata only, not a direct media stream; KangoDL does not attempt to evade Reddit's access controls.
- Reddit text-only/external-link posts, private or quarantined communities unavailable to the app, removed posts, and media without public direct/HLS URLs are not downloadable. Full live extraction/download validation is pending OAuth credentials; the VPS currently returns HTTP 403 for unauthenticated post JSON.

### X (Twitter) support

- `app/extractors/x.py` is KangoDL's native X status resolver. It parses public `x.com` and legacy `twitter.com` status URLs, reads public post metadata and MP4 variants from X's syndication JSON endpoint, and falls back to publicly exposed media metadata in the status page. It does not use yt-dlp, gallery-dl, third-party downloader APIs, account cookies, or private-content bypasses.
- `GET /info?url=<X status URL>` returns the post text, author, thumbnail, duration when exposed, and MP4 quality choices. `POST /jobs` supports MP4/WebM video and MP3 audio extraction. The default is the highest-bitrate public MP4 variant returned by X.
- Only publicly accessible posts with a direct MP4 variant are supported. Photos-only posts, protected/deleted posts, login-only or age-restricted posts, and posts for which X does not expose a public MP4 URL are not bypassed. GIF-style X posts are treated as MP4 video where X exposes an MP4 variant. If a higher-bitrate CDN variant cannot be fully retrieved, the downloader tries the next lower public MP4 variant rather than returning a partial file.
- **Verified end-to-end (2026-11-06):** public NASA post `https://x.com/NASA/status/2110094874024219133` resolved through X's syndication JSON and exposed four MP4 variants (up to 1080p) plus audio extraction. The API completed MP4 and MP3 jobs; both file endpoints returned HTTP 200. The verified MP4 was 156,343 bytes, H.264/AAC, 480×270, 8.53 seconds (the higher-bitrate variants timed out on this VPS and the automatic quality fallback selected 480p). The verified MP3 was 206,346 bytes, 8.57 seconds, and passed FFprobe.

### Facebook support

- `app/extractors/facebook.py` resolves public Facebook video, Reel, Watch, and `fb.watch` share pages from Open Graph metadata and publicly embedded player JSON. It only accepts direct media URLs exposed by Facebook's public page/player response; it does not use yt-dlp, gallery-dl, login cookies, account credentials, or private-content bypasses.
- `GET /info?url=<Facebook video/Reel URL>` returns the title, description, thumbnail, and MP4/MP3 format choices. `POST /jobs` supports video MP4/WebM and audio MP3.
- Private profiles/groups, login-only videos, removed posts, and pages that do not expose a public direct MP4 URL are not supported. Facebook may return a login shell, rate-limit, or change its player markup; KangoDL reports unavailable media rather than attempting to bypass access controls.
- **Verified end-to-end (2026-11-06):** a public Facebook Reel resolved from structured player JSON; the job API downloaded a 5,916,279-byte H.264/AAC MP4 (720×1280, 60.949 seconds), and a separate MP3 job produced a valid 1,459,732-byte MP3 (60.792 seconds). Both completed file endpoints returned HTTP 200 and passed FFprobe validation.

### Snapchat support

- `app/extractors/snapchat.py` resolves public Snapchat Spotlight share pages and extracts the original CDN video URL from public page metadata. It uses KangoDL-owned URL parsing and metadata extraction with browser TLS impersonation; it does not use yt-dlp, gallery-dl, account credentials, or private-content bypasses.
- `GET /info?url=<Snapchat Spotlight URL>` returns the public title, description, thumbnail, and MP4/MP3 format choices. `POST /jobs` supports video MP4/WebM and audio MP3.
- Only public Spotlight share URLs are supported in this initial integration. Private stories, chat attachments, login-only content, and ephemeral/private content are not supported.
- **Verified end-to-end (2026-11-06):** a live public Spotlight page resolved to its Snapchat CDN MP4, downloaded successfully, and passed FFprobe validation as H.264/AAC MP4 (531,780 bytes, 540×960, 4.155 seconds). Individual posts may still be unavailable, expired, rate-limited, or omit a public media URL.

### Audiomack support

- `app/extractors/audiomack.py` implements its own OAuth 1.0a request signing and calls Audiomack's public catalog/playback API directly. It does not use yt-dlp, gallery-dl, or another extractor package.
- `GET /search/audiomack?query=<keywords>&limit=20&page=1&type=songs` searches tracks and returns titles, artists, artwork, duration, and canonical Audiomack song URLs. `type` can be `songs`, `albums`, `artists`, or `music`.
- `GET /info?url=<Audiomack song URL>` returns public track metadata and the available audio format.
- `POST /jobs` accepts `{"url":"https://audiomack.com/artist/song/track-slug","mode":"audio","output":"mp3"}`. The job uses Audiomack's documented play endpoint to request a short-lived public stream URL, downloads that stream, and converts it to MP3.
- The extractor refuses private, stream-only, Premium-only, geographically restricted, or otherwise unavailable tracks; it does not bypass account, subscription, or regional controls. Audiomack API client credentials can be overridden with `AUDIOMACK_CONSUMER_KEY` and `AUDIOMACK_CONSUMER_SECRET` environment variables.
- **Verified (2026-11-06):** native OAuth-signed song search returned real Wizkid/Asake catalog results; song metadata resolved by artist/track slug; Audiomack's play endpoint returned a short-lived public M4A stream; a 1,949,074-byte stream was downloaded and converted by FFmpeg into a valid 3,842,023-byte MP3.

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

On the working VPS, both API processes are managed by enabled systemd services (`kangodl-engine.service` and `kangodl-pot.service`) and bind to loopback only. Their unit files are included in `deploy/systemd/`; the PO token service reads its private `.secret` file at startup. The public API has not been exposed through a reverse proxy or domain yet.


## Optional Web PO Token sidecar

A standalone Node.js service is available in `token-provider/`. It uses `bgutils-js` to mint video-bound YouTube Web GVS PO tokens and is intentionally separate from the Python YouTube extractor.

The provider is an optional Web-client research component and is not required by the current extractor. KangoDL now uses the VISIONOS player flow for verified high-resolution delivery; Web tokens must not be treated as Android, iOS, or VISIONOS tokens. See `token-provider/README.md` for setup and deployment.

## Optional YouTube cookies

The independent extractor supports an optional Netscape/Mozilla cookie jar. Set `KANGODL_YOUTUBE_COOKIE_FILE` to the absolute path of a locally stored cookie export before starting the API process. For systemd, add the variable through a protected environment file or a service override; keep the cookie file outside the Git repository, owned by the service account, and readable only by that account (for example, mode `0600`). Restart the API service after changing the environment. If the variable is unset, KangoDL continues using its normal unauthenticated flow. Expired cookies are ignored. Cookie files are credentials: never commit, publish, or expose them through an API route. Authentication does not guarantee YouTube will permit playback or extraction.

## Dual extraction and automatic fallback

Every supported platform is routed through a platform-specific resolver with explicit primary/fallback strategy reporting. The engine records the selected method in `/info` as `extraction_strategy` and in completed download jobs as `extraction_method`. A fallback is attempted only when the primary resolver cannot produce usable media metadata; a completed job is still required to verify a real file.

- **YouTube:** VISIONOS Innertube player is primary; Android and iOS Innertube player clients are fallback API strategies. These are independent player-client requests, not the YouTube Data API (which does not provide downloadable media streams).
- **TikTok:** public page JSON is primary; the public embed player's Frontity state is a fallback for video and photo posts, with the public item-detail endpoint as an additional alternate. The embed state exposes `imagePostInfo.displayImages` for photo slideshows. Each slide's CDN mirror URLs are treated as alternatives, not separate images, and the downloader validates JPEG/PNG/WebP signatures before adding slides to the ZIP. The supplied photo post was retrieved as a two-image ZIP and each image passed FFprobe. TikTok's official Display API requires an authorized user and is not a general-purpose direct media-download API for arbitrary posts.
- **Pornhub:** watch-page player JSON is primary; a separate public embed/player page is the fallback. Only complete title+duration metadata is cached or advertised for full downloads. Native HLS segment fetching avoids FFmpeg's intermittent CDN TLS pull failures; the engine tries alternate qualities and refreshes signed URLs once. Pornhub can still return HTTP 403 or expose only an incomplete embed preview to datacenter IPs; those cases fail closed instead of claiming a full download.
- **Pinterest:** public pin page/player JSON is primary; the official Pinterest API v5 pin lookup is an optional fallback when `PINTEREST_ACCESS_TOKEN` is configured and the token owner is authorized to access that Pin. Pinterest API access requires OAuth and is not an anonymous public download API. No token is currently configured, so only the page resolver was tested.
- **Audiomack:** documented OAuth 1.0a catalog/playback API is primary; public Next.js/RSC page-state parsing is a metadata fallback. The fallback was forced in a test and still produced a valid MP3 by using Audiomack's documented play endpoint to mint the public short-lived stream URL. If that playback endpoint is unavailable, the page metadata alone cannot supply a stream.
- **Instagram:** public page JSON is primary; Instagram's public media-info endpoint is attempted as a fallback when the page omits media. The fallback returned HTTP 429 in the test environment. Meta oEmbed is intentionally not treated as a downloader because it is an embed/metadata interface, not a direct media-stream API.
- **X:** X public syndication tweet-result JSON is primary; the public status-page parser is the fallback. Only direct `video.twimg.com` MP4 variants returned in public post data are accepted.
- **Snapchat:** public Spotlight OG media metadata is primary; embedded player/structured JSON is the secondary resolver. Both methods were tested against the supplied Spotlight: the JSON fallback was forced, downloaded the MP4, and passed FFprobe. Snapchat's public developer Spotlight API is for publishing/managing a profile's Spotlights, not retrieving arbitrary public Spotlight media.

The YouTube Android fallback was also forced independently and returned 14 direct formats; the normal VISIONOS path remains primary. The YouTube Data API does not provide downloadable media streams. The Audiomack RSC fallback and Snapchat JSON fallback both completed actual file downloads and passed FFprobe. Pornhub's separate embed parser returned public HLS formats. TikTok's photo-post fallback and Instagram's media-info fallback did not pass in this run; Pinterest's OAuth fallback could not be exercised because no token is configured. No platform is marked as dual-path-successful merely because both method names exist. Optional API credentials are not fabricated or embedded in the repository.

## Secure platform authentication settings

Authentication settings are managed through the API and are protected by the configured `KANGODL_API_KEY` (send it as `X-API-Key` or `Authorization: Bearer ...`). The settings API refuses changes when no API key is configured.

- `GET /auth/status` reports which supported credential profiles are configured without returning secret values.
- `PUT /auth/credentials/reddit` accepts `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, and optionally `REDDIT_USER_AGENT`.
- `PUT /auth/credentials/pinterest` accepts `PINTEREST_ACCESS_TOKEN`.
- `PUT /auth/credentials/audiomack` accepts `AUDIOMACK_CONSUMER_KEY` and `AUDIOMACK_CONSUMER_SECRET`.
- `POST /auth/youtube/cookies` accepts a multipart `file` containing a valid Netscape/Mozilla cookie export (maximum 1 MiB). The file is stored under `storage/auth/` with owner-only permissions and is never returned by the API.
- `DELETE /auth/youtube/cookies` removes the stored YouTube cookie file.

Restart `kangodl-engine.service` after changing credentials or uploading/removing YouTube cookies so import-time extractor settings reload. Cookie support is currently wired only to the YouTube extractor. Other platforms use their documented OAuth/API credential profiles where implemented; the API does not pretend that a generic cookie file works across every platform. Instagram, Facebook, TikTok, X, Snapchat, and Pornhub remain public-extraction-only in this build. Authentication does not unlock private content or bypass platform access restrictions.
