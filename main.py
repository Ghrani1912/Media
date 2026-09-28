"""Flask front-end for the media analyzer.

Accepts either a YouTube/Twitch link or an uploaded media file, runs it through
the pipeline in ``process.py``, and reports the result.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_from_directory)
from werkzeug.utils import secure_filename

import served_ads
from process import REPORT_PATH, WHISPER_MODEL, pop_stage, process_video

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "m4a", "mp3", "wav"}
ALLOWED_MODELS = {"tiny", "base", "small", "medium", "large"}
MAX_CONTENT_LENGTH = 1024 * 1024 * 1024  # 1 GiB upload cap

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# One-line description of each transcription model, shown by the segmented
# accuracy control.
MODEL_NOTES = {
    "tiny": "lowest resource, fastest turnaround",
    "base": "small and quick, rougher on noisy audio",
    "small": "balanced accuracy",
    "medium": "more accurate, slower",
    "large": "most accurate, slowest deep pass",
}


def _display_title(link: str | None, file_path: str | None) -> str:
    """Human title for the report header: video title when it can be probed,
    otherwise the file/URL's own name."""
    if file_path:
        # strip the uuid prefix /analyze adds for collision-free storage
        return re.sub(r"^[0-9a-f]{8}_", "", os.path.basename(file_path))
    if link:
        try:
            info = served_ads.probe_title(link)
        except Exception as exc:  # probe must never break the page
            logger.info("Title probe failed for %s: %s", link, exc)
            info = None
        if info:
            return info
        return served_ads.fallback_title(link)
    return "Untitled media"


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.context_processor
def inject_form_defaults():
    """Ensure every template render has the model selector data available.

    ``selected_model`` defaults to the configured model so the segmented
    accuracy control actually reflects the model the server will use.
    """
    return {
        "models": sorted(ALLOWED_MODELS),
        "selected_model": WHISPER_MODEL,
        "default_model": WHISPER_MODEL,
        "model_notes": MODEL_NOTES,
        "max_upload_label": (
            f"{MAX_CONTENT_LENGTH // (1024 * 1024 * 1024)} GB"
            if MAX_CONTENT_LENGTH % (1024 * 1024 * 1024) == 0
            else f"{MAX_CONTENT_LENGTH // (1024 * 1024)} MB"
        ),
    }


@app.template_filter("hms")
def hms_filter(seconds) -> str:
    """Hour-aware clock format: 3661 -> '1:01:01', 66 -> '1:06'."""
    try:
        total = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        return "0:00"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@app.template_filter("tc")
def tc_filter(seconds) -> str:
    """Zero-padded timecode for tables and transcripts: 490.5 -> '00:08:10'."""
    try:
        total = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        return "00:00:00"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@app.template_filter("basename")
def basename_filter(path) -> str:
    """Final path segment, slash- and backslash-tolerant."""
    return str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


# The capture readers write proof clips, decoded frames and manifests under
# proof/; this route lets the report sheet link to them without making the
# whole uploads tree public.
@app.route("/proof/<path:filename>")
def proof_file(filename: str):
    return send_from_directory(served_ads.DEFAULT_PROOF_DIR, filename)


@app.route("/downloads/<path:filename>")
def download_file(filename: str):
    """Force a download (with a filename) for report workbook links."""
    return send_from_directory(
        os.path.dirname(os.path.abspath(REPORT_PATH)),
        os.path.basename(REPORT_PATH),
        as_attachment=True,
        download_name=filename or "media_report.xlsx",
    )


@app.route("/mime/<path:guess>")
def mime_probe(guess: str):
    # Kept minimal: mimetypes is only used to hint at the right download name.
    return {"guessed": mimetypes.guess_type(guess)[0]}


# --------------------------------------------------------------------------- #
# Job model: the browser posts the form as JSON, gets a job id back immediately,
# and polls /status/<id> while a worker thread runs the real pipeline. The
# pipeline announces its stages (fetch/transcribe/ads) via report_stage, so the
# ANALYZE button shows where the work actually is instead of timed guesses.
# --------------------------------------------------------------------------- #
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 1800.0  # seconds a finished job stays fetchable

_STAGE_LABELS = {
    "fetch": ("01/03 FETCHING AUDIO", 33.3),
    "transcribe": ("02/03 TRANSCRIBING", 66.6),
    "ads": ("03/03 READING ADS", 100.0),
}


def _prune_jobs() -> None:
    cutoff = time.time() - _JOB_TTL
    with _JOBS_LOCK:
        for job_id in [j for j, v in _JOBS.items()
                       if v.get("finished_at", 0) and v["finished_at"] < cutoff]:
            _JOBS.pop(job_id, None)


