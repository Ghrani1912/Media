"""Tests for the media analyzer pipeline and Flask front-end.

Whisper and yt-dlp are mocked so the suite runs fast and offline.
"""

import io
import os

import pytest

import process


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
class _FakeModel:
    def __init__(self, text="mocked transcript", segments=None):
        self.text = text
        self.segments = segments or []
        self.calls = []

    def transcribe(self, audio_path, **kwargs):
        self.calls.append((audio_path, kwargs))
        return {"text": self.text, "segments": self.segments, "language": "en"}


@pytest.fixture
def fake_model(monkeypatch):
    model = _FakeModel()
    monkeypatch.setattr(process, "_get_model", lambda name=None: model)
    return model


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "clip.mp3"
    path.write_bytes(b"not-really-audio")
    return str(path)


# --------------------------------------------------------------------------- #
# analyze_text
# --------------------------------------------------------------------------- #
def test_analyze_text_positive():
    sentiment, keywords = process.analyze_text(
        "I love this wonderful and amazing song, the music is beautiful."
    )
    assert sentiment > 0
    assert keywords


def test_analyze_text_negative():
    sentiment, _ = process.analyze_text("This is terrible, awful and horrible.")
    assert sentiment < 0


def test_analyze_text_empty():
    assert process.analyze_text("") == (0.0, [])
    assert process.analyze_text(None) == (0.0, [])


def test_analyze_text_falls_back_when_noun_phrases_fail(monkeypatch):
    class _Boom:
        sentiment = type("S", (), {"polarity": 0.1})()

        @property
        def noun_phrases(self):
            raise RuntimeError("no corpora")

    monkeypatch.setattr(process, "TextBlob", lambda text: _Boom())
    sentiment, keywords = process.analyze_text("the quick brown fox fox fox")
    assert sentiment == pytest.approx(0.1)
    assert "fox" in keywords


def test_fallback_keywords_orders_by_frequency():
    words = process._fallback_keywords("apple banana apple apple cherry banana")
    assert words[0] == "apple"


# --------------------------------------------------------------------------- #
# save_to_excel
# --------------------------------------------------------------------------- #
def test_save_to_excel_creates_file_with_headers(tmp_path):
    from openpyxl import load_workbook

    report = tmp_path / "r.xlsx"
    process.save_to_excel("YouTube", "u", "text", 0.5, ["a", "b"], path=str(report))

    ws = load_workbook(str(report)).active
    assert ws.max_row == 2
    assert [c.value for c in ws[1]] == process.REPORT_HEADERS
    assert ws.cell(2, 1).value == "YouTube"
    assert ws.cell(2, 5).value == "a, b"


def test_save_to_excel_appends_rows(tmp_path):
    from openpyxl import load_workbook

    report = tmp_path / "r.xlsx"
    process.save_to_excel("YouTube", "1", "t1", 0.1, ["k"], path=str(report))
    process.save_to_excel("Uploaded File", "2", "t2", -0.2, [], path=str(report))

    ws = load_workbook(str(report)).active
    assert ws.max_row == 3
    assert ws.cell(2, 2).value == "1"
    assert ws.cell(3, 2).value == "2"


def test_save_to_excel_accepts_string_keywords(tmp_path):
    from openpyxl import load_workbook

    report = tmp_path / "r.xlsx"
    process.save_to_excel("YouTube", "u", "t", 0.0, "already, joined", path=str(report))
    ws = load_workbook(str(report)).active
    assert ws.cell(2, 5).value == "already, joined"


def test_save_to_excel_writes_ad_columns(tmp_path):
    from openpyxl import load_workbook

    report = tmp_path / "r.xlsx"
    ad_report = {
        "ad_count": 2,
        "ad_seconds": 61.3,
        "segments": [
            {"category": "sponsor", "start": 490.5, "end": 545.8, "duration": 55.3, "source": "sponsorblock"},
            {"category": "sponsor", "start": 708.0, "end": 723.0, "duration": 15.1, "source": "sponsorblock"},
        ],
    }
    process.save_to_excel("YouTube", "u", "t", 0.1, ["k"],
                          ad_report=ad_report, path=str(report))
    ws = load_workbook(str(report)).active
    assert ws.cell(1, 6).value == "Ads Detected"
    assert ws.cell(2, 6).value == 2
    assert ws.cell(2, 7).value == 61.3
    assert ws.cell(2, 8).value == "sponsor 490-546s; sponsor 708-723s"


def test_save_to_excel_migrates_old_report_headers(tmp_path):
    """A report written by an older version must be extended, not corrupted."""
    from openpyxl import Workbook, load_workbook

    report = tmp_path / "old.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Platform", "Link/File", "Transcript", "Sentiment", "Keywords"])
    ws.append(["YouTube", "old-link", "old text", 0.3, "old, words"])
    wb.save(str(report))

    process.save_to_excel("Uploaded File", "new", "new text", 0.0, ["n"],
                          ad_report={"ad_count": 0, "ad_seconds": 0, "segments": []},
                          path=str(report))

    ws = load_workbook(str(report)).active
    assert [c.value for c in ws[1]] == process.REPORT_HEADERS
    # the pre-existing row is untouched
    assert ws.cell(2, 2).value == "old-link"
    assert ws.cell(2, 5).value == "old, words"
    assert ws.cell(3, 2).value == "new"


