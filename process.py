"""Media analysis pipeline.

Downloads (or accepts) a media file, extracts its audio, transcribes it with
OpenAI Whisper, runs a light sentiment/keyword analysis, and appends the result
to an Excel report.

This module intentionally avoids ``pytube`` (unmaintained and repeatedly broken
by YouTube changes) and ``moviepy`` (its ``moviepy.editor`` namespace was removed
in 2.x). Audio handling is delegated to ``yt-dlp`` and ``ffmpeg`` instead.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid

import whisper
import yt_dlp
from openpyxl import Workbook, load_workbook
from textblob import TextBlob

import ads
import served_ads

# Sentiment: a small transformer beats TextBlob's lexicon on casual speech and
# gaming slang (its training domain is tweets). TextBlob stays as the fallback
# when transformers/model download is unavailable. SENTIMENT_MODEL=off -> TextBlob.
SENTIMENT_MODEL_DEFAULT = "cardiffnlp/twitter-roberta-base-sentiment-latest"
SENTIMENT_MAX_CHARS = 256 * 4  # chunk transcripts longer than the model window

_sentiment_pipeline = None
_sentiment_pipeline_attempted = False


def _get_sentiment_pipeline():
    """Load the transformer sentiment model once (GPU when available)."""
    global _sentiment_pipeline, _sentiment_pipeline_attempted
    if _sentiment_pipeline_attempted:
        return _sentiment_pipeline
    _sentiment_pipeline_attempted = True
    name = os.environ.get("SENTIMENT_MODEL", SENTIMENT_MODEL_DEFAULT).strip()
    if not name or name.lower() == "off":
        return None
    try:
        from transformers import pipeline as hf_pipeline

        device = _resolve_device()
        _sentiment_pipeline = hf_pipeline(
            "sentiment-analysis", model=name,
            device=0 if device == "cuda" else -1,
            truncation=True,
        )
        logger.info("Sentiment model '%s' loaded on %s", name,
                    "GPU" if device == "cuda" else "CPU")
    except Exception as exc:
        logger.warning("Sentiment transformer unavailable (%s); using TextBlob.",
                       str(exc)[:140])
        _sentiment_pipeline = None
    return _sentiment_pipeline

logger = logging.getLogger(__name__)

# Directory used for intermediate downloads/extractions. Kept out of the repo.
WORK_DIR = os.path.join(tempfile.gettempdir(), "media_analyzer")

# Excel report produced by the pipeline.
REPORT_PATH = os.environ.get(
    "MEDIA_REPORT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_report.xlsx"),
)

REPORT_HEADERS = [
    "Platform", "Link/File", "Transcript", "Sentiment", "Keywords",
    "Ads Detected", "Ad Time (s)", "Ad Details",
    "Served Ads", "Served Ad Details",
]

# Served-ad capture drives a real browser, so it is opt-in. Enable it with
# CAPTURE_SERVED_ADS=1 (or the UI checkbox when analyzing a link).
CAPTURE_SERVED_ADS = os.environ.get("CAPTURE_SERVED_ADS", "").strip().lower() in {
    "1", "true", "yes"
}
# Optional fixed-length capture window. On YouTube this replaces the sweep with a
# plain watch of that many seconds; on Twitch it caps how long the live channel is
# watched for a break. Unset, each platform uses its own default (a full sweep for
# YouTube, SERVED_ADS_TWITCH_WATCH for Twitch).
SERVED_ADS_SECONDS = os.environ.get("SERVED_ADS_SECONDS")


def _served_window() -> float | None:
    """``SERVED_ADS_SECONDS`` as a number, or None when unset/unusable."""
    if not SERVED_ADS_SECONDS:
        return None
    try:
        seconds = float(SERVED_ADS_SECONDS)
    except ValueError:
        logger.warning("SERVED_ADS_SECONDS=%r is not a number; ignoring it",
                       SERVED_ADS_SECONDS)
        return None
    return seconds if seconds > 0 else None

# Audio extensions that need no ffmpeg conversion.
_READY_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}

# Transcript segments that sound like an ad read are highlighted in the UI.
_AD_TEXT_RE = re.compile(
    r"\b("
    r"sponsor(?:ed|ship)?\s+by"
    r"|thanks\s+to\s+(?:today'?s|this|our)"
    r"|brought\s+to\s+you\s+by"
    r"|promo\s*code|use\s+code|discount\s+code"
    r"|check\s+out|sign\s+up\s+at|link\s+in\s+the\s+description"
    r")\b",
    re.IGNORECASE,
)


def _is_ad_like_text(text: str) -> bool:
    """Heuristic: does this transcript segment read like a sponsor mention?"""
    return bool(_AD_TEXT_RE.search(text or ""))

# Whisper model used for transcription. "base" is small but noticeably wrong on
# music/noisy audio; "small" is a much better default. Override with WHISPER_MODEL.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")

# Decoder strategy. Greedy (1) is 2-3x faster than beam search for a marginal
# accuracy cost; set WHISPER_BEAM_SIZE=5 for the old behavior.
WHISPER_BEAM_SIZE_DEFAULT = int(os.environ.get("WHISPER_BEAM_SIZE", "1") or 1)
# Device for Whisper. WHISPER_DEVICE forces "cpu" or "cuda"; the default "auto"
# uses the GPU when torch sees CUDA, which is ~5-10x faster than CPU.
def _detect_device() -> str:
    forced = os.environ.get("WHISPER_DEVICE", "auto").strip().lower()
    if forced in {"cpu", "cuda"}:
        return forced
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # torch always present here, but stay defensive
        return "cpu"


WHISPER_DEVICE_FORCED = os.environ.get("WHISPER_DEVICE", "auto").strip().lower()


# fp16 halves GPU math with no quality loss; it does nothing on CPU. Default is
# automatic: on when running on CUDA, off on CPU. WHISPER_FP16=1/0 forces it.
_FP16_ENV = os.environ.get("WHISPER_FP16", "").strip().lower()
WHISPER_FP16_DEFAULT: bool | None = (
    _FP16_ENV in {"1", "true", "yes"} if _FP16_ENV else None
)  # None = decide from the device at load time

# Optional yt-dlp authentication, needed when YouTube asks to "confirm you're not
# a bot". Point YTDLP_COOKIES at an exported cookies.txt, or set
# YTDLP_COOKIES_FROM_BROWSER=chrome/firefox/edge to read cookies from a browser.
YTDLP_COOKIES = os.environ.get("YTDLP_COOKIES")
YTDLP_COOKIES_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER")

# Loaded lazily and cached per (device, model name).
_MODEL_CACHE: dict[str, object] = {}
_DEVICE: str | None = None


def _resolve_device() -> str:
    """Pick the Whisper device once: auto -> cuda when available, else cpu."""
    global _DEVICE
    if _DEVICE is None:
        _DEVICE = _detect_device()
    return _DEVICE

# The Flask dev server runs threaded, so serialize Excel read-modify-write to
# avoid two requests corrupting the report.
_REPORT_LOCK = threading.Lock()

# ---- Cancellation ---------------------------------------------------------- #
# Jobs the user asked to stop. A set of job ids checked by the long-running
# loops (download nudges, the browser sweep, the Twitch playlist watch), which
# each bail out at their next tick. The worker thread itself is a daemon, so a
# job that is inside a non-interruptible call (Whisper on a chunk) finishes
# that call and then stops cleanly — cancellation is cooperative by design.
_CANCEL_LOCK = threading.Lock()
_CANCELLED: set[str] = set()


def request_cancel(job_id: str | None) -> bool:
    """Flag ``job_id`` for cancellation; returns True if a job was flagged."""
    if not job_id:
        return False
    with _CANCEL_LOCK:
        _CANCELLED.add(job_id)
    return True


def cancel_requested(job_id: str | None) -> bool:
    if not job_id:
        return False
    with _CANCEL_LOCK:
        return job_id in _CANCELLED


def _clear_cancel(job_id: str | None) -> None:
    if not job_id:
        return
    with _CANCEL_LOCK:
        _CANCELLED.discard(job_id)


# Live pipeline stages, published for the web UI's progress readout. Keys are
# job ids (``uuid4`` hex from main.py), values {"stage": str, "started": float,
# "detail": str}. ``stage`` is one of fetch/transcribe/ads/done/error. Anything
# UI-side must treat unknown stages as benign and keep the previous label.
_STAGE_LOCK = threading.Lock()
_JOB_STAGES: dict[str, dict] = {}


def report_stage(job_id: str | None, stage: str, detail: str = "") -> None:
    """Publish the current pipeline stage for ``job_id`` (no-op without one)."""
    if not job_id:
        return
    now = time.time()
    with _STAGE_LOCK:
        # Prune entries nobody polled for (closed tab, non-JS client).
        for stale in [k for k, v in _JOB_STAGES.items()
                      if now - v.get("updated", 0) > 3600]:
            _JOB_STAGES.pop(stale, None)
        _JOB_STAGES[job_id] = {"stage": stage, "detail": detail,
                               "updated": now}


def pop_stage(job_id: str) -> dict | None:
    """Latest stage announcement without consuming it.

    The poller calls this every couple of seconds; popping would make the UI
    fall back to its "fetch" default during every quiet stretch (e.g. minutes
    inside Whisper), so peeking is the correct read semantics. Entries are
    reclaimed by the stale-prune in :func:`report_stage` instead.
    """
    with _STAGE_LOCK:
        info = _JOB_STAGES.get(job_id)
        return dict(info) if info else None


def _js_runtimes() -> dict:
    """Return installed JS runtimes for yt-dlp.

    Recent yt-dlp versions only enable Deno by default; without *some* JS runtime
    YouTube format extraction degrades and often fails outright.
    """
    runtimes: dict[str, dict] = {}
    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if path:
            runtimes[name] = {"path": path}
    return runtimes


def _enrich_served_ads(served_report: dict | None) -> None:
    """Attach ready-to-render fields (``_ui``) to every captured served ad.

    The capture readers store proof paths as filesystem paths; the web UI needs
    URLs it can link to, an ordinal badge, and the creative identity in one
    place. Everything is optional — a failed or partial capture renders with
    whatever it has.
    """
    if not served_report:
        return
    base = served_report.get("proof_dir") or ""
    for i, ad in enumerate(served_report.get("ads") or [], start=1):
        # Display order, not the pod index: two spots of one pod would
        # otherwise both badge as "AD 02".
        ui: dict = {
            "ordinal": ad.get("ordinal") or i,
        }

        def _url(value: str | None) -> str | None:
            if not value:
                return None
            if base and value.startswith(base):
                tail = value[len(base):].lstrip("\\/").replace("\\", "/")
                return f"/proof/{tail}"
            # Unknown prefix: the /proof route serves by basename within the
            # default proof dir, which is where captures write anyway.
            tail = value.replace("\\", "/").rsplit("/", 1)[-1]
            return f"/proof/{tail}"

        frames = []
        for key, label in (("frame_start", "FRAME START"), ("frame_mid", "FRAME MID"),
                           ("frame_end", "FRAME END"), ("frame_before", "BEFORE"),
                           ("frame_after", "AFTER")):
            path = ad.get(key)
            if path:
                frames.append({"label": label, "url": _url(path) or path, "path": path})
        ui["frame_urls"] = frames
        ui["frame_paths"] = [f["path"] for f in frames]
        ui["thumb_url"] = _url(ad.get("frame_start") or ad.get("frame_mid")
                               or ad.get("thumbnail"))
        clip = ad.get("clip")
        ui["clip_url"] = _url(clip) if clip else None
        manifest = served_report.get("manifest")
        ui["manifest_url"] = _url(manifest) if manifest else None
        index = served_report.get("index")
        ui["index_url"] = _url(index) if index else None
        creatives = served_report.get("creatives_index")
        ui["creatives_index_url"] = _url(creatives) if creatives else None
        ad["_ui"] = ui


def _build_timeline(link: str | None, ads_report: dict | None,
                    served_report: dict | None) -> dict | None:
    """Percent-positioned marks for the report's timeline bar.

    Serves the web UI only — the Excel report already carries the raw numbers.
    """
    if not link:
        return None
    ads_report = ads_report or {}
    served_report = served_report or {}

    break_starts = [float(b.get("start") or 0.0)
                    for b in (ads_report.get("ad_breaks") or {}).get("breaks") or []]
    spans = [(float(s.get("start") or 0.0), float(s.get("end") or 0.0),
              s.get("category") or "sponsor")
             for s in ads_report.get("segments") or []]
    served_end = max(
        [float(a.get("content_position") or 0) + float(a.get("duration") or 0)
         for a in served_report.get("ads") or [] if a.get("content_duration")]
        or [0.0],
    )

    ends = [b + 60.0 for b in break_starts]
    ends += [end for _, end, _ in spans if end > 0]
    ends.append(served_end)
    total = max(ends + [60.0])

    def _pct(seconds: float) -> float:
        return min(100.0, max(0.0, seconds / total * 100.0))

    ticks = [{"left": _pct(b), "label": served_ads.format_timeline(b),
              "placement": "scheduled break"}
             for b in break_starts]

    sponsors = []
    for start, end, category in spans:
        if end <= start:
            continue
        sponsors.append({
            "left": _pct(start),
            "width": max(0.5, _pct(end) - _pct(start)),
            "label": f"{category} {served_ads.format_timeline(start)} - "
                     f"{served_ads.format_timeline(end)}",
        })

    if not ticks and not sponsors:
        return None
    return {"total": total, "ticks": ticks, "sponsors": sponsors}


def _build_transcript_view(segments, ads_report: dict | None,
                           served_report: dict | None) -> list[dict] | None:
    """Timestamped transcript entries, flagged where an ad read happens.

    A segment is flagged when it overlaps a SponsorBlock sponsor span, a
    scheduled ad-break slot, or a served ad that interrupted at that moment —
    and otherwise when its own words read like a sponsor mention.
    """
    entries = []
    for s in segments or []:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        start = float(s.get("start") or 0.0)
        end = float(s.get("end") or start + 30.0)
        entries.append({"start": start, "end": end, "text": text, "ad": False})
    if not entries:
        return None

    ads_report = ads_report or {}
    served_report = served_report or {}

    def _mark_overlapping(ws: float, we: float) -> None:
        for e in entries:
            if max(e["start"], ws) < min(e["end"], we):
                e["ad"] = True

    for s in ads_report.get("segments") or []:
        _mark_overlapping(float(s.get("start") or 0.0), float(s.get("end") or 0.0))
    for b in (ads_report.get("ad_breaks") or {}).get("breaks") or []:
        start = float(b.get("start") or 0.0)
        _mark_overlapping(start, start + 10.0)
    for a in served_report.get("ads") or []:
        pos = a.get("content_position")
        if pos is None:
            continue
        _mark_overlapping(float(pos), float(pos) + float(a.get("duration") or 10.0) + 5.0)

    for e in entries:
        if not e["ad"] and _is_ad_like_text(e["text"]):
            e["ad"] = True
        del e["end"]
    return entries


def _ensure_work_dir() -> str:
    os.makedirs(WORK_DIR, exist_ok=True)
    return WORK_DIR


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def download_youtube_audio(url: str) -> str:
    """Download the best audio stream for ``url`` and return an mp3 path."""
    if not url or not url.strip():
        raise ValueError("A non-empty URL is required.")

    work = _ensure_work_dir()
    # Unique per download: the original code reused one fixed output name, so a
    # failed download silently left a *previous* run's audio in place and the app
    # transcribed the wrong media entirely.
    out_base = os.path.join(work, f"yt_{uuid.uuid4().hex[:10]}")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_base + ".%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "retries": 5,
    }
    runtimes = _js_runtimes()
    if runtimes:
        ydl_opts["js_runtimes"] = runtimes
    if YTDLP_COOKIES:
        ydl_opts["cookiefile"] = YTDLP_COOKIES
    if YTDLP_COOKIES_BROWSER:
        ydl_opts["cookiesfrombrowser"] = (YTDLP_COOKIES_BROWSER,)

    logger.info("Downloading audio from %s", url)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            last_error = None
            break
        except yt_dlp.utils.DownloadError as exc:
            last_error = exc
            message = str(exc)
            logger.warning("Download attempt %d/3 failed: %s", attempt,
                           message.strip().splitlines()[-1][:200])
            if attempt < 3:
                time.sleep(3 * attempt)

    if last_error is not None:
        raise RuntimeError(f"Could not download media from {url!r}: {last_error}") from last_error

    mp3_path = out_base + ".mp3"
    if not os.path.exists(mp3_path):
        # Post-processing can occasionally land on a different extension.
        candidates = [
            os.path.join(work, f)
            for f in os.listdir(work)
            if f.startswith(os.path.basename(out_base))
        ]
        if not candidates:
            raise RuntimeError("Download succeeded but no audio file was produced.")
        mp3_path = candidates[0]

    return mp3_path


def extract_audio(video_path: str) -> str:
    """Extract a 16 kHz mono wav track from ``video_path`` (or pass audio through)."""
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"No such file: {video_path}")

    if os.path.splitext(video_path)[1].lower() in _READY_AUDIO_EXT:
        return video_path

    if not _ffmpeg_available():
        raise RuntimeError(
            "ffmpeg is required to extract audio from video files but was not "
            "found on PATH."
        )

    audio_path = os.path.splitext(video_path)[0] + ".wav"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        audio_path,
    ]
    logger.info("Extracting audio: %s -> %s", video_path, audio_path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to extract audio: {result.stderr[-500:]}")
    return audio_path


def _get_model(name: str | None = None, device: str | None = None):
    """Load and cache the Whisper model on ``device`` (default: auto-detected).

    A GPU load that fails at the last moment (driver busy, OOM) falls back to
    CPU so an analysis never dies over placement.
    """
    name = name or WHISPER_MODEL
    device = device or _resolve_device()
    key = f"{device}:{name}"
    if key not in _MODEL_CACHE:
        logger.info("Loading Whisper model '%s' on %s", name, device)
        try:
            _MODEL_CACHE[key] = whisper.load_model(name, device=device)
        except Exception as exc:
            if device == "cpu":
                raise
            logger.warning("Whisper on %s failed (%s); falling back to CPU.",
                           device, str(exc)[:120])
            _MODEL_CACHE[key] = whisper.load_model(name, device="cpu")
    return _MODEL_CACHE[key]


# Tensor-shape RuntimeErrors Whisper can throw mid-decode (beam-search width
# mismatch on odd audio; empty-tensor reshape on CUDA with silent chunks). All
# are transient decode-strategy problems, not corrupt input, so the caller
# retries with progressively more conservative settings instead of failing.
_TENSOR_SHAPE_ERRORS = (
    "Sizes of tensors must match",
    "cannot reshape tensor of 0 elements",
)


def transcribe(audio_path: str, model_name: str | None = None,
               language: str | None = None) -> dict:
    """Transcribe an audio file, returning text, timestamped segments, language.

    ``beam_size=5`` trades a little speed for better accuracy than the default
    greedy decoder. Segments are needed for transcript-based ad detection.

    Recovery ladder for decode-time tensor errors: (1) configured settings,
    (2) greedy + fp32 on the same device, (3) CPU for this file when running on
    CUDA — CPU is Whisper's best-tested path.
    """
    model = _get_model(model_name)
    on_cuda = str(getattr(model, "device", "cpu")).startswith("cuda")
    use_fp16 = WHISPER_FP16_DEFAULT
    if use_fp16 is None:  # auto: fp16 only makes sense on CUDA
        use_fp16 = on_cuda
    beam_size = WHISPER_BEAM_SIZE_DEFAULT

    def _run(m, fp16, beam):
        return m.transcribe(
            audio_path, fp16=fp16, beam_size=beam, language=language
        )

    try:
        result = _run(model, use_fp16, beam_size)
    except RuntimeError as exc:
        msg = str(exc)
        if not any(marker in msg for marker in _TENSOR_SHAPE_ERRORS):
            raise
        logger.warning("Whisper decode failed on %s (%s); retrying greedy+fp32.",
                       audio_path, msg[:120])
        try:
            result = _run(model, False, 1)
        except RuntimeError as exc2:
            msg2 = str(exc2)
            if not any(marker in msg2 for marker in _TENSOR_SHAPE_ERRORS) \
                    or not on_cuda:
                raise
            logger.warning("Whisper still failing on CUDA (%s); using CPU for "
                           "this file.", msg2[:120])
            model = _get_model(model_name, device="cpu")
            result = _run(model, False, 1)
    segments = [
        {
            "start": float(s.get("start", 0.0)),
            "end": float(s.get("end", 0.0)),
            "text": (s.get("text") or "").strip(),
        }
        for s in (result.get("segments") or [])
    ]
    return {
        "text": (result.get("text") or "").strip(),
        "segments": segments,
        "language": result.get("language"),
        "device": str(getattr(model, "device", "cpu")),
    }


def transcribe_audio(audio_path: str, model_name: str | None = None,
                     language: str | None = None) -> str:
    """Backwards-compatible helper returning only the transcript text."""
    return transcribe(audio_path, model_name=model_name, language=language)["text"]


def _transformer_sentiment(text: str) -> float | None:
    """Polarity in [-1, 1] from the transformer model, or None if unavailable.

    Long transcripts are split into sentence-ish chunks (the model's window is
    a few hundred characters), each scored, then combined by confidence-weighted
    vote; the winner's score is signed by the label.
    """
    nlp = _get_sentiment_pipeline()
    if nlp is None:
        return None
    try:
        chunks = [c.strip() for c in text.split(".") if c.strip()]
        if not chunks:
            return None
        # Pack chunks up to the model window to cut the number of passes.
        packed: list[str] = []
        buf = ""
        for chunk in chunks:
            if len(buf) + len(chunk) + 2 <= SENTIMENT_MAX_CHARS:
                buf = f"{buf}. {chunk}" if buf else chunk
            else:
                if buf:
                    packed.append(buf)
                buf = chunk[:SENTIMENT_MAX_CHARS]
        if buf:
            packed.append(buf)

        preds = nlp(packed)
        score_by_label = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
        weighted: dict[str, float] = {"positive": 0.0, "negative": 0.0, "neutral": 0.0}
        for pred in preds:
            label = str(pred.get("label", "neutral")).strip().lower()
            conf = float(pred.get("score", 0.0))
            if label.startswith("label_"):  # some checkpoints emit LABEL_0/1/2
                label = {"label_0": "negative", "label_1": "neutral",
                         "label_2": "positive"}.get(label, "neutral")
            if label not in weighted:
                continue
            weighted[label] += conf
        best = max(weighted, key=weighted.get)
        return score_by_label[best] * weighted[best]
    except Exception as exc:
        logger.warning("Transformer sentiment failed (%s); falling back.",
                       str(exc)[:140])
        return None


def analyze_text(text: str) -> tuple[float, list[str]]:
    """Return (sentiment polarity, keyword list) for ``text``.

    Sentiment uses a small transformer fine-tuned on casual text (tweets),
    which reads gaming slang and hype correctly where TextBlob's lexicon does
    not; TextBlob remains the fallback when the model is unavailable. Keyword
    extraction is best-effort: TextBlob's noun-phrase tagger can require NLTK
    corpora that may not be present, so it falls back to frequency-based
    extraction rather than failing the whole pipeline.
    """
    text = text or ""
    if not text.strip():
        return 0.0, []

    sentiment = _transformer_sentiment(text)
    blob = TextBlob(text)  # still used for noun-phrase keywords
    if sentiment is None:
        sentiment = float(blob.sentiment.polarity)

    keywords: list[str] = []
    try:
        keywords = [phrase for phrase in blob.noun_phrases if phrase.strip()]
    except Exception as exc:  # missing NLTK corpora, tagger errors, etc.
        logger.warning("Noun-phrase extraction failed (%s); using fallback.", exc)

    if not keywords:
        keywords = _fallback_keywords(text)

    # De-duplicate while preserving order, then cap the list.
    seen = set()
    unique = []
    for word in keywords:
        w = word.strip()
        if w and w.lower() not in seen:
            seen.add(w.lower())
            unique.append(w)
    return sentiment, unique[:25]


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "so", "of", "to", "in",
    "on", "at", "for", "with", "as", "by", "is", "are", "was", "were", "be",
    "been", "it", "this", "that", "these", "those", "i", "you", "he", "she",
    "we", "they", "me", "him", "her", "us", "them", "my", "your", "his", "its",
    "our", "their", "not", "no", "do", "does", "did", "have", "has", "had",
    "will", "would", "can", "could", "just", "about", "there", "here", "what",
    "which", "who", "when", "where", "why", "how", "all", "any", "some", "from",
    "up", "down", "out", "off", "over", "under", "again", "very", "too", "s",
    "t", "m", "re", "ve", "ll", "d", "o",
}


def _fallback_keywords(text: str, limit: int = 25) -> list[str]:
    counts: dict[str, int] = {}
    for raw in text.lower().split():
        word = "".join(ch for ch in raw if ch.isalnum())
        if len(word) < 3 or word in _STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ranked[:limit]]


def _ensure_headers(sheet) -> None:
    """Make the sheet's header row match REPORT_HEADERS.

    Reports written by older versions have fewer columns; the header row is
    extended in place so existing rows stay aligned.
    """
    if sheet.max_row == 1 and sheet.cell(row=1, column=1).value is None:
        sheet.append(REPORT_HEADERS)
        return
    for column, header in enumerate(REPORT_HEADERS, start=1):
        if sheet.cell(row=1, column=column).value != header:
            sheet.cell(row=1, column=column, value=header)


def save_to_excel(
    platform: str,
    target: str,
    transcript: str,
    sentiment: float,
    keywords,
    ad_report: dict | None = None,
    served_report: dict | None = None,
    path: str = REPORT_PATH,
) -> str:
    """Append one analysis row to the Excel report, creating it if needed."""
    if isinstance(keywords, (list, tuple)):
        keywords_value = ", ".join(keywords)
    else:
        keywords_value = str(keywords)

    ad_report = ad_report or {}
    served_report = served_report or {}
    served = served_report.get("ads") or []
    row = [
        platform,
        target,
        transcript,
        sentiment,
        keywords_value,
        ad_report.get("ad_count", 0),
        ad_report.get("ad_seconds", 0),
        ads.format_segments(ad_report.get("segments") or []),
        served_report.get("ad_count", 0),
        served_ads.format_served_ads(served),
    ]

    with _REPORT_LOCK:
        if os.path.exists(path):
            workbook = load_workbook(path)
            sheet = workbook.active
            _ensure_headers(sheet)
        else:
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Sheet1"
            sheet.append(REPORT_HEADERS)

        sheet.append(row)
        workbook.save(path)
    logger.info("Appended result to %s", path)
    return path


def process_video(link: str | None = None, file_path: str | None = None,
                  keep_temp: bool = False, report_path: str = REPORT_PATH,
                  model_name: str | None = None, language: str | None = None,
                  capture_served: bool | None = None,
                  served_watch_seconds: float | None = None,
                  proof_dir: str | None = None,
                  job_id: str | None = None) -> dict:
    """Run the full pipeline and return a summary dict.

    Provide exactly one of ``link`` (remote URL) or ``file_path`` (local media).

    ``capture_served`` enables the served-ad capture for links. For YouTube that
    means driving a real Chrome to watch the video and record the ads actually
    played (the pre-roll/mid-roll spots behind the yellow progress bar), which is
    why it is off by default and takes ``served_watch_seconds``. For Twitch no
    browser is needed: its ads are stitched into the stream, so they are read
    from the stream's own playlist markers while the channel is live.

    ``job_id`` (optional) keys the stage announcements the web UI polls while
    the request runs; pipeline calls without one behave exactly as before.
    """
    if capture_served is None:
        capture_served = CAPTURE_SERVED_ADS
    if served_watch_seconds is None:
        served_watch_seconds = _served_window()
    temp_files: list[str] = []

    if link:
        platform = "Twitch" if served_ads.detect_platform(link) == "twitch" else "YouTube"
        target = link
        report_stage(job_id, "fetch",
                     "downloading audio from " + (target or "the link"))
        video_path = download_youtube_audio(link)
        temp_files.append(video_path)
    elif file_path and os.path.exists(file_path):
        platform = "Uploaded File"
        target = file_path
        report_stage(job_id, "fetch", "reading " + os.path.basename(file_path))
        video_path = file_path
    else:
        raise ValueError("Provide a YouTube link or upload a media file.")

    audio_path = extract_audio(video_path)
    if audio_path != video_path:
        temp_files.append(audio_path)

    # Served-ad capture only needs the link, so start it in parallel with the
    # (much slower) transcription instead of after it. On a 27-minute video
    # this hides the whole sweep inside the Whisper pass.
    capture_thread = None
    capture_box: dict = {}
    if capture_served and link:
        report_stage(job_id, "ads",
                     "watching for served ads in parallel with transcription on "
                     + ("twitch.tv" if platform == "Twitch" else "youtube"))

        def _capture():
            try:
                capture_box["report"] = served_ads.capture_served_ads(
                    link,
                    watch_seconds=served_watch_seconds,
                    proof_dir=proof_dir,
                    video_id=ads.extract_video_id(link),
                    cancel_check=lambda: cancel_requested(job_id),
                )
            except Exception as exc:  # capture must never break the pipeline
                logger.warning("Served-ad capture failed: %s", exc)
                capture_box["report"] = {
                    "available": False, "captured": False, "ads": [], "ad_count": 0,
                    "ad_seconds": 0.0, "note": f"Served-ad capture failed: {exc}",
                }

        capture_thread = threading.Thread(target=_capture, name="served-capture",
                                          daemon=True)
        capture_thread.start()

    _device = _resolve_device()
    report_stage(job_id, "transcribe",
                 f"whisper {model_name or WHISPER_MODEL} on {_device.upper()} "
                 f"— {os.path.basename(audio_path)}")
    result = transcribe(audio_path, model_name=model_name, language=language)
    transcript = result["text"]
    sentiment, keywords = analyze_text(transcript)

    served_report: dict | None = None
    if capture_thread is not None:
        capture_thread.join()  # usually already finished inside the Whisper pass
        served_report = capture_box.get("report")

    report_stage(job_id, "ads", "scanning transcript for sponsor reads")
    try:
        ad_report = ads.detect_ads(
            link=link,
            transcript_segments=result["segments"],
            transcript=transcript,
            served_ads_report=served_report,
        )
    except Exception as exc:  # ad detection must never break the pipeline
        logger.warning("Ad detection failed: %s", exc)
        ad_report = {
            "ad_count": 0, "ad_seconds": 0, "segments": [],
            "verified": None, "note": f"Ad detection failed: {exc}",
        }

    _enrich_served_ads(served_report)
    timeline = _build_timeline(link, ad_report, served_report)
    transcript_view = _build_transcript_view(result["segments"], ad_report,
                                             served_report)

    save_to_excel(platform, target, transcript, sentiment, keywords,
                  ad_report=ad_report, served_report=served_report,
                  path=report_path)

    if not keep_temp:
        for tmp in temp_files:
            try:
                os.remove(tmp)
            except OSError:
                pass

    summary = {
        "platform": platform,
        "target": target,
        "transcript": transcript,
        "transcript_view": transcript_view,
        "timeline": timeline,
        "sentiment": sentiment,
        "keywords": keywords,
        "report": report_path,
        "model": model_name or WHISPER_MODEL,
        "device": result.get("device", "cpu"),
        "language": result.get("language"),
        "ads": ad_report,
        "served": served_report or ad_report.get("served") or {},
    }
    report_stage(job_id, "done", "analysis complete")
    logger.info("Analysis complete: %s", os.path.basename(str(target)))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    process_video(
        link="https://www.youtube.com/watch?v=2vjPBrBU-TM"
    )