def _run_job(job_id: str, **kwargs) -> None:
    """Worker: run the pipeline, record the outcome, keep the page's data.

    The job is marked done/error only after the upload is cleaned up and the
    log archived, so anything that sees "done" also sees every side effect.
    """
    file_path = kwargs.get("file_path")
    state, html, error, summary = "error", None, None, None
    try:
        with app.app_context():  # render_template needs a context; threads don't get one
            result = process_video(job_id=job_id, **kwargs)
            result["title"] = _display_title(kwargs.get("link"), file_path)
            html = render_template(
                "index.html", result=result,
                selected_model=kwargs.get("model_name") or WHISPER_MODEL)
            state, summary = "done", result
    except Exception as exc:  # the page must show the reason, not spin forever
        logger.exception("Job %s failed", job_id)
        error = str(exc)
        with app.app_context():
            html = render_template("index.html", error=f"Processing failed: {exc}")
    finally:
        # Uploads are transient: the old synchronous path removed them in a
        # finally, and the worker must too.
        if file_path:
            try:
                os.remove(file_path)
            except OSError:
                pass
        if html is not None:
            _record_history(job_id, state, html,
                            summary or {"platform": "", "title": (error or "")[:120]})
        with _JOBS_LOCK:
            _JOBS[job_id].update(state=state, html=html, error=error,
                                 finished_at=time.time())


def _pop_unseen_stage(job_id: str) -> dict | None:
    """Latest un-polled stage announcement, if any."""
    return pop_stage(job_id)


@app.post("/analyze")
def analyze():
    """Accept the form (JSON or multipart), start a job, return its id."""
    _prune_jobs()
    payload = request.get_json(silent=True) or {}

    link = (payload.get("link") or request.form.get("link") or "").strip()
    model = (payload.get("model") or request.form.get("model") or "").strip()
    capture_served = bool(payload.get("capture_served")
                          or request.form.get("capture_served"))
    file = request.files.get("file")

    if not link and not (file and file.filename):
        return jsonify({"error": "Provide a YouTube/Twitch link or upload a file."}), 400
    if link and file and file.filename:
        return jsonify({"error": "Provide a link or a file, not both."}), 400
    if file and file.filename and not allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type. Allowed: "
                        + ", ".join(sorted(ALLOWED_EXTENSIONS))}), 400
    if model not in ALLOWED_MODELS:
        model = None  # fall back to the configured default

    job_id = uuid.uuid4().hex
    file_path = None
    if file and file.filename:
        file_path = _unique_path(file.filename)
        file.save(file_path)

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "state": "running", "created_at": time.time(),
            "finished_at": None, "html": None, "error": None,
        }

    kwargs = {"model_name": model, "capture_served": capture_served}
    if link:
        kwargs["link"] = link
    else:
        kwargs["file_path"] = file_path
    worker = threading.Thread(target=_run_job, args=(job_id,), kwargs=kwargs,
                              name=f"analyze-{job_id[:8]}", daemon=True)
    worker.start()
    return jsonify({"job": job_id})


