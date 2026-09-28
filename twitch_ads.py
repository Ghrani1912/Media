"""Capture the *served* ads Twitch stitches into a live stream.

Twitch does not overlay ads on top of the player the way YouTube does; it
**stitches them into the HLS stream itself** ("server-side ad insertion", SSAI).
That changes the whole approach used in ``served_ads.py``:

* There is no overlay to read and no client-side "Skip Ad" button, so there is
  nothing for a driven browser to observe. Scrubbing the page is a dead end too:
  the Twitch player fetches its media from a worker, so the page's own
  network log and even the page's CDP ``Network`` domain never see a playlist
  request (verified -- see the notes in ``tests/test_twitch_ads.py``).
* What *is* authoritative is the stream's own ad timeline. The media playlist
  carries ``#EXT-X-DATERANGE`` tags with ``CLASS="twitch-stitched-ad"``, one per
  spot in a break, naming its length, its position in the pod, the roll type
  (``PREROLL``/``MIDROLL``) and the creative that was stitched in.
* Because the ad *is* media, its frames can be pulled straight out of the
  stream. Each ad's segments are listed in the playlist, so this module
  downloads the segment covering the ad's start, middle and end -- plus the
  content segments either side of it -- and decodes one frame from each with
  ffmpeg. That is stronger evidence than a screenshot: it is the ad's own
  picture, at a known point in its own playback.

So the capture here is a playlist reader, not a browser session. It opens the
same playback session a viewer would (Twitch's GraphQL playback token, then the
usher master playlist), then polls the media playlist for as long as it is told
to, watching for ad markers. Ad breaks are scheduled on the broadcast, so every
viewer sees the same mid-roll at the same moment; the pre-roll belongs to our own
session.

Limits, stated plainly
----------------------
* Markers only exist while a break is in the playlist window, so this is a live
  watch: ``watch_seconds`` says how long to wait for a mid-roll.
* Twitch caps and personalises ad frequency, so a repeat run can legitimately
  see no pre-roll at all.
* Twitch VOD playlists carry no ad markers (checked against a real archive), so
  a ``/videos/<id>`` link can only be told that served ads are not readable from
  it -- use the live channel URL.
* The marker names no brand, so ``advertiser`` is reported as the host of the
  ad's click-through URL, not a name.
* A fresh, logged-out session is usually given Twitch's own "commercial break in
  progress" slate instead of a paid spot; those breaks are flagged as house ads
  rather than dressed up as an advertiser.
* Positions come from the playlist's own clock and segment boundaries (~2s), and
  "aired at" is the stream edge, a couple of seconds ahead of a viewer's screen.

Requires: nothing beyond the standard library; ``ffmpeg`` for the proof frames.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

# proof index / manifest / timeline writers are shared with the YouTube path
import served_ads
from served_ads import CREATIVES_PAGE

logger = logging.getLogger(__name__)

# Public web client id (the one yt-dlp uses); override to use your own.
CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "ue6666qo983tsx6so1t0vnawi233wa")
GQL_URL = "https://gql.twitch.tv/gql"
USHER_BASE = "https://usher.ttvnw.net"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# The class the playlist uses for an ad stitched into the stream.
STITCHED_AD_CLASS = "twitch-stitched-ad"

# How often to re-read the playlist. Twitch segments are ~2s, so this is the
# resolution of both detection and the "how late did we notice" figure.
POLL_SECONDS = float(os.environ.get("SERVED_ADS_TWITCH_POLL", "2"))
# How long to watch a live channel for a break, when no budget is given.
WATCH_SECONDS = float(os.environ.get("SERVED_ADS_TWITCH_WATCH", "300"))
REQUEST_TIMEOUT = float(os.environ.get("SERVED_ADS_TWITCH_TIMEOUT", "20"))
# How many frames to decode per ad (0 disables segment proof entirely).
FRAME_LIMIT = int(os.environ.get("SERVED_ADS_TWITCH_FRAMES", "5"))
# Preferred rendition height for those frames: Twitch's smallest variant is a
# 144p thumbnail-sized picture, which makes for poor proof, while higher ones
# cost bandwidth for no extra evidence.
FRAME_HEIGHT = int(os.environ.get("SERVED_ADS_TWITCH_HEIGHT", "360"))
# After the ad is over, how many polls to keep trying for the "content after" frame.
AFTER_POLLS = int(os.environ.get("SERVED_ADS_TWITCH_AFTER_POLLS", "3"))
# Twitch rotates a small pool of creatives, and its markers name no brand, so the
# same ad is recognised on later runs by hashing its picture. How many bits of a
# 64-bit dHash may differ and still count as the same creative: the animated
# background on Twitch's own break slate alone moves a hash by around 9 bits
# between two frames of the *same* ad, so a tight threshold would never match it.
FINGERPRINT_TOLERANCE = int(os.environ.get("SERVED_ADS_TWITCH_FINGERPRINT_TOLERANCE", "10"))
# Sightings keep their own hashes (an animated creative drifts), so a later run
# matches the nearest one rather than a single remembered picture.
FINGERPRINTS_KEPT = int(os.environ.get("SERVED_ADS_TWITCH_FINGERPRINTS_KEPT", "8"))
# The rolling gallery of creatives seen so far, kept beside the proof frames.
REGISTRY_FILE = "twitch_ad_creatives.json"

# How each proof frame is pinned to the stream: seconds relative to the ad's
# start (or end) that the frame should be decoded from.
FRAME_OFFSETS = {
    "before": -1.0,
    "start": 0.5,
    "mid": None,  # filled in from the ad's own duration
    "end": -0.5,  # from the ad's end
    "after": 1.0,  # after the ad's end
}

TWITCH_FORMAT = "ssai stitched in-stream"

# Paths on twitch.tv that are not channel names.
_RESERVED_LOGINS = {
    "videos", "directory", "downloads", "settings", "search", "p", "inventory",
    "subscriptions", "wallet", "drops", "jobs", "turbo", "store", "friends",
    "messages", "notifications", "following", "u", "team", "teams", "legal",
    "products", "creatorcamp", "broadcast", "embed", "channel", "clips", "setup",
}

_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{2,25}$")
_VOD_RE = re.compile(r"^(\d{3,12})$")
# Click-through hosts that are tracking infrastructure rather than the advertiser.
_TRACKER_HOSTS = ("doubleclick", "click", "beacon", "amazon-adsystem", "adservice", "adsystem")


# --------------------------------------------------------------------------- #
# URLs and playback session
# --------------------------------------------------------------------------- #
class TwitchError(RuntimeError):
    """Raised when a Twitch playback session cannot be opened."""


def twitch_target(url: str) -> tuple[str, str] | None:
    """Return ``("live", login)`` or ``("vod", id)`` for a Twitch URL, else None."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not (host == "twitch.tv" or host.endswith(".twitch.tv")):
        return None
    query = urllib.parse.parse_qs(parts.query)
    if "channel" in query and query["channel"]:
        login = query["channel"][0].lower()
        return ("live", login) if _LOGIN_RE.match(login) else None

    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return None
    if segments[0] == "videos" and len(segments) > 1:
        return ("vod", segments[1]) if _VOD_RE.match(segments[1]) else None
    if segments[0] == "v" and len(segments) > 1:
        return ("vod", segments[1]) if _VOD_RE.match(segments[1]) else None
    if segments[0] in {"channel", "embed"} and len(segments) > 1:
        segments = segments[1:]
    login = segments[0].lower()
    if login in _RESERVED_LOGINS or not _LOGIN_RE.match(login):
        return None
    return ("live", login)


