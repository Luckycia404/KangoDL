from __future__ import annotations

STRATEGIES = {
    "youtube": {
        "primary": "VISIONOS Innertube player API",
        "fallback": "Android and iOS Innertube player API clients",
        "fallback_kind": "alternate player-client requests",
    },
    "tiktok": {
        "primary": "public page hydration JSON",
        "fallback": "public embed player Frontity JSON, then public item-detail API",
        "fallback_kind": "two independent public resolver paths; embed player exposes imagePostInfo for photo slideshows",
    },
    "pornhub": {
        "primary": "watch-page player JSON/mediaDefinitions",
        "fallback": "separate public embed/player page",
        "fallback_kind": "alternate player page",
    },
    "pinterest": {
        "primary": "public pin page/player JSON",
        "fallback": "Pinterest API v5 GET /pins/{id}",
        "fallback_kind": "official OAuth API; requires PINTEREST_ACCESS_TOKEN and access to the Pin",
    },
    "audiomack": {
        "primary": "documented OAuth 1.0a catalog/playback API",
        "fallback": "public Next.js/RSC page-state parser for track metadata",
        "fallback_kind": "native page metadata fallback; public playback still requires the play endpoint",
    },
    "instagram": {
        "primary": "public page JSON/metadata",
        "fallback": "public media-info endpoint",
        "fallback_kind": "alternate public endpoint; may be rate-limited",
    },
    "reddit": {
        "primary": "Reddit OAuth public post JSON",
        "fallback": "Reddit public oEmbed post metadata",
        "fallback_kind": "metadata-only fallback; media extraction requires Reddit OAuth on hosted VPS IP ranges",
    },
    "x": {
        "primary": "X public syndication tweet-result JSON",
        "fallback": "X public status-page media metadata",
        "fallback_kind": "alternate public page parser; no login or private-content bypass",
    },
    "facebook": {
        "primary": "public video page Open Graph/player metadata",
        "fallback": "public structured player JSON",
        "fallback_kind": "alternate parser over publicly exposed player data; no login or private-content bypass",
    },
    "snapchat": {
        "primary": "public Spotlight Open Graph media metadata",
        "fallback": "embedded player/structured JSON URL scan",
        "fallback_kind": "alternate parser over public page data",
    },
}


def strategy_info(primary: str, fallback: str, selected: str, attempts: list[str] | None = None) -> dict:
    """Describe the actual resolver path used; never label a method successful unless it returned media data."""
    return {
        "primary": primary,
        "fallback": fallback,
        "selected": selected,
        "attempts": attempts or [selected],
        "fallback_used": selected != primary,
    }
