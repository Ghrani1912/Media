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

Covering the whole video
------------------------
Watching a 30-minute video in real time is slow, so the default strategy is a
**seek sweep**: watch the opening for pre-rolls, then jump along the timeline
(``SEEK_STEP`` seconds at a time). Seeking past a mid-roll break makes the player
serve that break's ad, so a few minutes of sweeping covers the full runtime. Pass
``full_watch=True`` to play straight through instead, which is exact but slow.

Proof clips
-----------
With ``proof_dir`` set, every captured ad is screenshot once a second while it
plays. The frames are stitched into a short mp4 (and a thumbnail), so the ad can
be watched back and judged. A ``manifest.json`` records the metadata, the video
timeline position, and the wall-clock time of each ad.

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

  const adsText = t('.video-ads') || '';
  const pod = adsText.match(/(\d+)\s*of\s*(\d+)/);

  // The skip button stays in the DOM between ads, so require it to be visible.
  let skippable = false;
  try {
    const skipEl = q('.ytp-skip-ad-button') || q('.ytp-ad-skip-button-modern');
    if (skipEl) { const r = skipEl.getBoundingClientRect(); skippable = r.width > 0 && r.height > 0; }
  } catch (e) {}

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
    ad_duration: adDuration,
    skippable: skippable,
    skip_text: t('.ytp-skip-ad-button__text'),
    content_time: contentTime,
    content_duration: contentDuration,
    ended: ended,
    paused: paused,
    player_rect: rect
  });
