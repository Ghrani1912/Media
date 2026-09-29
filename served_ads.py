"""Capture the *served* ads YouTube plays during a real viewing session.

There are two very different things people call "ads" on YouTube:

* **In-video sponsor reads** -- a creator reading a sponsor script. Those live
  inside the media file and are handled by ``ads.py`` (SponsorBlock + transcript).

* **Served ads** -- the pre-roll/mid-roll spots YouTube injects *while you watch*,
  the ones that draw the yellow segments on the progress bar and sometimes show a
  "Skip Ad" button. These are chosen at play time, are personalized, and are never
  part of the downloaded stream, so no amount of audio analysis can recover them.

The only reliable way to see served ads is to watch the video the way a person
does: open it in a browser and read the player's ad overlay. That is what this
module does, using Selenium to drive a local Chrome.

Twitch is different again: it *stitches* ads into the HLS stream instead of
overlaying them, so there is no player state to read and the browser trick has
nothing to watch. Twitch links are therefore handed to ``twitch_ads``, which
reads the stream's own ``twitch-stitched-ad`` markers; ``detect_platform`` picks
the right reader and ``capture_served_ads`` stays the single entry point for
either platform.

Covering the whole video
------------------------
Watching a 30-minute video in real time is slow, so the default strategy is a
**seek sweep**: watch the opening for pre-rolls, then jump along the timeline
(``SEEK_STEP`` seconds at a time). Seeking past a mid-roll break makes the player
serve that break's ad, so a few minutes of sweeping covers the full runtime. Pass
``full_watch=True`` to play straight through instead, which is exact but slow.

Proof clips
-----------
With ``proof_dir`` set the player is screenshotted on *every* poll -- while an ad
plays and for a second or two either side of it -- so the transition from content
into the ad and back is on record. Each ad is saved as a labelled sequence
(``before``, ``start``, ``mid``, ``end``, ``after``) and stitched into a short
mp4. Read together with ``first_seen_ad_time`` -- how far into the ad's own
playback the player already was on the first poll that admitted an ad was on
screen -- that shows whether detection fires the moment the ad starts or only
partway through it. A ``manifest.json`` records the metadata, the video timeline
position, the wall-clock time, and the frame timings of each ad.

Requires: ``selenium`` and Google Chrome (or Edge); ``ffmpeg`` for the clips.
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
import urllib.parse
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_PROOF_DIR = os.environ.get("SERVED_ADS_PROOF_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "proof"
)

# How long to watch the opening, where pre-rolls land.
PRE_ROLL_SECONDS = float(os.environ.get("SERVED_ADS_PRE_ROLL_SECONDS", "35"))
# Time spent parked at each seek stop, waiting for a mid-roll break to fire.
SEEK_DWELL_SECONDS = float(os.environ.get("SERVED_ADS_SEEK_DWELL", "14"))
# Once an ad does fire, the sweep keeps waiting for it (and for the content that
# follows) rather than moving on, so the whole ad is on record. This is the cap
# that stops a stuck ad from eating the whole budget.
AD_WAIT_SECONDS = float(os.environ.get("SERVED_ADS_AD_WAIT", "120"))
# How long to film the page settling, before any watch window starts. The pre-roll
# often begins in these first seconds, so they are the only chance to catch what
# it interrupted.
WARMUP_SECONDS = float(os.environ.get("SERVED_ADS_WARMUP", "8"))
# A pod's next spot restarts the ad's own playhead. A drop of more than this many
# seconds, back to within the tolerance of zero, means "new ad" even when the
# advertiser card is identical.
POD_RESTART_SECONDS = float(os.environ.get("SERVED_ADS_POD_RESTART", "5"))
POD_RESTART_TOLERANCE = float(os.environ.get("SERVED_ADS_POD_RESTART_TOLERANCE", "3"))
# An ad this short or shorter is a bumper rather than an in-stream spot.
BUMPER_SECONDS = float(os.environ.get("SERVED_ADS_BUMPER_SECONDS", "7"))
# How far to jump between seek stops.
SEEK_STEP_SECONDS = float(os.environ.get("SERVED_ADS_SEEK_STEP", "120"))
# Safety ceiling for a single capture.
MAX_SECONDS = float(os.environ.get("SERVED_ADS_MAX_SECONDS", "900"))

# Headless Chrome is not served ads, so the visible browser is the default.
HEADLESS = os.environ.get("SERVED_ADS_HEADLESS", "").strip().lower() in {"1", "true", "yes"}
# Play the whole video instead of sweeping it (exact, but real-time slow).
FULL_WATCH = os.environ.get("SERVED_ADS_FULL", "").strip().lower() in {"1", "true", "yes"}
# Record proof clips by default; disable with SERVED_ADS_RECORD=0.
RECORD = os.environ.get("SERVED_ADS_RECORD", "1").strip().lower() not in {"0", "false", "no"}
# Frames kept from *before* an ad is noticed (a rolling lead-in), so the moment
# content turns into an ad is on record. At one poll per second this is ~3s.
BUFFER_FRAMES = max(1, int(os.environ.get("SERVED_ADS_BUFFER_FRAMES", "3")))
# How long to keep filming after an ad ends, to catch the return to content.
POST_ROLL_SECONDS = float(os.environ.get("SERVED_ADS_POST_ROLL_SECONDS", "2"))
# Which lead-in/lead-out frame to keep as the labelled boundary snapshot.
BOUNDARY_SECONDS = float(os.environ.get("SERVED_ADS_BOUNDARY_SECONDS", "1"))

# A persistent profile makes each capture look like a returning visitor (so the
# consent dialog is answered only once) instead of a brand-new automated session.
# Point SERVED_ADS_PROFILE at an existing Chrome profile dir to reuse it.
PROFILE_DIR = os.environ.get("SERVED_ADS_PROFILE") or os.path.join(
    tempfile.gettempdir(), "media_analyzer", "chrome-profile"
)

_CHROME_CANDIDATES = [
    os.environ.get("CHROME_BINARY"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

# Reads the YouTube ad overlay and the player's transport state.
# NOTE: this must stay a bare script body with a top-level `return` -- Selenium
# runs it as a function body, so wrapping it in an IIFE would discard the value
# and silently yield None on every poll.
_EXTRACT_JS = r"""
  const q = (s) => document.querySelector(s);
  const t = (s) => { const e = q(s); return e ? (e.textContent || '').trim() : null; };
  const player = q('.html5-video-player');
  const video = q('video');

  let contentTime = null, contentDuration = null, ended = false, paused = true;
  try {
    if (player && typeof player.getCurrentTime === 'function') contentTime = player.getCurrentTime();
    if (player && typeof player.getDuration === 'function') contentDuration = player.getDuration();
    if (video) { ended = video.ended; paused = video.paused; }
  } catch (e) {}

  // "1 of 2" can be rendered in either the ad slot or the overlay layout.
  const podText = (t('.video-ads') || '') + ' ' + (t('.ytp-ad-player-overlay-layout') || '');
  const pod = podText.match(/(\d+)\s*of\s*(\d+)/);

  // The badge YouTube stamps on the ad ("Ad", "Sponsored"). Best effort: not
  // every player layout renders one.
  const badge = t('.ytp-ad-simple-ad-badge') || t('.ytp-ad-badge__text') || t('.ytp-ad-badge');

  // The skip button stays in the DOM between ads, so require it to be visible.
  let skippable = false;
  try {
    const skipEl = q('.ytp-skip-ad-button') || q('.ytp-ad-skip-button-modern');
    if (skipEl) { const r = skipEl.getBoundingClientRect(); skippable = r.width > 0 && r.height > 0; }
  } catch (e) {}

  // While an ad plays the video element is the ad, so currentTime is how far
  // into the ad we already are -- i.e. how late detection is.
  let adTime = null, adDuration = null;
  try {
    if (video && !isNaN(video.currentTime)) adTime = video.currentTime;
    if (video && !isNaN(video.duration)) adDuration = video.duration;
  } catch (e) {}

  let rect = null;
  try { if (player) { const r = player.getBoundingClientRect(); rect = {x: r.x, y: r.y, w: r.width, h: r.height}; } } catch (e) {}

  return JSON.stringify({
    showing: !!(player && player.classList.contains('ad-showing')),
    advertiser: t('.ytp-ad-avatar-lockup-card__headline'),
    destination: t('.ytp-ad-avatar-lockup-card__description'),
    cta: t('.ytp-ad-button-vm__text'),
    ad_index: pod ? parseInt(pod[1], 10) : null,
    ad_pod_size: pod ? parseInt(pod[2], 10) : null,
    ad_time: adTime,
    ad_duration: adDuration,
    skippable: skippable,
    skip_text: t('.ytp-skip-ad-button__text'),
    badge: badge,
    content_time: contentTime,
    content_duration: contentDuration,
    ended: ended,
    paused: paused,
    player_rect: rect
  });