def _gql(query: str) -> dict:
    request = urllib.request.Request(
        GQL_URL,
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={
            "Client-Id": CLIENT_ID,
            "Content-Type": "text/plain;charset=UTF-8",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.load(response)


def playback_access_token(kind: str, ident: str) -> tuple[str, str]:
    """Ask Twitch for the playback token/signature for a channel or VOD."""
    params = 'params:{platform:"web",playerBackend:"mediaplayer",playerType:"site"}'
    if kind == "vod":
        query = "{videoPlaybackAccessToken(id:\"%s\",%s){value signature}}" % (ident, params)
        key = "videoPlaybackAccessToken"
    else:
        query = "{streamPlaybackAccessToken(channelName:\"%s\",%s){value signature}}" % (
            ident, params,
        )
        key = "streamPlaybackAccessToken"
    data = _gql(query)
    errors = data.get("errors") or []
    if errors:
        raise TwitchError(errors[0].get("message") or "Twitch refused the token request")
    node = (data.get("data") or {}).get(key)
    if not node:
        raise TwitchError("Twitch returned no playback token (is the channel live?)")
    return node["value"], node["signature"]


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def _usher_url(kind: str, ident: str, query: dict) -> str:
    # A channel's master playlist lives under /api/channel/hls; a VOD's does not.
    path = f"api/channel/hls/{ident}" if kind == "live" else f"vod/{ident}"
    return f"{USHER_BASE}/{path}.m3u8?" + urllib.parse.urlencode(query)


def media_playlist_url(kind: str, ident: str, quality: str | None = None) -> str:
    """Open a playback session and return the media playlist to poll.

    A modest video rendition is preferred on purpose: the only thing taken from
    the stream is a few proof frames, so there is no reason to pull 1080p60 down.
    """
    token, signature = playback_access_token(kind, ident)
    query = {
        "allow_source": "true",
        "allow_audio_only": "true",
        "p": uuid.uuid4().int % 9_000_000 + 1_000_000,
        "platform": "web",
        "player": "twitchweb",
        "supported_codecs": "av1,h265,h264",
        "playlist_include_framerate": "true",
        "sig": signature,
        "token": token,
    }
    master = _fetch_text(_usher_url(kind, ident, query))
    variants = _parse_master(master)
    if not variants:
        raise TwitchError("Twitch's master playlist listed no stream variants")
    return _pick_variant(variants, quality)


def _parse_master(text: str) -> list[dict]:
    """Parse a usher master playlist into ``{name, url, height}`` entries."""
    variants: list[dict] = []
    pending: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _attributes(line.split(":", 1)[1])
            resolution = attrs.get("RESOLUTION") or ""
            height = 0
            if "x" in resolution:
                try:
                    height = int(resolution.split("x")[-1])
                except ValueError:
                    height = 0
            pending = {
                "name": attrs.get("VIDEO") or attrs.get("NAME") or "",
                "height": height,
            }
        elif line and not line.startswith("#") and pending is not None:
            pending["url"] = line
            variants.append(pending)
            pending = None
    return variants


def _pick_variant(variants: list[dict], quality: str | None = None,
                  height: int | None = None) -> str:
    """Choose the cheapest rendition whose picture is still legible (``FRAME_HEIGHT``)."""
    if quality:
        for variant in variants:
            if quality.lower() in (variant.get("name") or "").lower():
                return variant["url"]
    video = [v for v in variants if v.get("height")]
    if not video:
        return variants[0]["url"]
    target = FRAME_HEIGHT if height is None else height
    big_enough = [v for v in video if v["height"] >= target]
    if big_enough:
        return min(big_enough, key=lambda v: v["height"])["url"]
    return max(video, key=lambda v: v["height"])["url"]


# --------------------------------------------------------------------------- #
# Playlist parsing
# --------------------------------------------------------------------------- #
# Attributes are quoted strings (ID="...") or bare values (DURATION=30.168).
_ATTR_RE = re.compile(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]*)')


def _attributes(text: str) -> dict:
    attrs: dict[str, str] = {}
    for key, value in _ATTR_RE.findall(text or ""):
        attrs[key] = value[1:-1] if value.startswith('"') and value.endswith('"') else value
    return attrs


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_date(value: str | None) -> float | None:
    """``2026-09-28T10:36:58.631Z`` -> epoch seconds."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def parse_playlist(text: str) -> dict:
    """Read a media playlist into its ad windows and its segments.

    Segments carry the wall-clock moment they air (from ``#EXT-X-PROGRAM-DATE-TIME``)
    and their offset from the start of the broadcast/VOD (``#EXT-X-TWITCH-ELAPSED-SECS``
    plus the running ``#EXTINF`` total), which is what lets an ad marker be
    reported as a position a viewer could seek to.
    """
    ads: list[dict] = []
    segments: list[dict] = []
    elapsed = None
    total = None
    server_time = None
    endlist = False
    pending_duration = None
    pending_title = None
    pending_start = None
    last_start = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-DATERANGE:"):
            attrs = _attributes(line.split(":", 1)[1])
            start = _parse_date(attrs.get("START-DATE"))
            if attrs.get("CLASS") == STITCHED_AD_CLASS:
                ads.append(_ad_window(attrs, start))
            elif attrs.get("X-SERVER-TIME"):
                server_time = _as_float(attrs.get("X-SERVER-TIME"))
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_start = _parse_date(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            body = line.split(":", 1)[1]
            duration, _, title = body.partition(",")
            pending_duration = _as_float(duration)
            pending_title = title.strip() or None
        elif line.startswith("#EXT-X-TWITCH-ELAPSED-SECS:"):
            elapsed = _as_float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TWITCH-TOTAL-SECS:"):
            total = _as_float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-ENDLIST"):
            endlist = True
        elif line.startswith("#"):
            continue
        else:
            if pending_start is None and last_start is not None and pending_duration:
                pending_start = last_start + pending_duration
            segments.append({
                "uri": line,
                "duration": pending_duration or 0.0,
                "title": pending_title,
                "start": pending_start,
                "offset": None,
            })
            last_start = pending_start
            pending_duration = None
            pending_title = None
            pending_start = None

    base = elapsed if elapsed is not None else 0.0
    running = 0.0
    for segment in segments:
        segment["offset"] = round(base + running, 3)
        running += segment["duration"]

    for window in ads:
        window["end"] = (
            window["start"] + window["duration"] if window["start"] is not None else None
        )

    return {
        "ads": ads,
        "segments": segments,
        "elapsed_secs": elapsed,
        "total_secs": total,
        "server_time": server_time,
        "endlist": endlist,
        "content_duration": round(base + running, 1),
    }


def _ad_window(attrs: dict, start: float | None) -> dict:
    """One stitched ad, as Twitch's own marker describes it."""
    position = _as_int(attrs.get("X-TV-TWITCH-AD-POD-POSITION"))
    return {
        "id": attrs.get("ID"),
        "start": start,
        "duration": _as_float(attrs.get("DURATION")) or 0.0,
        "pod_position": position,
        "pod_length": _as_int(attrs.get("X-TV-TWITCH-AD-POD-LENGTH")),
        "roll_type": attrs.get("X-TV-TWITCH-AD-ROLL-TYPE"),
        "ad_format": attrs.get("X-TV-TWITCH-AD-AD-FORMAT"),
        "click_url": attrs.get("X-TV-TWITCH-AD-URL")
        or attrs.get("X-TV-TWITCH-AD-CLICK-TRACKING-URL"),
        "commercial_id": attrs.get("X-TV-TWITCH-AD-COMMERCIAL-ID"),
        "creative_id": attrs.get("X-TV-TWITCH-AD-CREATIVE-ID"),
        "line_item_id": attrs.get("X-TV-TWITCH-AD-LINE-ITEM-ID"),
        "ad_session_id": attrs.get("X-TV-TWITCH-AD-AD-SESSION-ID"),
        "raw": attrs,
    }


def playlist_edge(parsed: dict) -> float | None:
    """The moment the playlist's newest segment airs -- the stream's own clock.

    Using the playlist rather than the local clock means the ad windows and the
    "current" moment are never compared across two machines' clocks.
    """
    for segment in reversed(parsed.get("segments") or []):
        if segment.get("start") is not None:
            return float(segment["start"])
    return parsed.get("server_time")


def ad_at(ads, when: float | None) -> dict | None:
    """The ad whose window contains ``when`` (Twitch's windows are contiguous)."""
    if when is None:
        return None
    for window in ads:
        start = window.get("start")
        if start is None:
            continue
        if start <= when < start + (window.get("duration") or 0.0):
            return window
    return None


def segment_at(segments, when: float | None) -> dict | None:
    """The segment airing at ``when``; the nearest earlier one if it is unlisted."""
    if when is None:
        return None
    fallback = None
    for segment in segments:
        start = segment.get("start")
        if start is None:
            continue
        if start <= when < start + (segment.get("duration") or 0.0):
            return segment
        if start <= when:
            fallback = segment
    return fallback


def segments_for_ad(window: dict, segments) -> list[dict]:
    """The segments belonging to one ad, matched on air time or segment title.

    The title fallback matters for playlists whose segments lost their
    ``#EXT-X-PROGRAM-DATE-TIME`` tags: Twitch labels ad segments with the stitched
    creative (``Amazon|2474283100494``), which is the marker's own creative id.
    """
    found = []
    start = window.get("start")
    end = start + (window.get("duration") or 0.0) if start is not None else None
    creative = window.get("creative_id")
    for segment in segments:
        launched = segment.get("start")
        if start is not None and end is not None and launched is not None:
            if start <= launched < end:
                found.append(segment)
                continue
        title = segment.get("title") or ""
        if creative and creative in title:
            found.append(segment)
    return found


# --------------------------------------------------------------------------- #
# Proof frames, decoded from the stream
# --------------------------------------------------------------------------- #
def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def decode_frame(uri: str, offset: float, target: str) -> bool:
    """Download one stream segment and decode a single frame at ``offset``.

    The offset is applied on the *output* side (``select``) rather than by seeking
    the input. Seeking into a short stitched segment lands mid-GOP without the
    reference frames that GOP needs, and ffmpeg then exits successfully having
    written no picture at all -- silently losing the frame. Decoding from the
    start and picking the frame by timestamp always produces one, and a frame
    from the segment's own first picture is used as a fallback.
    """
    binary = _ffmpeg()
    if binary is None:
        logger.debug("ffmpeg is not available; skipping ad frame")
        return False
    handle, temp_path = tempfile.mkstemp(suffix=".ts", prefix="twitch_seg_")
    os.close(handle)
    try:
        request = urllib.request.Request(uri, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            with open(temp_path, "wb") as out:
                shutil.copyfileobj(response, out)

        attempts: list[list[str]] = []
        if float(offset) > 0.01:
            attempts.append(["-vf", f"select=gte(t\\,{float(offset):.2f})",
                             "-frames:v", "1", "-vsync", "0"])
        attempts.append(["-frames:v", "1", "-vsync", "0"])

        for args in attempts:
            if os.path.exists(target):
                try:
                    os.remove(target)
                except OSError:
                    pass
            done = subprocess.run(
                [binary, "-y", "-loglevel", "error", "-nostdin", "-i", temp_path, *args, target],
                capture_output=True, text=True,
            )
            if done.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 0:
                return True
            logger.debug("ffmpeg frame decode failed: %s", (done.stderr or "")[-300:])
        return False
    except Exception as exc:
        logger.debug("could not decode ad frame from %s: %s", uri[:60], exc)
        return False
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def frame_fingerprint(path: str | None, size: int = 8) -> str | None:
    """A perceptual hash (dHash) of one frame, via ffmpeg -- no new dependency.

    The frame is reduced to a 9x8 grayscale grid and each pixel compared with its
    neighbour, which ignores brightness and scale but not content: the same slate
    or spot hashes the same on a later run, a different one does not.
    """
    binary = _ffmpeg()
    if binary is None or not path or not os.path.exists(path):
        return None
    try:
        done = subprocess.run(
            [binary, "-y", "-loglevel", "error", "-i", path,
             "-vf", f"scale={size + 1}:{size},format=gray",
             "-f", "rawvideo", "-"],
            capture_output=True,
        )
    except Exception as exc:
        logger.debug("could not fingerprint %s: %s", path, exc)
        return None
    pixels = done.stdout or b""
    if done.returncode != 0 or len(pixels) < (size + 1) * size:
        logger.debug("ffmpeg produced no pixels to fingerprint with")
        return None
    value = 0
    for row in range(size):
        base = row * (size + 1)
        for column in range(size):
            value = (value << 1) | (
                1 if pixels[base + column] > pixels[base + column + 1] else 0
            )
    return f"{value:0{size * size // 4}x}"


def fingerprint_distance(left: str | None, right: str | None) -> int:
    """How many bits of two fingerprints differ (64 when either is missing)."""
    if not left or not right or len(left) != len(right):
        return 64
    try:
        return bin(int(left, 16) ^ int(right, 16)).count("1")
    except ValueError:
        return 64


class CreativeRegistry:
    """The small pool of creatives Twitch keeps re-serving, remembered locally.

    Twitch's markers name a creative id but no brand, and the same handful of ads
    comes round again and again -- the house "commercial break in progress" slate
    most of all. Fingerprinting each ad's own first frame means a later run can
    say "this is the one you saw three times", even if Twitch hands out a fresh
    creative id for it. The label field is meant to be edited by hand: name a
    creative once and every later report and gallery shows the name.
    """

    def __init__(self, proof_dir: str, tolerance: int | None = None):
        self.proof_dir = proof_dir
        self.path = os.path.join(proof_dir, REGISTRY_FILE)
        self.tolerance = FINGERPRINT_TOLERANCE if tolerance is None else tolerance
        self.entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError):
            return
        if isinstance(saved, dict):
            self.entries = [e for e in (saved.get("creatives") or []) if isinstance(e, dict)]

    def save(self) -> None:
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "note": (
                "Twitch names no advertiser in its ad markers, so each creative is "
                "tracked by its fingerprint and the details the marker did give. "
                "Set 'label' on an entry to name that ad in later reports."
            ),
            "creatives": self.entries,
        }
        try:
            os.makedirs(self.proof_dir, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.debug("could not write the creative registry: %s", exc)

    def match(self, ad: dict, fingerprint: str | None) -> dict | None:
        """The entry this ad is a repeat of, if it is one."""
        creative = ad.get("creative_id")
        if creative:
            for entry in self.entries:
                if creative in (entry.get("creative_ids") or []):
                    return entry
        if not fingerprint:
            return None
        best = None
        for entry in self.entries:
            distance = min(
                (fingerprint_distance(fingerprint, known)
                 for known in self._fingerprints(entry)),
                default=64,
            )
            if distance <= self.tolerance and (best is None or distance < best[0]):
                best = (distance, entry)
        return best[1] if best else None

    @staticmethod
    def _fingerprints(entry: dict) -> list[str]:
        """Every hash this creative has been seen under (older files hold one)."""
        known = [fp for fp in (entry.get("fingerprints") or []) if fp]
        if entry.get("fingerprint") and entry["fingerprint"] not in known:
            known.append(entry["fingerprint"])
        return known

    def record(self, ad: dict, frame_path: str | None) -> dict:
        """File this ad against the pool and annotate it with what is known."""
        fingerprint = frame_fingerprint(frame_path)
        entry = self.match(ad, fingerprint)
        seen_at = datetime.now().isoformat(timespec="seconds")
        if entry is None:
            entry = {
                "id": f"creative-{len(self.entries) + 1:04d}",
                "fingerprint": fingerprint,
                "fingerprints": [fingerprint] if fingerprint else [],
                "creative_ids": [],
                "click_hosts": [],
                "house_ad": bool(ad.get("house_ad")),
                "label": None,
                "times_seen": 0,
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
                "channels": [],
                "placements": [],
                "thumbnail": os.path.basename(frame_path) if frame_path else None,
            }
            self.entries.append(entry)

        entry["times_seen"] = int(entry.get("times_seen") or 0) + 1
        entry["last_seen_at"] = seen_at
        if fingerprint:
            known = self._fingerprints(entry)
            if all(fingerprint_distance(fingerprint, previous) > 2 for previous in known):
                known.append(fingerprint)
            entry["fingerprints"] = known[-FINGERPRINTS_KEPT:]
            entry["fingerprint"] = entry["fingerprints"][0]
        if ad.get("creative_id") and ad["creative_id"] not in entry["creative_ids"]:
            entry["creative_ids"].append(ad["creative_id"])
        if ad.get("destination"):
            host = _host_of(ad["destination"])
            if host and host not in entry["click_hosts"]:
                entry["click_hosts"].append(host)
        if ad.get("channel") and ad["channel"] not in entry["channels"]:
            entry["channels"].append(ad["channel"])
        placement = ad.get("placement")
        if placement and placement not in entry["placements"]:
            entry["placements"].append(placement)
        if not entry.get("thumbnail") and frame_path:
            entry["thumbnail"] = os.path.basename(frame_path)

        ad["creative_ref"] = entry["id"]
        ad["times_seen"] = entry["times_seen"]
        ad["known_label"] = entry.get("label")
        ad["first_seen_at"] = entry.get("first_seen_at")
        self.save()
        logger.info(
            "Twitch creative %s (%s) seen %s time(s)",
            entry["id"], entry.get("label") or "unnamed", entry["times_seen"],
        )
        return entry

    def seen_label(self, entry: dict) -> str:
        """How to refer to a creative in prose: its name, or what it links to."""
        if entry.get("label"):
            return str(entry["label"])
        if entry.get("house_ad"):
            return "Twitch house slate"
        if entry.get("click_hosts"):
            return str(entry["click_hosts"][0])
        return str(entry.get("id"))


def _host_of(url: str | None) -> str | None:
    """A readable stand-in for the brand the marker does not tell us."""
    if not url:
        return None
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not host:
        return None
    if any(token in host for token in _TRACKER_HOSTS):
        return None
    return host[4:] if host.startswith("www.") else host


def is_house_ad(window: dict) -> bool:
    """True when Twitch filled the break with its own slate rather than an ad.

    Twitch pads a break it cannot sell with its own "Commercial break in
    progress" card, which clicks through to twitch.tv itself. There is no
    advertiser on those, and saying so is more useful than reporting a brand for
    a placeholder.
    """
    host = _host_of(window.get("click_url")) or ""
    return host == "twitch.tv" or host.endswith(".twitch.tv")


def _placement(window: dict) -> str:
    roll = (window.get("roll_type") or "").lower()
    if roll == "preroll":
        return "pre-roll"
    if roll == "midroll":
        return "mid-roll"
    return "unknown"


def _summarize(record: dict) -> str:
    """One line for the report: what the marker said, plus any repeat sighting.

    A creative that has been seen before is named -- by hand-set label, by the
    host it clicks through to, or by the registry id it has been filed under --
    rather than being re-described as an anonymous ad every time.
    """
    if record.get("known_label"):
        parts = [f"identified as {record['known_label']}"]
    elif record.get("house_ad"):
        parts = ["house/filler slate (Twitch's own break card, no advertiser)"]
    else:
        parts = ["stitched (ssai) ad"]
    if record.get("advertiser"):
        parts.append(f"via {record['advertiser']}")
    if record.get("duration"):
        parts.append(f"{record['duration']:.0f}s")
    if record.get("roll_type"):
        parts.append(str(record["roll_type"]).lower())
    if record.get("creative_id") and not record.get("known_label"):
        parts.append(f"creative {record['creative_id']}")
    if record.get("ad_index") and record.get("ad_pod_size"):
        parts.append(f"ad {record['ad_index']} of {record['ad_pod_size']}")
    if record.get("evidence") == "marker-only":
        parts.append("marker only (the break aired before we polled)")
    elif record.get("evidence") == "frames-unavailable":
        parts.append("no frame could be decoded")
    seen = int(record.get("times_seen") or 0)
    if record.get("creative_ref"):
        if seen > 1:
            parts.append(f"repeat sighting ({record['creative_ref']}, {seen} times)")
        else:
            parts.append(f"new creative {record['creative_ref']}")
    latency = record.get("detection_latency_s")
    if latency is not None:
        parts.append(f"noticed {float(latency):.1f}s into the ad")
    return " ".join(parts)


class _Proof:
    """Minimal recorder stand-in for the shared proof index/manifest writers."""

    def __init__(self, proof_dir: str, video_id: str):
        self.proof_dir = proof_dir
        self.video_id = video_id


class AdCollector:
    """Turns repeated playlist reads into served-ad records and proof frames.

    Every frame is pinned to a moment on the stream's own clock (a second before
    the ad, half a second into it, its midpoint, just before it ends, and a
    second after it). Whichever segment airs at that moment is the one decoded,
    so the strip is the ad's real start / middle / end bracketed by the content
    either side of it -- even when the break is still airing.
    """

    def __init__(self, proof_dir: str | None, video_id: str, frames: bool = True,
                 registry: "CreativeRegistry | None" = None, channel: str | None = None):
        self.proof_dir = proof_dir
        self.video_id = video_id
        self.channel = channel
        self.registry = registry
        self.frames = bool(frames and proof_dir)
        self.ads: list[dict] = []
        self.records: dict[str, dict] = {}
        self.awaiting_after: list[dict] = []
        self.recorded: set[str] = set()
        self.temp_dir: str | None = None
        self._next_ordinal = 1

    # -- bookkeeping ------------------------------------------------------ #
    def _temp(self) -> str:
        if self.temp_dir is None:
            self.temp_dir = os.path.join(
                tempfile.gettempdir(), "media_analyzer", f"twitchframes_{uuid.uuid4().hex[:8]}"
            )
            os.makedirs(self.temp_dir, exist_ok=True)
        return self.temp_dir

    def _write_target(self, record: dict, label: str) -> str:
        return os.path.join(self._temp(), f"{record['_key']}_{label}.png")

    def open(self, window: dict, parsed: dict, now: float) -> dict:
        edges = playlist_edge(parsed)
        first_seen = None
        if edges is not None and window.get("start") is not None:
            first_seen = max(0.0, edges - window["start"])
        record = {
            "_key": re.sub(r"[^A-Za-z0-9]+", "-", str(window.get("id") or uuid.uuid4().hex))[:40],
            "_ordinal": self._next_ordinal,
            "_window": window,
            "_opened": now,
            "_frames": {},
            "_frame_moments": {},
            "_moments": {},
            "_edge": edges,
            "_observed": False,
            "_coverage": 0.0,
            "_position": None,
            "_after_polls": 0,
        }
        if window.get("start") is not None:
            duration = window.get("duration") or 0.0
            record["_moments"] = {
                "before": window["start"] + FRAME_OFFSETS["before"],
                "start": window["start"] + FRAME_OFFSETS["start"],
                "mid": window["start"] + duration / 2 if duration else None,
                "end": window["start"] + duration + FRAME_OFFSETS["end"] if duration else None,
                "after": window["start"] + duration + FRAME_OFFSETS["after"] if duration else None,
            }
        if first_seen is not None:
            record["_latency"] = first_seen
        self._next_ordinal += 1
        self.records[window["id"]] = record
        return record

    # -- frames ----------------------------------------------------------- #
    def capture_targets(self, record: dict, parsed: dict, edge: float | None) -> None:
        """Decode whichever requested frames the stream has now moved past."""
        if not self.frames or edge is None:
            return
        if len(record["_frames"]) >= FRAME_LIMIT:
            return
        remaining = FRAME_LIMIT - len(record["_frames"])
        for label in ("before", "start", "mid", "end", "after"):
            if remaining <= 0:
                break
            moment = record["_moments"].get(label)
            if moment is None or                label in record["_frames"] or edge < moment:
                continue
            segment = segment_at(parsed["segments"], moment)
            if segment is None:
                continue
            offset = max(0.0, moment - (segment.get("start") or moment))
            duration = segment.get("duration") or 0.0
            if duration:
                offset = min(offset, duration - 0.05)
            if self._capture(record, label, segment, max(0.0, offset), moment):
                remaining -= 1
        self._drop_unreachable(record, edge)

    def _capture(self, record: dict, label: str, segment: dict, offset: float, moment: float) -> bool:
        target = self._write_target(record, label)
        if not decode_frame(segment["uri"], offset, target):
            return False
        record["_frames"][label] = target
        record["_frame_moments"][label] = moment
        return True

    def _drop_unreachable(self, record: dict, edge: float) -> None:
        """Stop waiting for a frame whose moment has scrolled out of the playlist."""
        for label, moment in list(record["_moments"].items()):
            if label in record["_frames"]:
                continue
            if moment is not None and edge - moment > 30:
                record["_moments"].pop(label, None)

    # -- closing ---------------------------------------------------------- #
    def close(self, record: dict) -> None:
        """The ad left the playlist window: finalise it (frames may still settle)."""
        window = record["_window"]
        window_id = window.get("id")
        wants_after = (
            self.frames
            and record["_moments"].get("after") is not None
            and "after" not in record["_frames"]
            and len(record["_frames"]) < FRAME_LIMIT
        )
        if wants_after:
            record["_after_polls"] = 0
            self.awaiting_after.append(record)
        else:
            self._finalize(record)
        # A Twitch session playlist keeps every marker it has ever carried, so a
        # break that has been recorded must not be picked up again next poll.
        self.recorded.add(window_id)
        self.records.pop(window_id, None)

    def resolve_after(self, parsed: dict, edge: float | None) -> None:
        """Give pending records a chance to pick up their "content after" frame."""
        still_waiting: list[dict] = []
        for record in self.awaiting_after:
            record["_after_polls"] += 1
            if self.frames and edge is not None:
                self.capture_targets(record, parsed, edge)
            if "after" in record["_frames"] or record["_after_polls"] > AFTER_POLLS:
                self._finalize(record)
            else:
                still_waiting.append(record)
        self.awaiting_after = still_waiting

    def finalize_all(self) -> None:
        for record in list(self.awaiting_after):
            self._finalize(record)
        self.awaiting_after = []
        for record in list(self.records.values()):
            self._finalize(record)
        self.records = {}

    def _finalize(self, record: dict) -> None:
        window = record["_window"]
        ad = {
            "platform": "twitch",
            "ad_id": window.get("id"),
            "ordinal": record["_ordinal"],
            "channel": self.channel,
            "placement": _placement(window),
            "format": TWITCH_FORMAT,
            "roll_type": window.get("roll_type"),
            "ad_format": window.get("ad_format"),
            "house_ad": is_house_ad(window),
            "advertiser": None if is_house_ad(window) else _host_of(window.get("click_url")),
            "destination": window.get("click_url"),
            "cta": None,
            "badge": None,
            "ad_index": window["pod_position"] + 1 if window.get("pod_position") is not None else None,
            "ad_pod_size": window.get("pod_length"),
            "duration": round(float(window.get("duration") or 0.0), 1),
            "duration_from_player": True,
            "skippable": False,
            "skip_after_s": None,
            "commercial_id": window.get("commercial_id"),
            "creative_id": window.get("creative_id"),
            "line_item_id": window.get("line_item_id"),
            "ad_session_id": window.get("ad_session_id"),
            "trigger": "playlist-marker",
            "elapsed_s": round(record["_opened"], 1),
            "observed_seconds": round(record["_coverage"], 1),
            "content_position": None if record["_position"] is None else round(record["_position"], 1),
            "content_duration": None,
            "wall_started_at": (
                datetime.fromtimestamp(window["start"]).isoformat(timespec="seconds")
                if window.get("start") is not None
                else None
            ),
            "first_seen_media_time": (
                None if record.get("_latency") is None else round(record["_latency"], 1)
            ),
            "detection_latency_s": (
                None if record.get("_latency") is None else round(record["_latency"], 1)
            ),
            "frames_source": "stream-segments",
            "frames": len(record["_frames"]),
            "ad_frames": 0,
            "clip": None,
            "thumbnail": None,
            "before_frame_s": None,
            "after_frame_s": None,
        }
        if record["_frames"]:
            ad["evidence"] = "stream-frames"
            ad["ad_frames"] = len(record["_frames"])
        elif record["_observed"]:
            # The break was watched while it aired, but no frame could be pulled
            # out of its segments (usually ffmpeg). The marker still stands.
            ad["evidence"] = "frames-unavailable"
        else:
            # The break was already over when the marker first showed up, so it
            # was never seen airing and a "how late we noticed" figure would be
            # meaningless.
            ad["evidence"] = "marker-only"
            ad["detection_latency_s"] = None
        self._save_frames(ad, window, record)
        if self.registry is not None:
            # Twitch re-serves the same few creatives, so filing this one against
            # the local pool is what turns "an unnamed ad" into "the one from
            # yesterday, seen 3 times now".
            self.registry.record(ad, ad.get("frame_start") or ad.get("frame_mid"))
        ad["summary"] = _summarize(ad)
        # Records waiting on their "content after" frame finish later than the ads
        # that followed them, so the list is kept in the order the stream aired
        # them rather than the order they happened to be written out.
        self.ads.append(ad)
        self.ads.sort(key=lambda item: item["ordinal"])
        logger.info(
            "Twitch served ad recorded (%s, %ss, %s)",
            ad["placement"], ad["duration"], ad["evidence"],
        )

    def _save_frames(self, ad: dict, window: dict, record: dict) -> None:
        """Name and move the decoded frames beside the YouTube path's proof files."""
        frames = record["_frames"]
        if not frames or not self.proof_dir:
            return
        index = ad["ordinal"]
        slug = served_ads._slug(ad.get("advertiser") or ad.get("creative_id"), "ad")
        stem = os.path.join(self.proof_dir, f"{self.video_id}_ad{index:02d}_{slug}")
        start = window.get("start")
        end = start + (window.get("duration") or 0.0) if start is not None else None
        os.makedirs(self.proof_dir, exist_ok=True)
        for position, label in enumerate(served_ads._LABELS):
            source = frames.get(label)
            if not source:
                continue
            target = f"{stem}_{position}_{label}.png"
            try:
                shutil.move(source, target)
            except OSError as exc:
                logger.debug("could not keep %s frame: %s", label, exc)
                continue
            ad[f"frame_{label}"] = target
            if label == "start":
                ad["thumbnail"] = target
        if ad.get("thumbnail") is None:
            for label in ("mid", "end", "before", "after"):
                if ad.get(f"frame_{label}"):
                    ad["thumbnail"] = ad[f"frame_{label}"]
                    break
        moments = record["_frame_moments"]
        if "before" in moments and start is not None:
            ad["before_frame_s"] = round(max(0.0, start - moments["before"]), 1)
        if "after" in moments and end is not None:
            ad["after_frame_s"] = round(max(0.0, moments["after"] - end), 1)

    def cleanup(self) -> None:
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None


# --------------------------------------------------------------------------- #
# Watching a live stream
# --------------------------------------------------------------------------- #
def refresh_position(record: dict, parsed: dict) -> None:
    """Record where the ad sits on the broadcast, in stream seconds."""
    window = record["_window"]
    if record["_position"] is not None or window.get("start") is None:
        return
    for segment in parsed["segments"]:
        start = segment.get("start")
        if start is None or segment.get("offset") is None:
            continue
        if start <= window["start"] < start + (segment.get("duration") or 0.0):
            record["_position"] = segment["offset"]
            return
        if start > window["start"]:
            record["_position"] = segment["offset"]
            return


def write_creatives_index(registry: CreativeRegistry, proof_dir: str) -> str | None:
    """Write a browsable gallery of every creative seen so far.

    Twitch's pool is small, so a page that shows each creative once -- with how
    often it has come round, where, and a frame of the ad itself -- is a much
    better view of "what ads does Twitch actually show me" than any single
    session's timeline.
    """
    if not registry.entries:
        return None
    rows = []
    for entry in sorted(registry.entries,
                        key=lambda e: int(e.get("times_seen") or 0), reverse=True):
        thumb = entry.get("thumbnail")
        media = f'<img src="{os.path.basename(thumb)}" alt="ad frame">' if thumb else ""
        label = registry.seen_label(entry)
        facts = [f"seen {entry.get('times_seen', 0)}x"]
        if entry.get("house_ad"):
            facts.append("Twitch's own break slate")
        if entry.get("creative_ids"):
            facts.append("creative " + ", ".join(str(c) for c in entry["creative_ids"]))
        if entry.get("click_hosts"):
            facts.append("clicks to " + ", ".join(str(c) for c in entry["click_hosts"]))
        if entry.get("placements"):
            facts.append("as " + ", ".join(str(p) for p in entry["placements"]))
        if entry.get("channels"):
            facts.append("on " + ", ".join(str(c) for c in entry["channels"]))
        facts.append(f"first {entry.get('first_seen_at')}")
        facts.append(f"last {entry.get('last_seen_at')}")
        rows.append(
            "<li>"
            f'<div class="when">{label} <span class="ref">{entry.get("id")}</span></div>'
            f'<div class="meta">{" &middot; ".join(facts)}</div>'
            f'<div class="meta">fingerprint <code>{entry.get("fingerprint") or "n/a"}</code>'
            + (" &middot; name it by editing <code>label</code> in the registry JSON"
               if not entry.get("label") else "")
            + f'</div>{media}</li>'
        )
    html = (
        "<!DOCTYPE html><meta charset='utf-8'>"
        "<title>Twitch creatives seen so far</title>"
        "<style>body{font:14px system-ui;margin:0;padding:20px;background:#0f1115;color:#e8eaf0}"
        "li{list-style:none;margin:0 0 24px;padding:14px;background:#171a21;border-radius:10px}"
        ".when{font-weight:600;font-size:16px} .ref{color:#9aa3b2;font-weight:400}"
        ".meta{color:#9aa3b2;margin:4px 0 10px}"
        "img{width:100%;max-width:700px;border-radius:6px;display:block;margin-top:8px}"
        "code{color:#cbd5e1}</style>"
        "<h1>Twitch creatives seen so far</h1>"
        f"<p class='meta'>{len(registry.entries)} creative(s) in the local registry "
        "&middot; Twitch names no advertiser in its markers, so each one is "
        "identified by its fingerprint and whatever the marker did say.</p><ul>"
        + "".join(rows) + "</ul>"
    )
    path = os.path.join(proof_dir, CREATIVES_PAGE)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(html)
    except OSError as exc:
        logger.debug("could not write the creative gallery: %s", exc)
        return None
    return path


def watch_live(media_url: str, collector: AdCollector, budget: float,
               started: float) -> dict:
    """Poll the playlist until the budget runs out, recording every ad break."""
    stats = {"polls": 0, "failed_polls": 0, "content_duration": None, "ads": 0}
    deadline = started + budget
    while True:
        now = time.time()
        if now >= deadline:
            break
        parsed = _poll_playlist(media_url)
        if parsed is not None:
            stats["polls"] += 1
            # For a live stream the meaningful "duration" is how long the
            # broadcast has been running, which is what Twitch's own counter says.
            stats["content_duration"] = parsed.get("total_secs") or parsed.get("content_duration")
            edge = playlist_edge(parsed)
            _advance(collector, parsed, edge, now - started)
            collector.resolve_after(parsed, edge)
        else:
            stats["failed_polls"] += 1
        time.sleep(POLL_SECONDS)
    collector.finalize_all()
    return stats


def _poll_playlist(media_url: str) -> dict | None:
    try:
        return parse_playlist(_fetch_text(media_url))
    except Exception as exc:
        logger.debug("could not read the Twitch playlist: %s", exc)
        return None


def _advance(collector: AdCollector, parsed: dict, edge: float | None,
             now: float) -> None:
    """Open, extend or close a record for every marker in the playlist."""
    for window in parsed["ads"]:
        start = window.get("start")
        if start is None or edge is None:
            continue
        if window["id"] in collector.recorded:
            continue  # already recorded (the playlist keeps past markers)
        if edge < start:
            continue  # announced but not on air yet
        record = collector.records.get(window["id"])
        if record is None:
            record = collector.open(window, parsed, now)
        refresh_position(record, parsed)
        record["_edge"] = edge
        # Seen while it was actually on air (rather than already over), which is
        # what makes the "how late did we notice" figure meaningful.
        record["_observed"] = True
        record["_coverage"] = max(
            record["_coverage"], min(edge, start + (window.get("duration") or 0.0)) - start
        )
        collector.capture_targets(record, parsed, edge)
        if edge >= start + (window.get("duration") or 0.0):
            collector.close(record)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def capture_twitch_ads(url: str, watch_seconds: float | None = None,
                       headless: bool | None = None,
                       proof_dir: str | None = None,
                       record: bool | None = None,
                       full_watch: bool | None = None,
                       max_seconds: float | None = None,
                       video_id: str | None = None) -> dict:
    """Record the ads Twitch stitches into a live stream, without a browser.

    Signature-compatible with ``served_ads.capture_served_ads`` so the pipeline
    can hand either platform to the same call. ``headless``/``full_watch`` are
    accepted and ignored: there is no browser here, and the whole timeline of a
    live stream is available as it airs.
    """
    result = {
        "available": False,
        "captured": False,
        "ads": [],
        "ad_count": 0,
        "ad_seconds": 0.0,
        "watched_seconds": 0.0,
        "strategy": "live",
        "proof_dir": None,
        "manifest": None,
        "note": "",
        "source": "stream-markers",
        "channel": None,
        "vods_supported": False,
    }

    target = twitch_target(url)
    if target is None:
        result["note"] = (
            "Not a Twitch channel or VOD link, so the Twitch ad reader was not used."
        )
        return result
    kind, ident = target
    result["channel"] = ident
    if kind == "vod":
        result["strategy"] = "vod"
        result["available"] = True
        result["note"] = (
            "Twitch VOD playlists carry no ad markers (the recorded timeline is the "
            "stream's own media, and Twitch does not expose which parts of an archive "
            "were ad breaks), so served ads cannot be read from a /videos link. Point "
            "the capture at the live channel URL to catch pre-rolls and mid-rolls as "
            "they air."
        )
        return result

    started = time.time()
    budget = float(watch_seconds if watch_seconds else WATCH_SECONDS)
    if max_seconds is not None:
        budget = min(budget, float(max_seconds))
    if record is None:
        record = served_ads.RECORD

    resolved_id = video_id or ident
    proof = None
    if record:
        proof = _Proof(proof_dir or served_ads.DEFAULT_PROOF_DIR, resolved_id)
        os.makedirs(proof.proof_dir, exist_ok=True)
        result["proof_dir"] = proof.proof_dir

    try:
        media_url = media_playlist_url(kind, ident)
    except TwitchError as exc:
        result["note"] = f"Could not open a Twitch playback session: {exc}"
        return result
    except (urllib.error.URLError, OSError) as exc:
        result["note"] = f"Could not reach Twitch: {exc}"
        return result

    result["available"] = True
    registry = CreativeRegistry(proof.proof_dir) if proof else None
    collector = AdCollector(
        proof.proof_dir if proof else None,
        resolved_id,
        frames=bool(record) and FRAME_LIMIT > 0,
        registry=registry,
        channel=ident,
    )
    try:
        stats = watch_live(media_url, collector, budget, started)
    except Exception as exc:  # capture must never break the pipeline
        logger.warning("Twitch ad capture failed: %s", exc)
        result["note"] = f"Twitch ad capture failed: {exc}"
        return result
    finally:
        collector.cleanup()

    ads = collector.ads
    result["watched_seconds"] = round(time.time() - started, 1)
    result["ads"] = ads
    result["ad_count"] = len(ads)
    result["ad_seconds"] = round(sum(a["duration"] for a in ads), 1)
    result["captured"] = bool(ads)
    result["polls"] = stats.get("polls", 0)
    result["content_duration"] = stats.get("content_duration")
    for ad in ads:
        ad["content_duration"] = stats.get("content_duration")
    if proof is not None:
        result["manifest"] = served_ads._write_manifest(
            proof, url, resolved_id, ads, result, result["strategy"]
        )
        result["index"] = served_ads._write_index(proof, url, ads)
        if registry is not None:
            result["creatives"] = [
                {key: entry.get(key) for key in
                 ("id", "label", "fingerprint", "times_seen", "house_ad",
                  "creative_ids", "click_hosts", "channels", "thumbnail")}
                for entry in registry.entries
            ]
            result["creative_count"] = len(registry.entries)
            result["creatives_index"] = write_creatives_index(registry, proof.proof_dir)
    result["note"] = _note(result, ads, ident, stats)
    return result


def _note(result: dict, ads, login: str, stats: dict) -> str:
    watched = int(result["watched_seconds"])
    if not ads:
        return (
            f"Read the live playlist of twitch.tv/{login} for {watched}s "
            f"({stats.get('polls', 0)} polls) and no stitched ad was served to this "
            "session. Twitch personalises and frequency-caps ads, and mid-rolls only "
            "appear when the streamer runs a break, so a repeat run can differ."
        )
    pods = sum(1 for ad in ads if (ad.get("ad_pod_size") or 1) > 1)
    note = (
        f"Captured {len(ads)} served ad(s) from the stream's own ad markers over "
        f"{watched}s of twitch.tv/{login}"
    )
    if pods:
        note += f", including {pods} inside a pod"
    repeats = [ad for ad in ads if int(ad.get("times_seen") or 0) > 1]
    if repeats:
        names = ", ".join(sorted({
            str(ad.get("known_label") or ad.get("creative_ref") or "a known creative")
            for ad in repeats
        }))
        note += (
            f". {len(repeats)} of them are creatives the local registry has seen "
            f"before ({names})"
        )
    house = sum(1 for ad in ads if ad.get("house_ad"))
    if house:
        note += (
            f". {house} of them filled by Twitch's own \"commercial break in "
            "progress\" slate rather than a paid spot, which is what a fresh, "
            "logged-out session is most often given"
        )
    note += (
        ". Twitch stitches ads into the stream (SSAI), so there is no overlay to "
        "read: these come from the playlist's twitch-stitched-ad markers, and each "
        "ad's frames were decoded from the segments it stitched in."
    )
    if not any(ad.get("frame_start") for ad in ads):
        if not _ffmpeg():
            note += " No frames were decoded: ffmpeg is not available on this machine."
        elif any(ad.get("evidence") == "frames-unavailable" for ad in ads):
            note += (
                " No frames could be decoded from the stitched segments for at "
                "least one ad; its marker is still on record."
            )
    return note