@app.get("/status/<job_id>")
def job_status(job_id: str):
    """Poll a job: pipeline stage while running, the rendered page when done."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "unknown job"}), 404
        state = job["state"]
        error = job["error"]
        html = job["html"]

    if state == "done":
        return jsonify({"state": "done", "html": html})
    if state == "error":
        return jsonify({"state": "error", "error": error, "html": html})

    stage_info = _pop_unseen_stage(job_id)
    stage = (stage_info or {}).get("stage") or "fetch"
    label, pct = _STAGE_LABELS.get(stage, ("01/03 FETCHING AUDIO", 33.3))
    return jsonify({"state": "running", "stage": stage, "label": label,
                    "percent": pct, "detail": (stage_info or {}).get("detail", "")})


# --------------------------------------------------------------------------- #
# Log history: every finished analysis is archived as a rendered page plus an
# index.json line, so past logs can be re-opened from the input page even after
# a server restart. The dev server's reloader would otherwise wipe _JOBS, and
# the synchronous POST path never had a handle to re-open anyway.
# --------------------------------------------------------------------------- #
HISTORY_DIR = os.environ.get("MEDIA_HISTORY_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "history"
)
HISTORY_MAX = 30  # keep the most recent N logs; older ones are deleted


def _history_load() -> list[dict]:
    path = os.path.join(HISTORY_DIR, "index.json")
    try:
        with open(path, encoding="utf-8") as fh:
            entries = json.load(fh)
        return entries if isinstance(entries, list) else []
    except (OSError, ValueError):
        return []


def _history_save(entries: list[dict]) -> None:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, "index.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # atomic-ish: a crash never truncates the index


_HISTORY_LOCK = threading.Lock()


def _record_history(job_id: str, state: str, html: str, summary: dict) -> None:
    """Archive one finished analysis (best-effort; never breaks the job)."""
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        page_path = os.path.join(HISTORY_DIR, f"{job_id}.html")
        with open(page_path, "w", encoding="utf-8") as fh:
            fh.write(html)

        ads_report = summary.get("ads") or {}
        served_report = summary.get("served") or {}
        transcript = (summary.get("transcript") or "").strip()
        entry = {
            "id": job_id,
            "state": state,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": time.time(),
            "platform": summary.get("platform", ""),
            "title": summary.get("title") or summary.get("target") or "",
            "model": summary.get("model", ""),
            "sentiment": round(float(summary.get("sentiment") or 0.0), 3),
            "ads": (ads_report.get("ad_count") or 0),
            "served": (served_report.get("ad_count") or 0),
            # first words of the transcript, so a row is recognisable at a glance
            "snippet": transcript[:140] + ("…" if len(transcript) > 140 else ""),
        }
        with _HISTORY_LOCK:  # concurrent workers share the index read-modify-write
            entries = [e for e in _history_load() if e.get("id") != job_id]
            entries.insert(0, entry)
            dropped = entries[HISTORY_MAX:]
            entries = entries[:HISTORY_MAX]
            _history_save(entries)
        for old in dropped:
            try:
                os.remove(os.path.join(HISTORY_DIR, f"{old.get('id')}.html"))
            except OSError:
                pass
    except Exception:
        logger.exception("Could not archive log %s", job_id)


@app.get("/logs/<job_id>")
def open_log(job_id: str):
    """Serve an archived log page (its own self-contained report sheet)."""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        abort(404)
    return send_from_directory(HISTORY_DIR, f"{job_id}.html")


@app.post("/logs/clear")
def clear_logs():
    """Forget all archived logs (used by the PREVIOUS LOGS 'CLEAR' button)."""
    if os.path.isdir(HISTORY_DIR):
        for name in os.listdir(HISTORY_DIR):
            if name.endswith(".html") or name == "index.json":
                try:
                    os.remove(os.path.join(HISTORY_DIR, name))
                except OSError:
                    pass
    return redirect("/")


def _unique_path(filename: str) -> str:
    """Return a collision-free path so concurrent uploads never overwrite."""
    safe = secure_filename(filename) or "upload"
    return os.path.join(app.config["UPLOAD_FOLDER"], f"{uuid.uuid4().hex[:8]}_{safe}")


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_template("index.html", history=_history_load())

    link = (request.form.get("link") or "").strip()
    file = request.files.get("file")
    model = (request.form.get("model") or "").strip()
    capture_served = bool(request.form.get("capture_served"))
    uploaded_path = None
    if model not in ALLOWED_MODELS:
        model = None  # fall back to the configured default

    try:
        if link:
            result = process_video(link=link, model_name=model,
                                   capture_served=capture_served)
        elif file and file.filename:
            if not allowed_file(file.filename):
                return render_template(
                    "index.html",
                    error=(
                        "Unsupported file type. Allowed: "
                        + ", ".join(sorted(ALLOWED_EXTENSIONS))
                    ),
                ), 400
            filepath = _unique_path(file.filename)
            file.save(filepath)
            uploaded_path = filepath
            try:
                result = process_video(file_path=filepath, model_name=model)
            finally:
                # Don't let uploads accumulate on disk once processed.
                try:
                    os.remove(filepath)
                except OSError:
                    pass
        else:
            return render_template(
                "index.html", error="Provide a YouTube/Twitch link or upload a file."
            ), 400

    except Exception as exc:  # surface the real reason instead of a blank page
        logger.exception("Processing failed")
        return render_template("index.html", error=f"Processing failed: {exc}",
                               history=_history_load()), 500

    result["title"] = _display_title(link, uploaded_path)
    html = render_template("index.html", result=result,
                           selected_model=model or WHISPER_MODEL)
    # Rendered server-side for non-JS clients; the browser JS uses /analyze.
    # Archive it too, so the log is re-openable from PREVIOUS LOGS later.
    _record_history(uuid.uuid4().hex, "done", html, result)
    return html


@app.errorhandler(413)
def too_large(_exc):
    return (
        render_template(
            "index.html",
            error=f"File is too large. Maximum upload size is "
            f"{MAX_CONTENT_LENGTH // (1024 * 1024)} MB.",
            history=_history_load(),
        ),
        413,
    )


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    # The default watchdog reloader restarts on ANY file event under the cwd,
    # including yt-dlp cache writes inside ./mediaenv311 -- that would wipe the
    # in-memory job table mid-analysis. The plain stat reloader only watches
    # imported .py files, so long jobs survive package churn.
    app.run(debug=True, use_reloader=True, reloader_type="stat",
            host="127.0.0.1", port=5000)