"""

# Reads the Twitch VOD player's ad overlay and transport state. Twitch renders
# a purple "_ad-overlay" root while an ad plays, with advertiser text, a CTA
# and (newer layouts) a countdown; the <video> element is the ad itself, so the
# playhead/duration readouts work exactly like YouTube's. Same bare-body/return
# contract as _EXTRACT_JS above.
_TWITCH_EXTRACT_JS = r"""
  const q = (s) => document.querySelector(s);
  const t = (s) => { const el = q(s); return el ? el.textContent.trim() : null; };

  const video = q('video');
  const overlay = q('[data-a-target="video-ad-overlay"]')
      || q('.ad-showing') || q('.video-refactor-ad-layout');

  let contentTime = null, contentDuration = null, ended = false, paused = true;
  try {
    if (video) {
      ended = video.ended; paused = video.paused;
      if (!isNaN(video.duration) && video.duration > 0 && !video.ended) {
        // While an ad plays the element IS the ad; otherwise it is the VOD.
        if (overlay) { var adDuration = video.duration; var adTime = video.currentTime; }
        else {
          contentDuration = video.duration;
          contentTime = video.currentTime;
        }
      }
    }
  } catch (e) {}

  const countdown = t('[data-a-target="video-ad-label"]')
      || t('.ad-countdown') || t('[class*="ad-countdown"]');
  const podText = (countdown || '') + ' ' + (t('[class*="ad-overlay"]') || '');
  const pod = podText.match(/(\d+)\s*of\s*(\d+)/);

  let rect = null;
  try { const p = q('.video-player__container') || q('video'); if (p) { const r = p.getBoundingClientRect(); rect = {x: r.x, y: r.y, w: r.width, h: r.height}; } } catch (e) {}

  return JSON.stringify({
    showing: !!overlay,
    advertiser: t('[data-a-target="video-ad-banner-title"]') || t('[class*="ad-overlay"] [class*="brand"]'),
    destination: t('[data-a-target="video-ad-banner-subtitle"]'),
    cta: t('[data-a-target="video-ad-cta"]') || t('[class*="ad-overlay"] button span'),
    badge: countdown,
    ad_index: pod ? parseInt(pod[1], 10) : null,
    ad_pod_size: pod ? parseInt(pod[2], 10) : null,
    ad_time: typeof adTime !== 'undefined' ? adTime : null,
    ad_duration: typeof adDuration !== 'undefined' ? adDuration : null,
    skippable: false,
    skip_text: null,
    content_time: contentTime,
    content_duration: contentDuration,
    ended: ended,
    paused: paused,
    player_rect: rect
  });