def test_transcribe_returns_segments(monkeypatch, audio_file):
    model = _FakeModel(segments=[{"start": 0.0, "end": 2.0, "text": " hi "}])
    monkeypatch.setattr(process, "_get_model", lambda name=None: model)
    result = process.transcribe(audio_file)
    assert result["text"] == "mocked transcript"
    assert result["segments"] == [{"start": 0.0, "end": 2.0, "text": "hi"}]


def test_process_video_includes_ad_report(monkeypatch, audio_file, tmp_path):
    model = _FakeModel(segments=[
        {"start": 5.0, "end": 30.0, "text": "This video is sponsored by Acme."}
    ])
    monkeypatch.setattr(process, "_get_model", lambda name=None: model)
    summary = process.process_video(file_path=audio_file,
                                    report_path=str(tmp_path / "r.xlsx"))
    assert summary["ads"]["ad_count"] == 1
    assert summary["ads"]["source"] == "heuristic"
    assert summary["language"] == "en"


def test_process_video_survives_ad_detection_failure(monkeypatch, audio_file, tmp_path):
    monkeypatch.setattr(process, "_get_model", lambda name=None: _FakeModel())
    monkeypatch.setattr(process.ads, "detect_ads",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("api down")))
    summary = process.process_video(file_path=audio_file,
                                    report_path=str(tmp_path / "r.xlsx"))
    assert summary["ads"]["ad_count"] == 0
    assert "failed" in summary["ads"]["note"].lower()


# --------------------------------------------------------------------------- #
# process_video
# --------------------------------------------------------------------------- #
def test_process_video_upload(audio_file, fake_model, tmp_path):
    report = tmp_path / "r.xlsx"
    summary = process.process_video(file_path=audio_file, report_path=str(report))
    assert summary["platform"] == "Uploaded File"
    assert summary["transcript"] == "mocked transcript"
    assert summary["model"] == process.WHISPER_MODEL
    assert os.path.exists(str(report))


def test_process_video_uses_selected_model(audio_file, fake_model, tmp_path):
    summary = process.process_video(
        file_path=audio_file, model_name="medium", report_path=str(tmp_path / "r.xlsx")
    )
    assert summary["model"] == "medium"


def test_process_video_link_downloads_and_cleans_up(fake_model, monkeypatch, tmp_path):
    downloaded = tmp_path / "yt.mp3"
    downloaded.write_bytes(b"x")
    monkeypatch.setattr(process, "download_youtube_audio", lambda url: str(downloaded))

    summary = process.process_video(link="http://x", report_path=str(tmp_path / "r.xlsx"))
    assert summary["platform"] == "YouTube"
    # temp download removed when keep_temp is False
    assert not downloaded.exists()


def test_process_video_requires_input():
    with pytest.raises(ValueError):
        process.process_video()


# --------------------------------------------------------------------------- #
# download retry logic
# --------------------------------------------------------------------------- #
def test_download_retries_then_raises(monkeypatch):
    class _FailingYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def download(self, urls):
            raise process.yt_dlp.utils.DownloadError("HTTP Error 429")

    monkeypatch.setattr(process.yt_dlp, "YoutubeDL", _FailingYDL)
    monkeypatch.setattr(process.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError):
        process.download_youtube_audio("http://example.com/video")


def test_js_runtimes_returns_mapping():
    assert isinstance(process._js_runtimes(), dict)


# --------------------------------------------------------------------------- #
# Flask routes
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    import main

    monkeypatch.setattr(
        main,
        "process_video",
        lambda **kwargs: {
            "platform": "YouTube",
            "target": "http://x",
            "transcript": "hello world",
            "sentiment": 0.4,
            "keywords": ["hello"],
            "report": "r.xlsx",
            "model": kwargs.get("model_name") or "small",
        },
    )
    main.app.config.update(TESTING=True)
    return main.app.test_client()


def test_index_get(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b'<select id="model"' in r.data


def test_default_model_is_marked_selected(client):
    import main

    body = client.get("/").data.decode()
    needle = f'value="{main.WHISPER_MODEL}" selected'
    assert needle in body, f"default model not selected in dropdown: {needle}"


def test_health(client):
    assert client.get("/health").get_json() == {"status": "ok"}


def test_post_no_input(client):
    r = client.post("/", data={})
    assert r.status_code == 200
    assert b"Provide a YouTube" in r.data


def test_post_bad_extension(client):
    r = client.post(
        "/", data={"file": (io.BytesIO(b"x"), "evil.exe")},
        content_type="multipart/form-data",
    )
    assert b"Unsupported file type" in r.data


def test_post_upload_shows_result(client):
    r = client.post(
        "/",
        data={"file": (io.BytesIO(b"abc"), "clip.mp3"), "model": "base"},
        content_type="multipart/form-data",
    )
    body = r.data.decode()
    assert r.status_code == 200
    assert "hello world" in body
    assert "<dd>base</dd>" in body


def test_post_invalid_model_falls_back(client):
    r = client.post(
        "/",
        data={"file": (io.BytesIO(b"abc"), "clip.mp3"), "model": "godzilla"},
        content_type="multipart/form-data",
    )
    assert "<dd>small</dd>" in r.data.decode()


def test_uploaded_file_is_deleted(tmp_path, monkeypatch, client):
    import main

    monkeypatch.setitem(main.app.config, "UPLOAD_FOLDER", str(tmp_path))
    client.post(
        "/",
        data={"file": (io.BytesIO(b"abc"), "clip.mp3")},
        content_type="multipart/form-data",
    )
    assert os.listdir(str(tmp_path)) == []
