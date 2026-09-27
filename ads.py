"""Ad / sponsor detection for analyzed media.

Two layers, because there are genuinely two kinds of "ad":

1. **Served ads** (YouTube pre-roll/mid-roll) are stitched in by the player and
   are *never* part of the downloaded stream, so they cannot be recovered from
   the media file. Nothing here can invent them.

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
               transcript: str | None = None) -> dict:
    """Detect ad/sponsor segments for one media source.

    YouTube links consult SponsorBlock first; anything else (uploads, or videos
    SponsorBlock hasn't tagged) falls back to the transcript heuristic.
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