"""

# The rolling gallery of Twitch creatives, written by ``twitch_ads``; the proof
# index links to it whenever it sits beside the frames.
CREATIVES_PAGE = "creatives.html"

_YOUTUBE_HOSTS = ("youtube.com", "youtube-nocookie.com")


def detect_platform(url: str) -> str:
    """Which served-ad reader a link needs: ``youtube``, ``twitch`` or ``unknown``."""
    try:
        host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return "unknown"
    if host == "youtu.be" or any(
        host == name or host.endswith("." + name) for name in _YOUTUBE_HOSTS
    ):
        return "youtube"
    if host == "twitch.tv" or host.endswith(".twitch.tv"):
        return "twitch"
    return "unknown"


_DISMISS_SELECTORS = (
    "button[aria-label*='Accept all']",
    "button[aria-label*='Accept']",
    "button[aria-label*='Reject all']",
)


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #
def _find_chrome() -> str | None:
    for candidate in _CHROME_CANDIDATES:
        if candidate and os.path.exists(candidate):
            return candidate
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def availability() -> tuple[bool, str]:
    """Return (is_available, reason). Never raises."""
    try:
        import selenium  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on environment
        return False, f"selenium is not installed ({exc})"

    if _find_chrome() is None:
        return False, "Google Chrome was not found on this machine"
    return True, ""


# --------------------------------------------------------------------------- #
# Browser plumbing
# --------------------------------------------------------------------------- #
def _build_options(headless: bool):
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--mute-audio")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--autoplay-policy=no-user-gesture-required")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument("--log-level=3")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    except OSError as exc:  # fall back to a throwaway profile
        logger.debug("could not prepare capture profile: %s", exc)
    binary = _find_chrome()
    if binary:
        opts.binary_location = binary
    return opts


def _new_driver(headless: bool):
    """Start Chrome, retrying once with a throwaway profile on a failed launch.

    "session not created: Chrome instance exited" almost always means the
    shared capture profile was locked by an orphaned chrome.exe or left in a
    state a Chrome update can't reuse. A throwaway profile sidesteps both;
    the capture loses its cookies, which YouTube does not require.
    """
    from selenium import webdriver
    from selenium.common.exceptions import SessionNotCreatedException

    opts = _build_options(headless)
    try:
        driver = webdriver.Chrome(options=opts)
    except SessionNotCreatedException as exc:
        if "Chrome instance exited" not in str(exc) and "devtoolsActivePort" \
                not in str(exc).lower():
            raise
        logger.warning("Chrome launch failed with the capture profile (%s); "
                       "retrying with a fresh profile.", str(exc)[:140])
        fresh = _build_options(headless)
        fresh.add_argument(
            f"--user-data-dir={tempfile.mkdtemp(prefix='ma-chrome-')}"
        )
        driver = webdriver.Chrome(options=fresh)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
        )
    except Exception:
        pass
    return driver


def _poll(driver, extract_js: str | None = None) -> dict | None:
    """Read one snapshot of the player's ad state, or None if it can't be read."""
    try:
        raw = driver.execute_script(extract_js or _EXTRACT_JS)
    except Exception as exc:
        logger.warning("ad overlay read failed: %s", exc)
        return None
    if not isinstance(raw, str):
        # A None here means the injected script returned nothing -- e.g. it was
        # wrapped so Selenium discarded its value. Surface it instead of
        # quietly reporting "no ads served".
        logger.warning("ad overlay read returned %r (expected JSON text)", raw)
        return None
    try:
        return json.loads(raw)
    except ValueError:
        logger.warning("ad overlay JSON could not be parsed")
        return None


def _dismiss_consent(driver) -> None:
    for selector in _DISMISS_SELECTORS:
        try:
            buttons = driver.find_elements("css selector", selector)
        except Exception:
            return
        if buttons:
            try:
                buttons[0].click()
                time.sleep(1.5)
            except Exception:
                pass
            return


def _play(driver) -> None:
    """Press play: a paused ad or a stalled playhead would hold a session open."""
    try:
        driver.execute_script(
            "const v=document.querySelector('video'); if (v && v.paused) v.play();"
        )
    except Exception as exc:
        logger.debug("play nudge failed: %s", exc)


def _seek(driver, position: float) -> None:
    """Jump the content video to ``position`` seconds."""
    try:
        driver.execute_script(
            "const v=document.querySelector('video');"
            f"if (v) {{ v.currentTime={float(position)}; v.play(); }}"
        )
    except Exception as exc:
        logger.debug("seek to %ss failed: %s", position, exc)


# --------------------------------------------------------------------------- #
# Proof clips
# --------------------------------------------------------------------------- #
def _slug(value: str | None, fallback: str = "ad") -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", (value or "")).strip("-").lower()
    return text[:40] or fallback


def probe_title(url: str) -> str | None:
    """Best-effort video title via yt-dlp metadata, without downloading.

    Uses flat extraction (no format resolution), so it is one quick network
    round trip. Returns None when the title cannot be had — callers fall back
    to :func:`fallback_title`.
    """
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                               "skip_download": True, "extract_flat": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        title = (info or {}).get("title")
        return str(title).strip() or None
    except Exception:
        return None


def fallback_title(url: str) -> str:
    """Short human label for a link when no video title can be probed.

    Twitch: the channel login. YouTube: the 11-char video id. Anything else:
    the URL's path tail. Only shapes the report's headline — never used for
    lookups.
    """
    platform = detect_platform(url)
    if platform == "twitch":
        import twitch_ads  # local import: twitch_ads imports this module

        return twitch_ads.twitch_target(url)[1] or url
    import ads  # local import: ads.py imports this module's format helpers

    video_id = ads.extract_video_id(url)
    if video_id:
        return video_id
    tail = urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return tail or url


class _ClipRecorder:
    """Films the player every poll and stitches labelled proof frames.

    Frames are captured whether or not an ad is on screen: a rolling buffer keeps
    the content immediately *before* an ad, and filming carries on briefly after
    it ends. A finished ad therefore becomes a labelled sequence -- ``before``,
    ``start``, ``mid``, ``end``, ``after`` -- which, read alongside
    ``first_seen_ad_time`` (how far into the ad's own playback the first poll
    that showed an ad already was), shows whether detection fires the moment the
    ad starts or only somewhere in the middle of it.
    """

    def __init__(self, proof_dir: str, video_id: str | None, enabled: bool = True):
        self.proof_dir = proof_dir
        self.video_id = video_id or "video"
        self.enabled = enabled
        self.rect: dict | None = None
        self.frames_dir: str | None = None
        self.counter = 0
        self.tracked: set[str] = set()
        self.buffer: list[dict] = []
        self.active: dict | None = None
        self.pending: list[dict] = []
        self.checkpoint_frame: dict | None = None
        os.makedirs(self.proof_dir, exist_ok=True)

    # -- capture ---------------------------------------------------------- #
    def _next_frame_path(self) -> str:
        if self.frames_dir is None:
            self.frames_dir = os.path.join(
                tempfile.gettempdir(), "media_analyzer", f"adframes_{uuid.uuid4().hex[:8]}"
            )
            os.makedirs(self.frames_dir, exist_ok=True)
        path = os.path.join(self.frames_dir, f"frame_{self.counter:05d}.png")
        self.counter += 1
        self.tracked.add(path)
        return path

    def tick(self, driver, now: float, showing: bool, rect=None) -> None:
        """Capture one frame. Call on *every* poll, ad or no ad."""
        if not self.enabled:
            return
        if rect and float(rect.get("w") or 0) > 0:
            self.rect = rect
        path = self._next_frame_path()
        try:
            driver.save_screenshot(path)
        except Exception as exc:
            logger.debug("ad screenshot failed: %s", exc)
            self._forget(path)
            return
        frame = {"path": path, "t": float(now), "showing": bool(showing)}
        self.buffer.append(frame)
        self.buffer = self.buffer[-BUFFER_FRAMES:]
        if self.active is not None:
            self.active["frames"].append(frame)
        for entry in self.pending:
            if entry.get("frozen") is not None:
                continue
            if float(now) - entry["ended"] <= POST_ROLL_SECONDS:
                entry["frames"].append(frame)
        self._prune()

    def begin(self, rect, now: float) -> None:
        """An ad was just noticed: start a sequence, seeded with the lead-in."""
        if not self.enabled:
            return
        if rect and float(rect.get("w") or 0) > 0:
            self.rect = rect
        seed = [f for f in self.buffer if not f["showing"]]
        if not seed and self.checkpoint_frame is not None:
            seed = [dict(self.checkpoint_frame)]
        self.active = {"frames": seed, "started": float(now)}

    def checkpoint(self, driver, now: float) -> None:
        """Stash the current frame as the lead-in for an ad that may follow.

        A seek serves the next break's ad almost immediately, so no frame of the
        content at the new position is ever drawn. The last content frame before
        the jump is then the best available "just before the ad" evidence.
        """
        if not self.enabled:
            return
        path = self._next_frame_path()
        try:
            driver.save_screenshot(path)
        except Exception as exc:
            logger.debug("checkpoint screenshot failed: %s", exc)
            self._forget(path)
            return
        if self.checkpoint_frame is not None:
            self._forget(self.checkpoint_frame["path"])
        self.checkpoint_frame = {"path": path, "t": float(now), "showing": False}

    def freeze(self) -> None:
        """Stop filming post-roll frames: the playhead is about to jump away."""
        for entry in self.pending:
            entry.setdefault("frozen", len(entry["frames"]))

    def finish(self, ad: dict, index: int, now: float) -> None:
        """The ad left the screen; keep filming briefly to catch the return."""
        if not self.enabled:
            return
        active = self.active
        self.active = None
        if active and active["frames"]:
            self.pending.append(
                {"ad": ad, "index": index, "frames": active["frames"], "ended": float(now)}
            )

    def flush_ready(self, now: float, force: bool = False) -> None:
        """Write out every ad whose post-roll window has finished filming."""
        if not self.enabled:
            return
        waiting = []
        for entry in self.pending:
            if force or float(now) - entry["ended"] >= POST_ROLL_SECONDS:
                self._write(entry)
            else:
                waiting.append(entry)
        self.pending = waiting

    def close(self, now: float) -> None:
        if not self.enabled:
            return
        self.flush_ready(now, force=True)
        if self.frames_dir:
            shutil.rmtree(self.frames_dir, ignore_errors=True)
        self.frames_dir = None
        self.tracked.clear()
        self.buffer = []

    # -- internals -------------------------------------------------------- #
    def _entry_frames(self, entry: dict) -> list[dict]:
        """Frames belonging to an ad; a freeze cuts off anything filmed later."""
        frames = entry["frames"]
        frozen = entry.get("frozen")
        return frames[:frozen] if frozen is not None else frames

    def _kept_paths(self) -> set[str]:
        kept = {f["path"] for f in self.buffer}
        if self.checkpoint_frame is not None:
            kept.add(self.checkpoint_frame["path"])
        if self.active is not None:
            kept.update(f["path"] for f in self.active["frames"])
        for entry in self.pending:
            kept.update(f["path"] for f in self._entry_frames(entry))
        return kept

    def _prune(self) -> None:
        """Drop frames that have fallen out of every window (keeps temp small)."""
        for path in list(self.tracked - self._kept_paths()):
            self._forget(path)

    def _forget(self, path: str) -> None:
        self.tracked.discard(path)
        try:
            os.remove(path)
        except OSError:
            pass

    def _write(self, entry: dict) -> None:
        """Save the labelled frames for one ad and stitch them into a clip."""
        ad = entry["ad"]
        frames = self._entry_frames(entry)
        shown = [f for f in frames if f["showing"]]
        if len(frames) < 2 or not shown:
            return

        first, last = shown[0], shown[-1]
        lead_in = [f for f in frames if not f["showing"] and f["t"] < first["t"]]
        lead_out = [f for f in frames if not f["showing"] and f["t"] > last["t"]]

        stem = os.path.join(
            self.proof_dir,
            f"{self.video_id}_ad{entry['index']:02d}_{_slug(ad.get('advertiser'))}",
        )
        sequence = [
            ("before", _closest(lead_in, first["t"] - BOUNDARY_SECONDS)),
            ("start", first),
            ("mid", shown[len(shown) // 2]),
            ("end", last),
            ("after", _closest(lead_out, last["t"] + BOUNDARY_SECONDS)),
        ]
        for position, (label, frame) in enumerate(sequence):
            if frame is None:
                continue
            target = f"{stem}_{position}_{label}.png"
            try:
                shutil.copyfile(frame["path"], target)
            except OSError as exc:
                logger.debug("could not save %s frame: %s", label, exc)
                continue
            ad[f"frame_{label}"] = target
            if label == "start":
                ad["thumbnail"] = target

        ad["clip"] = None
        ad["frames"] = len(frames)
        ad["ad_frames"] = len(shown)
        ad["ad_started_at"] = round(first["t"], 1)
        ad["ad_ended_at"] = round(last["t"], 1)
        ad["before_frame_s"] = round(first["t"] - lead_in[-1]["t"], 1) if lead_in else None
        ad["after_frame_s"] = round(lead_out[0]["t"] - last["t"], 1) if lead_out else None

        if _ffmpeg_available() and self.frames_dir:
            clip = stem + ".mp4"
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-framerate", "1",
                "-start_number", str(_frame_number(frames[0]["path"])),
                "-i", os.path.join(self.frames_dir, "frame_%05d.png"),
            ]
            crop = _crop_filter(self.rect)
            if crop:
                cmd += ["-vf", crop]
            cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264", clip]
            try:
                done = subprocess.run(cmd, capture_output=True, text=True)
                if done.returncode == 0 and os.path.exists(clip):
                    ad["clip"] = clip
                else:
                    logger.debug("ffmpeg clip failed: %s", (done.stderr or "")[-300:])
            except Exception as exc:
                logger.debug("ffmpeg clip failed: %s", exc)


def _closest(frames: list[dict], target: float) -> dict | None:
    """The frame nearest ``target`` seconds on the session clock."""
    if not frames:
        return None
    return min(frames, key=lambda frame: abs(frame["t"] - target))


def _frame_number(path: str) -> int:
    match = re.search(r"(\d+)", os.path.basename(path or ""))
    return int(match.group(1)) if match else 1


def _crop_filter(rect) -> str | None:
    """Crop the recording to the player element, rounded to even dimensions."""
    if not rect:
        return None
    try:
        w = int(float(rect.get("w") or 0)) // 2 * 2
        h = int(float(rect.get("h") or 0)) // 2 * 2
        x = max(0, int(float(rect.get("x") or 0)))
        y = max(0, int(float(rect.get("y") or 0)))
    except (TypeError, ValueError):
        return None
    if w < 120 or h < 80:
        return None
    return f"crop={w}:{h}:{x}:{y}"


# --------------------------------------------------------------------------- #
# Ad tracking
# --------------------------------------------------------------------------- #
def _clean(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _signature(snapshot: dict) -> tuple:
    """Identity of the ad currently playing, used to split an ad pod into ads."""
    return (
        _clean(snapshot.get("advertiser")),
        _clean(snapshot.get("destination")),
        _clean(snapshot.get("cta")),
        snapshot.get("ad_index"),
    )


def _should_split(current: dict, new_signature: tuple, trigger: str) -> bool:
    """Decide whether a changed overlay means a *new* ad or the same one.

    YouTube renders the ad card a beat after the ad starts, so the first poll of
    an ad often has no advertiser text. Treating that as a change would split one
    ad into two near-identical records.
    """
    if current.get("_trigger") != trigger:
        return True  # a different break entirely

    old = current["_signature"]
    if old[3] is not None and new_signature[3] is not None:
        return old[3] != new_signature[3]  # pod position is authoritative
    if old[0] is None and new_signature[0] is not None:
        return False  # the overlay text simply finished loading
    return old != new_signature


def _is_next_pod_member(previous: float | None, current: float | None) -> bool:
    """True when the ad's own playhead jumped back towards the start.

    That is the clearest sign YouTube moved on to the next spot in an ad pod, and
    it is the only one that works when both spots carry the same advertiser card
    (so the signature never changes). A playhead that is merely *stuttering*, or
    that reads 0 while the ad media is still loading, must not split an ad, so
    only a real drop counts.
    """
    if previous is None or current is None:
        return False
    return (
        previous - current > POD_RESTART_SECONDS
        and current <= POD_RESTART_TOLERANCE
    )


class _Tracker:
    """Turns a stream of player snapshots into discrete served-ad records."""

    def __init__(self, recorder: _ClipRecorder | None = None, driver=None):
        self.ads: list[dict] = []
        self.current: dict | None = None
        self.last_content_time = 0.0
        self.recorder = recorder
        self.driver = driver
        self.trigger = "playback"

    def update(self, snap: dict, now: float) -> None:
        showing = bool(snap.get("showing"))
        if showing:
            if self.current is None:
                self._start(snap, now)
            elif _should_split(self.current, _signature(snap), self.trigger):
                self._finish(now)
                self._start(snap, now)

            ad_time = None if snap.get("ad_time") is None else float(snap["ad_time"])
            if ad_time is not None and _is_next_pod_member(
                self.current["_last_ad_time"], ad_time
            ):
                # Same advertiser card, but the spot's playhead restarted: this is
                # the next ad of the pod, not a longer first one.
                logger.info("Pod advanced to the next spot (playhead %.1fs -> %.1fs)",
                            self.current["_last_ad_time"], ad_time)
                self._finish(now)
                self._start(snap, now)

            cur = self.current
            for key in ("advertiser", "destination", "cta", "badge"):
                value = _clean(snap.get(key))
                if value:
                    cur[key] = value
            if snap.get("ad_pod_size"):
                cur["ad_pod_size"] = snap["ad_pod_size"]
                cur["ad_index"] = snap.get("ad_index") or cur["ad_index"]
            if snap.get("content_duration"):
                cur["content_duration"] = round(float(snap["content_duration"]), 1)
            if snap.get("ad_duration"):
                cur["duration"] = max(cur["duration"], float(snap["ad_duration"]))
                cur["duration_from_player"] = True
            if snap.get("skippable") and not cur["skippable"]:
                cur["skippable"] = True
                cur["skip_after_s"] = round(now - cur["_started"], 1)
            if ad_time is not None:
                if cur["first_seen_media_time"] is None:
                    # The player's playhead the first time it admitted an ad was
                    # on screen. It is usually how far into the ad we already
                    # are -- the honest measure of how late detection is -- but
                    # see _detection_latency for when it cannot be read that way.
                    cur["first_seen_media_time"] = round(ad_time, 1)
                cur["_last_ad_time"] = ad_time
        else:
            if self.current is not None:
                self._finish(now)
            if snap.get("content_time") is not None:
                self.last_content_time = float(snap["content_time"])

        # Film on every poll -- including the quiet stretches either side of an
        # ad -- so the proof frames capture the moment the ad appears and goes.
        if self.recorder is not None:
            self.recorder.tick(self.driver, now, showing, snap.get("player_rect"))
            self.recorder.flush_ready(now)

    def freeze(self) -> None:
        """The playhead is about to jump, so stop filming the current context."""
        if self.recorder is not None:
            self.recorder.freeze()

    def checkpoint(self, driver, now: float) -> None:
        """Photograph the content just before the playhead moves.

        Skipped while an ad is on screen, because then the frame would show that
        ad rather than the content leading into the next one.
        """
        if self.recorder is not None and self.current is None:
            self.recorder.checkpoint(driver, now)

    def _start(self, snap: dict, now: float) -> None:
        self.current = {
            "_signature": _signature(snap),
            "_started": now,
            "_content_time": self.last_content_time,
            "_trigger": self.trigger,
            "_wall_started": datetime.now().isoformat(timespec="seconds"),
            "_last_ad_time": None if snap.get("ad_time") is None else float(snap["ad_time"]),
            "advertiser": None,
            "destination": None,
            "cta": None,
            "badge": _clean(snap.get("badge")),
            "ad_index": snap.get("ad_index"),
            "ad_pod_size": snap.get("ad_pod_size"),
            "duration": float(snap.get("ad_duration") or 0.0),
            "duration_from_player": bool(snap.get("ad_duration")),
            "content_duration": snap.get("content_duration"),
            "skippable": False,
            "skip_after_s": None,
            "first_seen_media_time": None,
            "trigger": self.trigger,
        }
        if self.recorder:
            self.recorder.begin(snap.get("player_rect"), now)
        logger.info("Served ad started (%s, trigger=%s)", self.current["_signature"], self.trigger)

    def _finish(self, now: float) -> None:
        cur = self.current
        if cur is None:
            return
        self.current = None
        ad = {k: v for k, v in cur.items() if not k.startswith("_")}
        started_elapsed = cur["_started"]
        content_time = float(cur.get("_content_time") or 0.0)
        trigger = cur.get("_trigger") or ""
        if trigger.startswith("seek@"):
            # The player sits still while an ad plays, so the seek target is the
            # better record of which break this ad belongs to.
            try:
                content_time = float(trigger.split("@", 1)[1])
            except (TypeError, ValueError):
                pass
        ad["elapsed_s"] = round(started_elapsed, 1)
        ad["wall_started_at"] = cur["_wall_started"]
        ad["content_position"] = round(content_time, 1)
        ad["observed_seconds"] = round(now - started_elapsed, 1)
        ad["duration"] = round(float(ad.get("duration") or ad["observed_seconds"]), 1)
        ad["placement"] = _classify(ad)
        ad["format"] = _classify_format(ad)
        ad["detection_latency_s"] = _detection_latency(ad)
        ad["summary"] = _summarize(ad)
        logger.info("Served ad ended (%s, %ss observed)",
                    ad.get("advertiser") or "unknown", ad["observed_seconds"])
        self.ads.append(ad)
        if self.recorder:
            # Clip paths and frame timings are attached once the post-roll
            # frames have been filmed, so the record is patched in place.
            self.recorder.finish(ad, len(self.ads), now)

    def close(self, now: float) -> None:
        if self.current is not None:
            self._finish(now)
        if self.recorder:
            self.recorder.close(now)


def _classify(ad: dict) -> str:
    trigger = ad.get("trigger") or ""
    if trigger.startswith("seek@"):
        return "mid-roll"
    if ad.get("content_position", 0.0) <= 1.0:
        return "pre-roll"
    return "mid-roll"


def _classify_format(ad: dict) -> str:
    """Which YouTube ad format the player was showing.

    Read only from what YouTube itself displayed, in the same order a viewer
    would judge it: a skip button means a *skippable* in-stream ad, a length of
    a few seconds means a *bumper*, and everything else is an ordinary
    non-skippable in-stream spot. A short ad only counts as a bumper when the
    player reported its length; a spot that merely looked short because the
    session moved on is not guessed at.
    """
    claimed = float(ad.get("duration") or 0.0)
    if ad.get("skippable"):
        return "skippable in-stream"
    if ad.get("duration_from_player") and claimed and claimed <= BUMPER_SECONDS:
        return "bumper"
    return "non-skippable in-stream"


def _detection_latency(ad: dict) -> float | None:
    """How far into the ad we already were on the first poll that saw it.

    That reading comes from the player's playhead, which is only meaningful while
    the ad's own media is loaded into the video element. A break served by a seek
    can be spotted before the ad media takes over, and then the playhead still
    reports the *content* position -- so a reading at or beyond the ad's own
    length is not a latency and is dropped rather than reported as one.
    """
    raw = ad.get("first_seen_media_time")
    if raw is None:
        return None
    raw = float(raw)
    claimed = float(ad.get("duration") or 0.0)
    if raw < 0 or (claimed and raw >= claimed):
        return None
    # A break served by a seek is spotted while the playhead still reads the
    # content position it just jumped to (the ad media has not taken over yet).
    # That number is the break's position, not a latency.
    position = ad.get("content_position")
    if position is not None and ad.get("placement") != "pre-roll":
        if abs(raw - float(position)) <= 1.5:
            return None
    return round(raw, 1)


def _summarize(ad: dict) -> str:
    who = ad.get("advertiser") or "unknown advertiser"
    parts = [who]
    if ad.get("destination"):
        parts.append(f"-> {ad['destination']}")
    if ad.get("cta"):
        parts.append(f"({ad['cta']})")
    if ad.get("format"):
        parts.append(f"[{ad['format']}]")

    # The player reports the whole ad pod's length; what we actually watched is
    # often shorter because the sweep moves on. Say both when they disagree.
    claimed = float(ad.get("duration") or 0.0)
    watched = float(ad.get("observed_seconds") or 0.0)
    if watched and claimed and abs(claimed - watched) > 5:
        parts.append(f"{watched:.0f}s seen (ad claims {claimed:.0f}s)")
    elif claimed:
        parts.append(f"{claimed:.0f}s")

    if ad.get("ad_index") and ad.get("ad_pod_size"):
        parts.append(f"ad {ad['ad_index']} of {ad['ad_pod_size']}")
    elif ad.get("ad_pod_size"):
        parts.append(f"in a pod of {ad['ad_pod_size']}")

    if ad.get("skippable"):
        skip = ad.get("skip_after_s")
        parts.append(f"skippable after {skip}s" if skip else "skippable")

    # How late the ad was spotted: on the first poll that showed an ad the player
    # was already this far into the ad itself.
    latency = ad.get("detection_latency_s")
    if latency is not None:
        parts.append(f"noticed {float(latency):.1f}s into the ad")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Watch strategies
# --------------------------------------------------------------------------- #
def _warmup(driver, tracker: _Tracker, deadline: float, started: float,
            cancel_check=None, extract_js: str | None = None) -> None:
    """Film the opening moments, before any watch window starts.

    The pre-roll usually starts while the watch page is still settling, so those
    first frames are the only record of the content the ad interrupted -- sleeping
    through them leaves every pre-roll without its "just before" evidence. A
    consent dialog that renders late would block playback entirely, so it is
    dismissed again for as long as the player has not appeared.
    """
    until = min(deadline, time.time() + WARMUP_SECONDS)
    while time.time() < until and not _cancelled(cancel_check):
        snap = _poll(driver, extract_js)
        if snap is not None:
            if snap.get("player_rect") is None and not snap.get("showing"):
                _dismiss_consent(driver)
            tracker.update(snap, time.time() - started)
        time.sleep(1.0)


def _linear_watch(driver, tracker: _Tracker, deadline: float, started: float,
                  cancel_check=None, extract_js: str | None = None) -> None:
    """Poll every second until the deadline (used when a budget is given)."""
    while time.time() < deadline and not _cancelled(cancel_check):
        snap = _poll(driver, extract_js)
        if snap is not None:
            tracker.update(snap, time.time() - started)
        time.sleep(1.0)


def _full_watch(driver, tracker: _Tracker, deadline: float, started: float,
                cancel_check=None, extract_js: str | None = None) -> None:
    """Play right through the video, stopping when it ends."""
    stalled = 0
    last = -1.0
    while time.time() < deadline and not _cancelled(cancel_check):
        snap = _poll(driver, extract_js)
        if snap is not None:
            tracker.update(snap, time.time() - started)
            if not snap.get("showing"):
                pos = snap.get("content_time") or 0.0
                duration = snap.get("content_duration") or 0.0
                if snap.get("ended") or (duration and pos >= duration - 1.5):
                    break
                # Nudge playback if it stalled outside of an ad.
                stalled = stalled + 1 if abs(pos - last) < 0.05 else 0
                last = pos
                if stalled >= 3:
                    _play(driver)
                    stalled = 0
        time.sleep(1.0)


def _watch_window(driver, tracker: _Tracker, deadline: float, started: float,
                  seconds: float, cancel_check=None,
                  extract_js: str | None = None) -> float | None:
    """Poll for ``seconds`` -- but never walk away from an ad that is playing.

    Leaving mid-ad would cut the ad out of its own proof clip and, worse, hide the
    return to content, which is half of what the recording exists to show. So the
    window stretches while an ad is on screen (capped by ``AD_WAIT_SECONDS``) and
    closes a moment after the content comes back. Returns the content duration if
    the player reported one. A cancellation still stops even mid-ad: the user's
    stop button outranks the proof clip.
    """
    duration = None
    stop_until = min(deadline, time.time() + seconds)
    ad_deadline = None
    waiting_on_ad = False
    while time.time() < stop_until and not _cancelled(cancel_check):
        snap = _poll(driver, extract_js)
        if snap is not None:
            tracker.update(snap, time.time() - started)
            if snap.get("content_duration"):
                duration = float(snap["content_duration"])
            if snap.get("showing"):
                if not waiting_on_ad:
                    # An absolute cap, fixed when the break starts: an ad that
                    # never reports itself finished must not hold us here for the
                    # whole budget.
                    ad_deadline = time.time() + AD_WAIT_SECONDS
                    logger.info("holding the watch window open for a served ad")
                waiting_on_ad = True
                stop_until = min(deadline, ad_deadline)
                if snap.get("paused"):
                    _play(driver)
            elif waiting_on_ad:
                waiting_on_ad = False
                stop_until = min(deadline, time.time() + POST_ROLL_SECONDS + 1)
        time.sleep(1.0)
    return duration


def _sweep_watch(driver, tracker: _Tracker, deadline: float, started: float,
                 cancel_check=None, extract_js: str | None = None) -> dict:
    """Watch the opening, then seek along the timeline to trigger mid-rolls."""
    info = {"seek_stops": 0, "content_duration": None}
    info["content_duration"] = _watch_window(
        driver, tracker, deadline, started, PRE_ROLL_SECONDS,
        cancel_check=cancel_check, extract_js=extract_js,
    )

    duration = info["content_duration"]
    if not duration:
        return info

    for position in _sweep_positions(duration, SEEK_STEP_SECONDS):
        if time.time() >= deadline or _cancelled(cancel_check):
            break
        tracker.trigger = f"seek@{int(position)}"
        tracker.freeze()  # post-roll frames from the old position are not evidence
        tracker.checkpoint(driver, time.time() - started)
        _seek(driver, position)
        info["seek_stops"] += 1
        _watch_window(driver, tracker, deadline, started, SEEK_DWELL_SECONDS,
                      cancel_check=cancel_check, extract_js=extract_js)
        tracker.trigger = "playback"
    return info


def _sweep_positions(duration: float, step: float) -> list[float]:
    """Timeline positions to park at while sweeping a video of ``duration``.

    Evenly stepped, plus a final stop near the end so the tail of the video is
    covered rather than skipped when the runtime is not a multiple of ``step``.
    """
    positions: list[float] = []
    position = step
    while position < duration - 5:
        positions.append(round(position, 1))
        position += step

    tail = round(max(0.0, duration - 20), 1)
    if tail > 5 and (not positions or tail - positions[-1] > step * 0.4):
        positions.append(tail)
    return positions


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def _cancelled(cancel_check) -> bool:
    """True when the caller asked this capture to stop."""
    try:
        return bool(cancel_check()) if cancel_check else False
    except Exception:
        return False


def _load_page(driver, url: str) -> None:
    """Open ``url``, tolerating one renderer hang with a reload.

    Heavy pages (Twitch especially) can outlast the load timeout while still
    being usable — the player keeps initializing — so a timeout is retried once
    and only a second failure propagates to the caller's error note.
    """
    from selenium.common.exceptions import TimeoutException

    try:
        driver.get(url)
    except TimeoutException:
        logger.warning("page load timed out for %s; retrying once", url)
        try:
            driver.get(url)
        except TimeoutException:
            pass


def capture_vod_sweep(url: str, watch_seconds: float | None = None,
                      headless: bool | None = None,
                      proof_dir: str | None = None,
                      record: bool | None = None,
                      max_seconds: float | None = None,
                      video_id: str | None = None,
                      cancel_check=None,
                      extract_js: str | None = None) -> dict:
    """Sweep a **VOD** (e.g. a Twitch archive) for served ads in a browser.

    A VOD player is served ads the same way YouTube's is, so this reuses the
    whole YouTube machinery — tracker, recorder, seek sweep, proof writers —
    with a different overlay-extraction script, and relabels the report so it
    stays attributable (``source: vod-browser``, ``strategy: vod-sweep``).
    """
    # ``watch_seconds`` must become the sweep BUDGET, not a window: a window
    # would make the inner reader park and watch linearly instead of seeking
    # across the timeline, which is the whole point of a VOD sweep.
    budget = watch_seconds if watch_seconds is not None else max_seconds
    report = _capture_youtube_like(
        url, watch_seconds=None, headless=headless,
        proof_dir=proof_dir, record=record, full_watch=False,
        max_seconds=budget, video_id=video_id,
        cancel_check=cancel_check, extract_js=extract_js,
    )
    report["source"] = "vod-browser"
    report["strategy"] = "vod-sweep"
    if report.get("captured"):
        report["note"] = (
            f"Swept the VOD's timeline and captured {report['ad_count']} served "
            "ad(s) the player injected at the seek stops."
        )
    elif not report.get("note", "").startswith(("Live ad capture", "Capture was")):
        report["note"] = (
            "Swept the VOD's timeline in Chrome and the player served no ad in "
            "this session. VOD ads are personalized per viewer and run, so a "
            "repeat sweep can differ."
        )
    return report


def capture_served_ads(url: str, watch_seconds: float | None = None,
                       headless: bool | None = None,
                       proof_dir: str | None = None,
                       record: bool | None = None,
                       full_watch: bool | None = None,
                       max_seconds: float | None = None,
                       video_id: str | None = None,
                       cancel_check=None) -> dict:
    """Watch ``url`` and record the ads the platform actually served.

    YouTube links are watched in Chrome (the strategies below). Twitch **live**
    links go to ``twitch_ads`` (playlist markers, no browser); Twitch **VOD**
    links are swept in a browser like YouTube, because the recorded player is
    served ads the same way. Reports share one shape either way.

    Watching strategies (browser readers):

    * ``watch_seconds`` given -- poll for that many seconds (a plain window).
    * ``full_watch=True`` -- play the whole video in real time (exact, slow).
    * otherwise -- sweep: pre-roll window plus seek jumps across the timeline.

    Returns a report dict; never raises. ``captured`` is True only when at least
    one ad was observed, so "watched but got no ads" stays distinct from "could
    not watch at all".
    """
    if detect_platform(url) == "twitch":
        import twitch_ads

        # A VOD is a recording with a seekable timeline and a player that is
        # served ads exactly like YouTube's — so sweep it in a browser. Only
        # live channels go to the playlist-marker reader.
        target = twitch_ads.twitch_target(url)
        if target and target[0] == "vod":
            return capture_vod_sweep(
                url, watch_seconds=watch_seconds, headless=headless,
                proof_dir=proof_dir, record=record, max_seconds=max_seconds,
                video_id=video_id or target[1], cancel_check=cancel_check,
                extract_js=_TWITCH_EXTRACT_JS,
            )

        return twitch_ads.capture_twitch_ads(
            url,
            watch_seconds=watch_seconds,
            headless=headless,
            proof_dir=proof_dir,
            record=record,
            full_watch=full_watch,
            max_seconds=max_seconds,
            video_id=video_id,
            cancel_check=cancel_check,
        )

    return _capture_youtube_like(
        url, watch_seconds=watch_seconds, headless=headless,
        proof_dir=proof_dir, record=record, full_watch=full_watch,
        max_seconds=max_seconds, video_id=video_id,
        cancel_check=cancel_check,
    )


def _capture_youtube_like(url: str, watch_seconds: float | None = None,
                          headless: bool | None = None,
                          proof_dir: str | None = None,
                          record: bool | None = None,
                          full_watch: bool | None = None,
                          max_seconds: float | None = None,
                          video_id: str | None = None,
                          cancel_check=None,
                          extract_js: str | None = None) -> dict:
    """The browser reader: watch ``url`` in Chrome and record served ads.

    Used directly for YouTube and (via :func:`capture_vod_sweep`) for Twitch
    VODs, whose players both serve ads over the video while it plays.
    """
    result = {
        "available": False,
        "captured": False,
        "ads": [],
        "ad_count": 0,
        "ad_seconds": 0.0,
        "watched_seconds": 0.0,
        "strategy": "none",
        "proof_dir": None,
        "manifest": None,
        "note": "",
        "source": "live-browser",
    }

    ok, reason = availability()
    if not ok:
        result["note"] = (
            f"Live ad capture unavailable: {reason}. Install selenium and Google "
            "Chrome to record the ads YouTube serves while watching."
        )
        return result

    result["available"] = True
    headless = HEADLESS if headless is None else headless
    if record is None:
        record = RECORD
    if full_watch is None:
        full_watch = FULL_WATCH
    budget = float(max_seconds if max_seconds is not None else MAX_SECONDS)

    if watch_seconds is not None:
        strategy = "window"
        budget = min(budget, float(watch_seconds))
    elif full_watch:
        strategy = "full"
    else:
        strategy = "sweep"
    result["strategy"] = strategy

    recorder = None
    if record:
        recorder = _ClipRecorder(proof_dir or DEFAULT_PROOF_DIR, video_id)
        result["proof_dir"] = recorder.proof_dir

    driver = None
    try:
        driver = _new_driver(headless)
        driver.set_page_load_timeout(45)
        logger.info("Serving ads: opening %s (strategy=%s, budget=%ss)",
                    url, strategy, int(budget))
        _load_page(driver, url)

        # The clock starts with the page, not with playback: the pre-roll can
        # begin during the load, and those first seconds are evidence.
        started = time.time()
        deadline = started + budget
        tracker = _Tracker(recorder=recorder, driver=driver)
        _warmup(driver, tracker, deadline, started,
                cancel_check=cancel_check, extract_js=extract_js)
        _play(driver)

        extra: dict = {}
        if strategy == "window":
            _linear_watch(driver, tracker, deadline, started,
                          cancel_check=cancel_check, extract_js=extract_js)
        elif strategy == "full":
            _full_watch(driver, tracker, deadline, started,
                        cancel_check=cancel_check, extract_js=extract_js)
        else:
            extra = _sweep_watch(driver, tracker, deadline, started,
                                 cancel_check=cancel_check, extract_js=extract_js)
        cancelled = _cancelled(cancel_check)
        tracker.close(time.time() - started)

        ads = tracker.ads
        result["watched_seconds"] = round(time.time() - started, 1)
        result["ads"] = ads
        result["ad_count"] = len(ads)
        result["ad_seconds"] = round(sum(a["duration"] for a in ads), 1)
        result["captured"] = bool(ads)
        result.update({k: v for k, v in extra.items() if v is not None})
        result["manifest"] = _write_manifest(
            recorder, url, video_id, ads, result, strategy
        )
        result["index"] = _write_index(recorder, url, ads)

        if cancelled:
            result["cancelled"] = True
        if ads:
            names = ", ".join(sorted({(a.get("advertiser") or "unknown") for a in ads}))
            note = (
                f"Captured {len(ads)} served ad(s) during a "
                f"{int(result['watched_seconds'])}s "
                f"{'sweep' if strategy == 'sweep' else strategy} session: {names}."
            )
            if strategy == "sweep":
                note += (
                    f" Pre-roll positions are exact; mid-roll positions are accurate "
                    f"to within the {int(SEEK_STEP_SECONDS)}s seek step."
                )
            if cancelled:
                note += " Capture was stopped early at the user's request."
            result["note"] = note
        elif cancelled:
            result["note"] = (
                f"Capture was stopped after {int(result['watched_seconds'])}s at "
                "the user's request before any ad was served."
            )
        else:
            result["note"] = (
                f"Chrome watched the video for {int(result['watched_seconds'])}s "
                "and YouTube served no ad in this session. Served ads are "
                "personalized and are not always shown."
            )
        return result

    except Exception as exc:
        logger.warning("Live ad capture failed: %s", exc)
        result["note"] = (
            f"Live ad capture failed: {exc}. This can happen if Chrome is busy "
            "or the page did not load."
        )
        return result
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def _write_manifest(recorder, url, video_id, ads, result, strategy) -> str | None:
    """Persist a JSON manifest describing the capture, for auditing later."""
    if recorder is None:
        return None
    path = os.path.join(recorder.proof_dir, f"{recorder.video_id}_manifest.json")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "url": url,
        "video_id": video_id,
        "strategy": strategy,
        "watched_seconds": result.get("watched_seconds"),
        "ad_count": len(ads),
        "ads": ads,
    }
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.debug("could not write manifest: %s", exc)
        return None
    return path


_LABELS = ("before", "start", "mid", "end", "after")


_TWITCH_FRAME_CAPTIONS = {
    "before": "content segment just before the ad",
    "start": "the ad's own first frame",
    "mid": "the ad's own middle frame",
    "end": "the ad's own last frame",
    "after": "content segment just after the ad",
}


def _label_caption(label: str, ad: dict) -> str:
    """Caption a boundary frame with how far it sits from the ad's edges."""
    if ad.get("frames_source") == "stream-segments":
        # Twitch frames are decoded out of the segments the ad itself was
        # stitched from, so they mark a point *in* the ad rather than a delay in
        # noticing it.
        return _TWITCH_FRAME_CAPTIONS.get(label, label)
    if label == "before":
        delta = ad.get("before_frame_s")
        return "content just before" if delta is None else f"content {delta}s before"
    if label == "start":
        latency = ad.get("detection_latency_s")
        seen = "unknown" if latency is None else f"{float(latency):.1f}s"
        return f"first ad frame detected ({seen} into the ad)"
    if label == "end":
        return "last ad frame"
    if label == "after":
        delta = ad.get("after_frame_s")
        return "content after" if delta is None else f"content {delta}s after"
    return "mid-ad"