"""

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
    from selenium import webdriver

    driver = webdriver.Chrome(options=_build_options(headless))
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
        )
    except Exception:
        pass
    return driver


def _poll(driver) -> dict | None:
    """Read one snapshot of the player's ad state, or None if it can't be read."""
    try:
        raw = driver.execute_script(_EXTRACT_JS)
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


class _ClipRecorder:
    """Screenshots an ad while it plays and stitches the frames into an mp4."""

    def __init__(self, proof_dir: str, video_id: str | None, enabled: bool = True):
        self.proof_dir = proof_dir
        self.video_id = video_id or "video"
        self.enabled = enabled
        self.frames_dir: str | None = None
        self.frames: list[str] = []
        self.rect: dict | None = None
        os.makedirs(self.proof_dir, exist_ok=True)

    def begin(self, rect) -> None:
        if not self.enabled:
            return
        self.rect = rect or None
        self.frames_dir = os.path.join(
            tempfile.gettempdir(), "media_analyzer", f"adframes_{uuid.uuid4().hex[:8]}"
        )
        os.makedirs(self.frames_dir, exist_ok=True)
        self.frames = []

    def capture(self, driver) -> None:
        if not self.enabled or not self.frames_dir:
            return
        path = os.path.join(self.frames_dir, f"frame_{len(self.frames):04d}.png")
        try:
            driver.save_screenshot(path)
            self.frames.append(path)
        except Exception as exc:
            logger.debug("ad screenshot failed: %s", exc)

    def finish(self, ad: dict, index: int) -> dict:
        """Return {clip, thumbnail, frames} paths for a finished ad."""
        result = {"clip": None, "thumbnail": None, "frames": 0}
        if not self.enabled or not self.frames:
            self._cleanup()
            return result

        result["frames"] = len(self.frames)
        stem = os.path.join(
            self.proof_dir,
            f"{self.video_id}_ad{index:02d}_{_slug(ad.get('advertiser'))}",
        )
        try:
            shutil.copyfile(self.frames[0], stem + "_thumb.png")
            result["thumbnail"] = stem + "_thumb.png"
        except OSError:
            pass

        if len(self.frames) >= 2 and _ffmpeg_available():
            clip = stem + ".mp4"
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-framerate", "1", "-i", os.path.join(self.frames_dir, "frame_%04d.png"),
            ]
            crop = _crop_filter(self.rect)
            if crop:
                cmd += ["-vf", crop]
            cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264", clip]
            try:
                done = subprocess.run(cmd, capture_output=True, text=True)
                if done.returncode == 0 and os.path.exists(clip):
                    result["clip"] = clip
                else:
                    logger.debug("ffmpeg clip failed: %s", (done.stderr or "")[-300:])
            except Exception as exc:
                logger.debug("ffmpeg clip failed: %s", exc)

        self._cleanup()
        return result

    def _cleanup(self) -> None:
        if self.frames_dir:
            shutil.rmtree(self.frames_dir, ignore_errors=True)
        self.frames_dir = None
        self.frames = []


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
        if snap.get("showing"):
            if self.current is None:
                self._start(snap, now)
            elif _should_split(self.current, _signature(snap), self.trigger):
                self._finish(now)
                self._start(snap, now)

            cur = self.current
            for key in ("advertiser", "destination", "cta"):
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
            if snap.get("skippable") and not cur["skippable"]:
                cur["skippable"] = True
                cur["skip_after_s"] = round(now - cur["_started"], 1)
            if self.recorder:
                self.recorder.capture(self.driver)
        else:
            if self.current is not None:
                self._finish(now)
            if snap.get("content_time") is not None:
                self.last_content_time = float(snap["content_time"])

    def _start(self, snap: dict, now: float) -> None:
        self.current = {
            "_signature": _signature(snap),
            "_started": now,
            "_content_time": self.last_content_time,
            "_trigger": self.trigger,
            "_wall_started": datetime.now().isoformat(timespec="seconds"),
            "advertiser": None,
            "destination": None,
            "cta": None,
            "ad_index": snap.get("ad_index"),
            "ad_pod_size": snap.get("ad_pod_size"),
            "duration": float(snap.get("ad_duration") or 0.0),
            "content_duration": snap.get("content_duration"),
            "skippable": False,
            "skip_after_s": None,
            "trigger": self.trigger,
        }
        if self.recorder:
            self.recorder.begin(snap.get("player_rect"))
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
        if self.recorder:
            ad.update(self.recorder.finish(ad, len(self.ads) + 1))
        ad["summary"] = _summarize(ad)
        self.ads.append(ad)

    def close(self, now: float) -> None:
        if self.current is not None:
            self._finish(now)


def _classify(ad: dict) -> str:
    trigger = ad.get("trigger") or ""
    if trigger.startswith("seek@"):
        return "mid-roll"
    if ad.get("content_position", 0.0) <= 1.0:
        return "pre-roll"
    return "mid-roll"


def _summarize(ad: dict) -> str:
    who = ad.get("advertiser") or "unknown advertiser"
    parts = [who]
    if ad.get("destination"):
        parts.append(f"-> {ad['destination']}")
    if ad.get("cta"):
        parts.append(f"({ad['cta']})")

    # The player reports the whole ad pod's length; what we actually watched is
    # often shorter because the sweep moves on. Say both when they disagree.
    claimed = float(ad.get("duration") or 0.0)
    watched = float(ad.get("observed_seconds") or 0.0)
    if watched and claimed and abs(claimed - watched) > 5:
        parts.append(f"{watched:.0f}s seen (ad claims {claimed:.0f}s)")
    elif claimed:
        parts.append(f"{claimed:.0f}s")

    if ad.get("skippable"):
        skip = ad.get("skip_after_s")
        parts.append(f"skippable after {skip}s" if skip else "skippable")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Watch strategies
# --------------------------------------------------------------------------- #
def _linear_watch(driver, tracker: _Tracker, deadline: float, started: float) -> None:
    """Poll every second until the deadline (used when a budget is given)."""
    while time.time() < deadline:
        snap = _poll(driver)
        if snap is not None:
            tracker.update(snap, time.time() - started)
        time.sleep(1.0)


def _full_watch(driver, tracker: _Tracker, deadline: float, started: float) -> None:
    """Play right through the video, stopping when it ends."""
    stalled = 0
    last = -1.0
    while time.time() < deadline:
        snap = _poll(driver)
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
                    try:
                        driver.execute_script(
                            "const v=document.querySelector('video'); if (v && v.paused) v.play();"
                        )
                    except Exception:
                        pass
                    stalled = 0
        time.sleep(1.0)


def _sweep_watch(driver, tracker: _Tracker, deadline: float, started: float) -> dict:
    """Watch the opening, then seek along the timeline to trigger mid-rolls."""
    info = {"seek_stops": 0, "content_duration": None}

    pre_roll_deadline = min(deadline, started + PRE_ROLL_SECONDS)
    while time.time() < pre_roll_deadline:
        snap = _poll(driver)
        if snap is not None:
            tracker.update(snap, time.time() - started)
            if snap.get("content_duration"):
                info["content_duration"] = float(snap["content_duration"])
        time.sleep(1.0)

    duration = info["content_duration"]
    if not duration:
        return info

    for position in _sweep_positions(duration, SEEK_STEP_SECONDS):
        if time.time() >= deadline:
            break
        tracker.trigger = f"seek@{int(position)}"
        _seek(driver, position)
        info["seek_stops"] += 1
        stop_until = min(deadline, time.time() + SEEK_DWELL_SECONDS)
        while time.time() < stop_until:
            snap = _poll(driver)
            if snap is not None:
                tracker.update(snap, time.time() - started)
            time.sleep(1.0)
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
def capture_served_ads(url: str, watch_seconds: float | None = None,
                       headless: bool | None = None,
                       proof_dir: str | None = None,
                       record: bool | None = None,
                       full_watch: bool | None = None,
                       max_seconds: float | None = None,
                       video_id: str | None = None) -> dict:
    """Watch ``url`` in Chrome and record the ads YouTube actually plays.

    Watching strategies:

    * ``watch_seconds`` given -- poll for that many seconds (a plain window).
    * ``full_watch=True`` -- play the whole video in real time (exact, slow).
    * otherwise -- sweep: pre-roll window plus seek jumps across the timeline.

    Returns a report dict; never raises. ``captured`` is True only when at least
    one ad was observed, so "watched but got no ads" stays distinct from "could
    not watch at all".
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
        driver.get(url)
        time.sleep(4)
        _dismiss_consent(driver)
        try:
            driver.execute_script(
                "const v=document.querySelector('video'); if (v && v.paused) v.play();"
            )
        except Exception:
            pass

        started = time.time()
        deadline = started + budget
        tracker = _Tracker(recorder=recorder, driver=driver)

        extra: dict = {}
        if strategy == "window":
            _linear_watch(driver, tracker, deadline, started)
        elif strategy == "full":
            _full_watch(driver, tracker, deadline, started)
        else:
            extra = _sweep_watch(driver, tracker, deadline, started)
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
            result["note"] = note
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
            f"{media}</li>"
        )
    html = (
        "<!DOCTYPE html><meta charset='utf-8'>"
        f"<title>Ad proof - {recorder.video_id}</title>"
        "<style>body{font:14px system-ui;margin:0;padding:20px;background:#0f1115;color:#e8eaf0}"
        "li{list-style:none;margin:0 0 24px;padding:14px;background:#171a21;border-radius:10px}"
        ".when{font-weight:600} .meta{color:#9aa3b2;margin:4px 0 10px}"
        "video,img{width:100%;max-width:900px;border-radius:6px;display:block}</style>"
        f"<h1>Served ads for {recorder.video_id}</h1>"
        f"<p class='meta'>Source: {url}</p><ul>" + "".join(rows) + "</ul>"
    )
    path = os.path.join(recorder.proof_dir, "index.html")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(html)
    except OSError as exc:
        logger.debug("could not write proof index: %s", exc)
        return None
    return path


def format_timeline(ad: dict) -> str:
    """Human-readable position of an ad on the video timeline and wall clock."""
    position = ad.get("content_position")
    if position is None:
        return ""
    minutes, seconds = divmod(int(position), 60)
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
