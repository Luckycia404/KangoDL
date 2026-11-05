# KangoDL PO Token Provider

A separate Node.js service that creates video-bound Web PO tokens using `bgutils-js`. It is not yt-dlp and does not install or import yt-dlp.

## Local run

1. Create a private `.secret` file containing a long random value (one line) and restrict it to owner read/write.
2. Run `npm ci --omit=dev`.
3. Run `./start.sh`.

The service listens on `127.0.0.1:8878` by default. `GET /health` is a health check. `POST /token` accepts `{"videoId":"11_character_id"}` and requires `Authorization: Bearer <secret-from-.secret>`.

Tokens are content-bound to the supplied video ID. The service caches its integrity-token minter in memory and refreshes it before estimated expiry.

## Deployment

Build this directory as its own container. Set `KANGODL_POT_SECRET` as a runtime secret. Do not expose port 8878 publicly; keep it on a private container network. The default host is loopback for same-machine use; set `POT_HOST=0.0.0.0` only when a private container network requires it.

## Verification and current role

The provider successfully generated a video-bound Web GVS token for the public test video. The main KangoDL extractor now prefers the VISIONOS player flow, which has been verified for full 1080p and 720p MP4 delivery on that test asset. This sidecar remains an optional, separate component for future Web-client experiments; its Web token must not be attached to Android, iOS, or VISIONOS stream URLs. It is not required by the current main API path.

`bgutils-js` is MIT-licensed and maintained independently. YouTube may change the attestation flow at any time.