def _frame_strip(ad: dict) -> str:
    """The before / start / mid / end / after frames for one ad, in order."""
    cells = []
    for label in _LABELS:
        name = ad.get(f"frame_{label}")
        if not name:
            continue
        cells.append(
            f'<figure><img src="{os.path.basename(name)}" alt="{label} ad frame">'
            f"<figcaption><b>{label}</b> &middot; {_label_caption(label, ad)}</figcaption></figure>"
        )
    return f'<div class="strip">{"".join(cells)}</div>' if cells else ""


def _write_index(recorder, url: str, ads) -> str | None:
    """Write a browsable ``index.html`` so the proof clips can be reviewed."""
    if recorder is None or not ads:
        return None
    rows = []
    for ad in ads:
        clip = ad.get("clip")
        thumb = ad.get("thumbnail")
        media = ""
        if clip:
            media = (f'<video src="{os.path.basename(clip)}" controls '
                     f'poster="{os.path.basename(thumb) if thumb else ""}"></video>')
        elif thumb:
            media = f'<img src="{os.path.basename(thumb)}" alt="ad frame">'
        rows.append(
            "<li>"
            f'<div class="when">{ad.get("placement")} at {format_timeline(ad)} '
            f'&middot; {ad.get("wall_started_at")}</div>'
            f"<div class=\"meta\">{ad.get('summary', '')}</div>"
            f"{_frame_strip(ad)}"
            f"{media}</li>"
        )
    html = (
        "<!DOCTYPE html><meta charset='utf-8'>"
        f"<title>Ad proof - {recorder.video_id}</title>"
        "<style>body{font:14px system-ui;margin:0;padding:20px;background:#0f1115;color:#e8eaf0}"
        "li{list-style:none;margin:0 0 24px;padding:14px;background:#171a21;border-radius:10px}"
        ".when{font-weight:600} .meta{color:#9aa3b2;margin:4px 0 10px}"
        "video,img{width:100%;max-width:900px;border-radius:6px;display:block}"
        ".strip{display:flex;gap:8px;overflow-x:auto;margin:0 0 12px}"
        ".strip figure{margin:0;flex:0 0 220px}"
        ".strip img{border-radius:4px;border:1px solid #2a2f3a}"
        ".strip figcaption{color:#9aa3b2;font-size:12px;margin-top:4px}"
        ".strip b{color:#e8eaf0}</style>"
        f"<h1>Served ads for {recorder.video_id}</h1>"
        f"<p class='meta'>Source: {url}</p>"
        + (
            f"<p class='meta'><a href='{CREATIVES_PAGE}'>Every Twitch creative seen "
            "so far &rarr;</a></p>"
            if os.path.exists(os.path.join(recorder.proof_dir, CREATIVES_PAGE))
            else ""
        )
        + "<ul>" + "".join(rows) + "</ul>"
    )
    path = os.path.join(recorder.proof_dir, "index.html")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(html)
    except OSError as exc:
        logger.debug("could not write proof index: %s", exc)
        return None
    return path


def format_timeline(ad) -> str:
    """Human-readable position on the video timeline.

    Accepts either an ad dict (reads ``content_position``) or a raw number of
    seconds, so the report timeline can reuse the same clock formatting. Hours
    appear only when there are any, which keeps short videos reading as ``5:12``
    while still describing a long Twitch broadcast honestly.
    """
    if isinstance(ad, dict):
        position = ad.get("content_position")
    else:
        position = ad
    if position is None:
        return ""
    try:
        position = float(position)
    except (TypeError, ValueError):
        return ""
    hours, rest = divmod(int(position), 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_served_ads(ads) -> str:
    """One-line summary of served ads for the Excel report."""
    if not ads:
        return ""
    parts = []
    for ad in ads:
        where = format_timeline(ad)
        label = f"{ad.get('placement', 'ad')}"
        if where:
            label += f" @ {where}"
        parts.append(f"{label}: {ad.get('summary', '')}".strip())
    return "; ".join(parts)
