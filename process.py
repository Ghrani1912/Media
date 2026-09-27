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
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import whisper
import yt_dlp
from openpyxl import Workbook, load_workbook
from textblob import TextBlob

import ads
import served_ads

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
# CAPTURE_SERVED_ADS=1 (or the UI checkbox when analyzing a YouTube link).
CAPTURE_SERVED_ADS = os.environ.get("CAPTURE_SERVED_ADS", "").strip().lower() in {
    "1", "true", "yes"
}

# Audio extensions that need no ffmpeg conversion.
_READY_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}

# Whisper model used for transcription. "base" is small but noticeably wrong on
# music/noisy audio; "small" is a much better default. Override with WHISPER_MODEL.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")

# Optional yt-dlp authentication, needed when YouTube asks to "confirm you're not
# a bot". Point YTDLP_COOKIES at an exported cookies.txt, or set
# YTDLP_COOKIES_FROM_BROWSER=chrome/firefox/edge to read cookies from a browser.
YTDLP_COOKIES = os.environ.get("YTDLP_COOKIES")
YTDLP_COOKIES_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER")

# Loaded lazily and cached per model name.
_MODEL_CACHE: dict[str, object] = {}

# The Flask dev server runs threaded, so serialize Excel read-modify-write to
# avoid two requests corrupting the report.
_REPORT_LOCK = threading.Lock()


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


def _get_model(name: str | None = None):
    name = name or WHISPER_MODEL
    if name not in _MODEL_CACHE:
        logger.info("Loading Whisper model '%s'", name)
        _MODEL_CACHE[name] = whisper.load_model(name)
    return _MODEL_CACHE[name]


def transcribe(audio_path: str, model_name: str | None = None,
               language: str | None = None) -> dict:
    """Transcribe an audio file, returning text, timestamped segments, language.

    ``beam_size=5`` trades a little speed for better accuracy than the default
    greedy decoder. Segments are needed for transcript-based ad detection.
    """
    model = _get_model(model_name)
    result = model.transcribe(
        audio_path, fp16=False, beam_size=5, language=language
    )
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
    }


def transcribe_audio(audio_path: str, model_name: str | None = None,
                     language: str | None = None) -> str:
    """Backwards-compatible helper returning only the transcript text."""
    return transcribe(audio_path, model_name=model_name, language=language)["text"]


def analyze_text(text: str) -> tuple[float, list[str]]:
    """Return (sentiment polarity, keyword list) for ``text``.

    Keyword extraction is best-effort: TextBlob's noun-phrase tagger can require
    NLTK corpora that may not be present, so it falls back to frequency-based
    extraction rather than failing the whole pipeline.
    """
    text = text or ""
    if not text.strip():
        return 0.0, []

    blob = TextBlob(text)
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
                  proof_dir: str | None = None) -> dict:
    """Run the full pipeline and return a summary dict.

    Provide exactly one of ``link`` (remote URL) or ``file_path`` (local media).

    ``capture_served`` enables the live served-ad capture for YouTube links: a
    real Chrome is driven to watch the video and record the ads YouTube actually
    plays (the pre-roll/mid-roll spots behind the yellow progress bar). It is off
    by default because it opens a browser and takes ``served_watch_seconds``.
    """
    if capture_served is None:
        capture_served = CAPTURE_SERVED_ADS
    temp_files: list[str] = []

    if link:
        platform = "YouTube"
        target = link
        video_path = download_youtube_audio(link)
        temp_files.append(video_path)
    elif file_path and os.path.exists(file_path):
        platform = "Uploaded File"
        target = file_path
        video_path = file_path
    else:
        raise ValueError("Provide a YouTube link or upload a media file.")

    audio_path = extract_audio(video_path)
    if audio_path != video_path:
        temp_files.append(audio_path)

    result = transcribe(audio_path, model_name=model_name, language=language)
    transcript = result["text"]
    sentiment, keywords = analyze_text(transcript)

    served_report: dict | None = None
    if capture_served and link:
        try:
            served_report = served_ads.capture_served_ads(
                link,
                watch_seconds=served_watch_seconds,
                proof_dir=proof_dir,
                video_id=ads.extract_video_id(link),
            )
        except Exception as exc:  # capture must never break the pipeline
            logger.warning("Served-ad capture failed: %s", exc)
            served_report = {
                "available": False, "captured": False, "ads": [], "ad_count": 0,
                "ad_seconds": 0.0, "note": f"Served-ad capture failed: {exc}",
            }

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
        "sentiment": sentiment,
        "keywords": keywords,
        "report": report_path,
        "model": model_name or WHISPER_MODEL,
        "language": result.get("language"),
        "ads": ad_report,
        "served": served_report or ad_report.get("served") or {},
    }
    logger.info("Analysis complete: %s", os.path.basename(str(target)))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    process_video(
        link="https://www.youtube.com/watch?v=2vjPBrBU-TM"
    )
