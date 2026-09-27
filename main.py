"""Flask front-end for the media analyzer.

Accepts either a YouTube/Twitch link or an uploaded media file, runs it through
the pipeline in ``process.py``, and reports the result.
"""

from __future__ import annotations

import logging
import os
import uuid

from flask import Flask, render_template, request
from werkzeug.utils import secure_filename

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


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.context_processor
def inject_form_defaults():
    """Ensure every template render has the model dropdown data available.

    ``selected_model`` defaults to the configured model so the dropdown actually
    reflects the model the server will use, rather than silently showing the
    first alphabetical option.
    """
    return {
        "models": sorted(ALLOWED_MODELS),
        "selected_model": WHISPER_MODEL,
        "default_model": WHISPER_MODEL,
    }


def _unique_path(filename: str) -> str:
    """Return a collision-free path so concurrent uploads never overwrite."""
    safe = secure_filename(filename) or "upload"
    return os.path.join(app.config["UPLOAD_FOLDER"], f"{uuid.uuid4().hex[:8]}_{safe}")


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_template("index.html", models=sorted(ALLOWED_MODELS),
                               selected_model=WHISPER_MODEL)

    link = (request.form.get("link") or "").strip()
    file = request.files.get("file")
    model = (request.form.get("model") or "").strip()
    capture_served = bool(request.form.get("capture_served"))
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
                )
            filepath = _unique_path(file.filename)
            file.save(filepath)
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
            )

    except Exception as exc:  # surface the real reason instead of a blank page
        logger.exception("Processing failed")
        return render_template("index.html", error=f"Processing failed: {exc}"), 500

    return render_template("index.html", result=result, report=REPORT_PATH,
                           models=sorted(ALLOWED_MODELS),
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
