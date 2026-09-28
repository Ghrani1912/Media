"""Ad / sponsor detection for analyzed media.

Two layers, because there are genuinely two kinds of "ad":

1. **Served ads** are chosen at play time and are not part of what gets
   downloaded, so they cannot be recovered from the media file. Nothing here can
   invent them.

   Two extra layers cover served ads from this side: ``fetch_ad_breaks`` reads
   YouTube's published ad-break *schedule* (the yellow progress-bar layout), and
   ``detect_ads`` accepts a capture from whichever platform reader applies --
   ``served_ads.py`` for YouTube (a browser session, where the ad is an overlay)
   or ``twitch_ads.py`` for Twitch (the stream's own stitched-ad markers, since
   Twitch sews the ad into the media). This module never invents ads it cannot
   see.

2. **In-video sponsor/ad breaks** (a creator reading a sponsor script, a paid
   segment) *are* in the stream and can be detected. This module finds them two
   ways:

   * ``sponsorblock`` -- authoritative, community-tagged timestamps looked up by
     YouTube video id. Used whenever the source is a YouTube URL.
   * ``heuristic`` -- a transcript scan for sponsor-read language, used as a
     fallback for uploads or videos SponsorBlock has no data for.

The result is a single dict describing every detected segment.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

SPONSORBLOCK_URL = "https://sponsor.ajay.app/api/skipSegments"

WATCH_URL = "https://www.youtube.com/watch?v={video_id}&hl=en&gl=US"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# YouTube's adPlacement kinds -> the name viewers use for them.
_AD_KIND_NAMES = {
    "AD_PLACEMENT_KIND_START": "pre-roll",
    "AD_PLACEMENT_KIND_END": "post-roll",
    "AD_PLACEMENT_KIND_MILLISECONDS": "mid-roll",
}

# SponsorBlock categories we ask for. "sponsor" is the paid-promotion category;
# the rest are useful context (self-promo, subscribe reminders, intros...).
SPONSORBLOCK_CATEGORIES = [
    "sponsor",
    "selfpromo",
    "interaction",
    "intro",
    "outro",
    "preview",
    "music_offtopic",
    "filler",
]

# Categories that actually represent advertising/promotion. "sponsor-read" is
# the label the transcript heuristic attaches to its own findings.
AD_CATEGORIES = {"sponsor", "selfpromo", "sponsor-read"}

# Transcript language that strongly implies a sponsor/promo read.
_STRONG_PATTERNS = [
    r"sponsored by",
    r"sponsoring this video",
    r"sponsor of (this|today)",
    r"this (video|episode|segment|part) is sponsored",
    r"thanks to our sponsor",
    r"thanks to .{0,40} for sponsoring",
    r"brought to you by",
    r"paid (promotion|partnership|sponsorship)",
    r"promo code",
    r"discount code",
    r"coupon code",
    r"use (my |the )?code",
]

# Weaker signals: meaningful on their own only when they cluster.
_WEAK_PATTERNS = [
    r"link in the description",
    r"link in the bio",
    r"link below",
    r"check out the link",
    r"free trial",
    r"sign up (today|now|for)",
    r"\b\d+% off\b",
    r"first \d+ (people|customers|users)",
    r"before we (get|jump) (started|in)",
    r"head over to",
]

_STRONG_RE = re.compile("|".join(_STRONG_PATTERNS), re.IGNORECASE)
_WEAK_RE = re.compile("|".join(_WEAK_PATTERNS), re.IGNORECASE)

# Merge ad-like transcript segments no further apart than this.
_MERGE_GAP_SECONDS = 20.0
# Score needed to call a segment ad-like (strong counts double).
_SCORE_THRESHOLD = 2


def extract_video_id(url: str | None) -> str | None:
    """Return the YouTube video id from a URL, or None if it isn't YouTube."""
    if not url:
        return None
    patterns = [
        r"(?:youtube\.com/watch\?(?:.*&)?v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/(?:embed|shorts|live)/)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def fetch_sponsor_segments(video_id: str, timeout: float = 15.0) -> list[dict] | None:
    """Fetch SponsorBlock segments for ``video_id``.

    Returns a list of normalized segment dicts, ``[]`` when the video has no
    tagged segments (SponsorBlock answers 404), or ``None`` when the lookup
    itself failed (network error, rate limit) so callers can distinguish
    "verified: none" from "unknown".
    """
    if not video_id:
        return None

    query = urllib.parse.urlencode(
        {"videoID": video_id, "categories": json.dumps(SPONSORBLOCK_CATEGORIES)}
    )
    request = urllib.request.Request(
        f"{SPONSORBLOCK_URL}?{query}",
        headers={"User-Agent": "media-analyzer/1.0 (+https://example.invalid)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []  # no segments tagged for this video
        logger.warning("SponsorBlock lookup failed for %s: HTTP %s", video_id, exc.code)
        return None
    except Exception as exc:  # timeouts, DNS, JSON errors...
        logger.warning("SponsorBlock lookup failed for %s: %s", video_id, exc)
        return None

    segments = []
    for item in raw or []:
        try:
            start, end = item["segment"]
            category = item.get("category", "sponsor")
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        segments.append(
            {
                "category": category,
                "start": float(start),
                "end": float(end),
                "duration": float(end) - float(start),
                "source": "sponsorblock",
                "votes": item.get("votes"),
            }
        )
    return segments


def _raw_json_after(html: str, marker: str):
    """Return the JSON object that follows ``marker`` in ``html``, or None."""
    index = html.find(marker)
    if index < 0:
        return None
    start = html.find("{", index + len(marker))
    if start < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(html[start:])
    except ValueError:
        return None
    return obj


def fetch_ad_breaks(video_id: str, timeout: float = 20.0) -> dict | None:
    """Read the ad-break *schedule* YouTube publishes for a video.

    This is the structure behind the yellow segments on the progress bar: which
    breaks exist and where they sit (pre-roll / mid-roll / post-roll). It comes
    from the watch page's ``ytInitialPlayerResponse.adPlacements`` and does not
    need a browser.

    It says *where* ads may play, not *which* ads. The concrete spots are chosen
    at serve time and are only visible in a real playback session -- see
    ``served_ads.py`` for that.

    Returns None when the schedule could not be read at all (offline, blocked).
    """
    if not video_id:
        return None

    request = urllib.request.Request(
        WATCH_URL.format(video_id=video_id),
        headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", "replace")
    except Exception as exc:  # network, HTTP, decode...
        logger.warning("Ad-break schedule lookup failed for %s: %s", video_id, exc)
        return None

    player = _raw_json_after(html, "ytInitialPlayerResponse")
    if not isinstance(player, dict):
        return None

    breaks: list[dict] = []
    for placement in player.get("adPlacements") or []:
        renderer = (placement or {}).get("adPlacementRenderer") or {}
        config = (renderer.get("config") or {}).get("adPlacementConfig") or {}
        kind = config.get("kind") or "AD_PLACEMENT_KIND_START"
        offset = config.get("adTimeOffset") or {}
        try:
            start_ms = int(offset.get("offsetStartMilliseconds") or 0)
        except (TypeError, ValueError):
            start_ms = 0
        breaks.append(
            {
                "kind": kind,
                "placement": _AD_KIND_NAMES.get(kind, "ad break"),
                "start": round(start_ms / 1000.0, 1),
            }
        )

    # adBreakHeartbeatParams pairing with DAI tells us ads are stitched in
    # server-side, which still means "ad breaks exist for this video".
    dai = bool(player.get("playerConfig", {}).get("daiConfig"))
    monetized = bool(breaks) or dai

    if not monetized:
        note = "YouTube schedules no ad breaks for this video (not monetized)."
    elif breaks:
        note = (
            f"YouTube schedules {len(breaks)} ad break(s) for this video: "
            + ", ".join(
                b["placement"] if not b["start"] else f"{b['placement']} @ {b['start']:.0f}s"
                for b in breaks
            )
            + ". Which ads actually play depends on the viewer and session."
        )
    else:
        note = (
            "YouTube inserts ads for this video server-side, so exact break "
            "positions are not published. Capture a live session to see what "
            "actually plays."
        )

    return {
        "checked": True,
        "monetized": monetized,
        "breaks": breaks,
        "dai": dai,
        "note": note,
    }


def _score_text(text: str) -> int:
    return 2 * len(_STRONG_RE.findall(text)) + len(_WEAK_RE.findall(text))


def detect_ad_segments_from_transcript(segments) -> list[dict]:
    """Find ad-like spans in Whisper's timestamped transcript segments.

    Segments that read like a sponsor pitch are scored, merged when they are
    close together, and returned as candidate ad blocks.
    """
    if not segments:
        return []

    flagged = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        score = _score_text(text)
        if score >= _SCORE_THRESHOLD:
            flagged.append(
                {
                    "start": float(seg.get("start", 0.0)),
                    "end": float(seg.get("end", 0.0)),
                    "score": score,
                    "text": text,
                }
            )

    merged: list[dict] = []
    for block in flagged:
        if merged and block["start"] - merged[-1]["end"] <= _MERGE_GAP_SECONDS:
            merged[-1]["end"] = max(merged[-1]["end"], block["end"])
            merged[-1]["score"] += block["score"]
            merged[-1]["texts"].append(block["text"])
        else:
            merged.append({**block, "texts": [block["text"]]})

    results = []
    for block in merged:
        results.append(
            {
                "category": "sponsor-read",
                "start": block["start"],
                "end": block["end"],
                "duration": block["end"] - block["start"],
                "source": "heuristic",
                "votes": None,
                "excerpt": " ".join(block["texts"])[:200],
            }
        )
    return results


def detect_ads(link: str | None = None, transcript_segments=None,
               transcript: str | None = None, served_ads_report: dict | None = None,
               include_ad_breaks: bool = True) -> dict:
    """Detect ad/sponsor segments for one media source.

    YouTube links consult SponsorBlock first; anything else (uploads, or videos
    SponsorBlock hasn't tagged) falls back to the transcript heuristic.

    Two extra, independent layers ride along when available:

    * ``ad_breaks`` -- YouTube's own ad-break *schedule* (the yellow-bar layout).
    * ``served`` -- the ads actually served during a live viewing session, which
      the caller captures via ``served_ads.py`` and hands in here.
    """
    video_id = extract_video_id(link)
    verified: bool | None = None
    segments: list[dict] = []

    if video_id:
        sponsor_segments = fetch_sponsor_segments(video_id)
        if sponsor_segments is not None:
            verified = True
            segments = sponsor_segments
        else:
            verified = None  # lookup failed; unknown rather than "none"

    # Fall back to transcript analysis when we have no verified segments.
    if not segments and transcript_segments:
        segments = detect_ad_segments_from_transcript(transcript_segments)
        if verified is None:
            verified = False

    ad_segments = [s for s in segments if s["category"] in AD_CATEGORIES]
    by_category: dict[str, int] = {}
    for seg in segments:
        by_category[seg["category"]] = by_category.get(seg["category"], 0) + 1

    from_sponsorblock = bool(segments) and all(
        seg["source"] == "sponsorblock" for seg in segments
    )
    if from_sponsorblock:
        note = "Verified via SponsorBlock."
    elif segments:
        note = ("Detected from transcript wording (unverified; may include "
                "false positives).")
        if verified is True:
            note += " SponsorBlock has no segments for this video."
    elif verified is True:
        note = "SponsorBlock reports no sponsor/ad segments for this video."
    elif verified is None:
        note = ("Ad detection limited: SponsorBlock lookup failed and no ad "
                "language was found in the transcript.")
    else:
        note = "No ad segments detected."

    ad_breaks = fetch_ad_breaks(video_id) if (video_id and include_ad_breaks) else None

    served = served_ads_report or {}
    served_ads = served.get("ads") or []

    return {
        "video_id": video_id,
        "ad_count": len(ad_segments),
        "ad_seconds": round(sum(s["duration"] for s in ad_segments), 1),
        "segment_count": len(segments),
        "segment_seconds": round(sum(s["duration"] for s in segments), 1),
        "by_category": by_category,
        "segments": segments,
        "verified": verified,
        "source": segments[0]["source"] if segments else None,
        "note": note,
        "ad_breaks": ad_breaks,
        "served": served,
        "served_ads": served_ads,
        "served_ad_count": len(served_ads),
        "served_ad_seconds": round(sum(a.get("duration") or 0.0 for a in served_ads), 1),
    }


def format_segments(segments) -> str:
    """Compact human-readable summary of detected segments for the report."""
    if not segments:
        return ""
    parts = []
    for seg in segments:
        label = f"{seg['category']} {seg['start']:.0f}-{seg['end']:.0f}s"
        parts.append(label)
    return "; ".join(parts)
