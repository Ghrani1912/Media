"""Flask front-end for the media analyzer.

Accepts either a YouTube/Twitch link or an uploaded media file, runs it through
the pipeline in ``process.py``, and reports the result.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid

from flask import Flask, abort, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

import served_ads

from process import REPORT_PATH, WHISPER_MODEL, process_video

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
    "small": "balanced accuracy (default)",
    "medium": "more accurate, slower",
    "large": "most accurate, slowest deep pass",
}


def _display_title(link: str | None, file_path: str | None) -> str:
    """Human title for the report header: video title when it can be probed,
    otherwise the file/URL's own name."""
    if file_path:
        return os.path.basename(file_path)
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
        "max_upload_mb": MAX_CONTENT_LENGTH // (1024 * 1024),
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


@app.route("/_abort")
def _abort_probe():  # pragma: no cover - defensive helper
    abort(400)


def _unique_path(filename: str) -> str:
    """Return a collision-free path so concurrent uploads never overwrite."""
    safe = secure_filename(filename) or "upload"
    return os.path.join(app.config["UPLOAD_FOLDER"], f"{uuid.uuid4().hex[:8]}_{safe}")


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_template("index.html")

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
        return render_template("index.html", error=f"Processing failed: {exc}"), 500

    result["title"] = _display_title(link, uploaded_path)
    return render_template("index.html", result=result,
                           selected_model=model or WHISPER_MODEL)


@app.errorhandler(413)
def too_large(_exc):
    return (
        render_template(
            "index.html",
            error=f"File is too large. Maximum upload size is "
            f"{MAX_CONTENT_LENGTH // (1024 * 1024)} MB.",
        ),
        413,
    )


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
