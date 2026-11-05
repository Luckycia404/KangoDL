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

## Verification and current limitation

The provider successfully generated a 116-character GVS content-bound token for the user's public test video. However, the current VPS's YouTube MWEB player request returned `UNPLAYABLE` with a reload-required response, while Android player requests returned formats. A Web PO token must not be assumed interchangeable with Android/iOS tokens. For that reason, this service is kept separate and is not falsely wired into the Android extractor. Higher-resolution delivery remains unverified until a compatible MWEB/Web player response and full media download both succeed.

`bgutils-js` is MIT-licensed and maintained independently. YouTube may change the attestation flow at any time.
