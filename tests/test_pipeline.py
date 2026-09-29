"""Tests for the media analyzer pipeline and Flask front-end.

Whisper and yt-dlp are mocked so the suite runs fast and offline.
"""

import io
import os
import threading

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
    monkeypatch.setattr(process, "_transformer_sentiment", lambda text: None)
    sentiment, keywords = process.analyze_text("the quick brown fox fox fox")
    assert sentiment == pytest.approx(0.1)
    assert "fox" in keywords


def test_analyze_text_gaming_slang_reads_positive(monkeypatch):
    """The transformer must read hype slang as positive where TextBlob can't."""
    monkeypatch.setattr(process, "_transformer_sentiment",
                        lambda text: 0.9 if "insane" in text else -0.8)
    sentiment, _ = process.analyze_text(
        "bro that flash was insane, absolutely cracked play")
    assert sentiment > 0.5


def test_analyze_text_transformer_failure_falls_back_to_textblob(monkeypatch):
    """If the transformer is unavailable, TextBlob polarity drives the score."""
    calls = []

    def fake_blob(text):
        calls.append(text)
        return type("B", (), {
            "sentiment": type("S", (), {"polarity": -0.5})(),
            "noun_phrases": [],
        })()

    monkeypatch.setattr(process, "_transformer_sentiment", lambda text: None)
    monkeypatch.setattr(process, "TextBlob", fake_blob)
    sentiment, _ = process.analyze_text("This is terrible, awful and horrible.")
    assert sentiment == pytest.approx(-0.5)
    assert calls  # TextBlob was constructed


def test_analyze_text_sentiment_model_off_uses_textblob(monkeypatch):
    monkeypatch.setenv("SENTIMENT_MODEL", "off")
    monkeypatch.setattr(process, "_sentiment_pipeline", None)
    monkeypatch.setattr(process, "_sentiment_pipeline_attempted", False)
    monkeypatch.setattr(process, "TextBlob", lambda text: type("B", (), {
        "sentiment": type("S", (), {"polarity": 0.3})(),
        "noun_phrases": [],
    })())
    sentiment, _ = process.analyze_text("anything at all")
    assert sentiment == pytest.approx(0.3)


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


def test_save_to_excel_writes_served_ad_columns(tmp_path):
    from openpyxl import load_workbook

    report = tmp_path / "r.xlsx"
    served = {
        "ad_count": 1,
        "ads": [{
            "placement": "pre-roll", "advertiser": "Kurkure India",
            "destination": "instagram.com", "cta": "Know more!",
            "duration": 20.0, "skippable": True, "skip_after_s": 5.0,
            "summary": "Kurkure India -> instagram.com (Know more!) 20s skippable after 5.0s",
        }],
    }
    process.save_to_excel("YouTube", "u", "t", 0.1, ["k"], served_report=served,
                          path=str(report))
    ws = load_workbook(str(report)).active
    assert ws.cell(1, 9).value == "Served Ads"
    assert ws.cell(2, 9).value == 1
    assert "Kurkure India" in ws.cell(2, 10).value


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


def test_transcribe_retries_greedy_when_beam_search_blows_up(monkeypatch, audio_file):
    """The known Whisper beam-search tensor bug must not kill the analysis."""
    calls = []
    monkeypatch.setattr(process, "WHISPER_BEAM_SIZE_DEFAULT", 5)
    monkeypatch.setattr(process, "WHISPER_FP16_DEFAULT", False)

    class _FlakyModel:
        device = "cpu"

        def transcribe(self, path, **kwargs):
            calls.append(kwargs.get("beam_size"))
            if kwargs.get("beam_size") == 5:
                raise RuntimeError("Sizes of tensors must match except in dimension 1. "
                                   "Expected size 5 but got size 1 for tensor number 1 in the list.")
            return {"text": " recovered", "segments": [], "language": "en"}

    monkeypatch.setattr(process, "_get_model", lambda name=None, device=None: _FlakyModel())
    result = process.transcribe(audio_file)
    assert result["text"] == "recovered"
    assert calls == [5, 1]  # beam first, then the greedy retry


def test_transcribe_reraises_other_runtime_errors(monkeypatch, audio_file):
    class _BrokenModel:
        device = "cpu"

        def transcribe(self, path, **kwargs):
            raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(process, "_get_model", lambda name=None, device=None: _BrokenModel())
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        process.transcribe(audio_file)


def test_transcribe_cuda_empty_tensor_recovers_greedy_fp32_then_cpu(monkeypatch, audio_file):
    """The CUDA empty-tensor reshape bug recovers greedy+fp32, then on CPU."""
    calls = []

    class _CudaFlaky:
        device = "cuda:0"

        def transcribe(self, path, **kwargs):
            calls.append((kwargs.get("beam_size"), kwargs.get("fp16")))
            if kwargs.get("fp16") or kwargs.get("beam_size", 1) > 1:
                raise RuntimeError("cannot reshape tensor of 0 elements into shape "
                                   "[1, 0, 8, -1] because the unspecified dimension "
                                   "size -1 can be any value and is ambiguous")
            return {"text": " ok", "segments": [], "language": "en"}

    monkeypatch.setattr(
        process, "_get_model",
        lambda name=None, device=None: _CudaFlaky() if device in (None, "cuda:0")
        else _device_model("cpu"))
    result = process.transcribe(audio_file)
    assert result["text"] == "ok"
    assert calls == [(1, True), (1, False)]  # auto fp16, then fp32 retry on GPU


def test_transcribe_cuda_error_on_cpu_model_reraises(monkeypatch, audio_file):
    calls = []

    class _CpuFlaky:
        device = "cpu"

        def transcribe(self, path, **kwargs):
            calls.append(kwargs.get("beam_size"))
            raise RuntimeError("cannot reshape tensor of 0 elements into shape [1, 0, 8, -1]")

    monkeypatch.setattr(process, "_get_model", lambda name=None, device=None: _CpuFlaky())
    with pytest.raises(RuntimeError, match="cannot reshape"):
        process.transcribe(audio_file)
    assert calls == [1, 1]  # configured beam, then the greedy retry — then give up


def _device_model(device_str):
    class _Model:
        device = device_str

        def transcribe(self, path, **kwargs):
            return {"text": " hi", "segments": [], "language": "en"}

    return _Model()


def test_whisper_auto_runs_on_cuda_when_available(monkeypatch, audio_file):
    """Auto device must pick the GPU when torch sees CUDA."""
    monkeypatch.setattr(process, "WHISPER_FP16_DEFAULT", None)
    loads = []

    def fake_load(name, device="cpu"):
        loads.append(device)
        return _device_model("cuda")

    monkeypatch.setattr(process.whisper, "load_model", fake_load)
    monkeypatch.setattr(process, "_DEVICE", None)  # reset auto-detect cache
    monkeypatch.setattr(process, "_MODEL_CACHE", {})
    monkeypatch.setattr(process, "_detect_device", lambda: "cuda")

    result = process.transcribe(audio_file)
    assert loads == ["cuda"]
    assert result["device"] == "cuda"
    # fp16 auto-enables on CUDA
    assert process.WHISPER_FP16_DEFAULT is None


def test_whisper_auto_stays_on_cpu_without_cuda(monkeypatch, audio_file):
    monkeypatch.setattr(process, "WHISPER_FP16_DEFAULT", None)
    loads = []

    class _CpuModel:
        device = "cpu"

        def transcribe(self, path, **kwargs):
            # fp16 must be forced off on CPU when left on auto
            assert kwargs.get("fp16") is False
            return {"text": " hi", "segments": [], "language": "en"}

    def fake_load(name, device="cpu"):
        loads.append(device)
        return _CpuModel()

    monkeypatch.setattr(process.whisper, "load_model", fake_load)
    monkeypatch.setattr(process, "_DEVICE", None)
    monkeypatch.setattr(process, "_MODEL_CACHE", {})
    monkeypatch.setattr(process, "_detect_device", lambda: "cpu")

    result = process.transcribe(audio_file)
    assert loads == ["cpu"]
    assert result["device"] == "cpu"


def test_whisper_gpu_load_failure_falls_back_to_cpu(monkeypatch, audio_file):
    loads = []

    def fake_load(name, device="cpu"):
        loads.append(device)
        if device != "cpu":
            raise RuntimeError("CUDA error: out of memory")
        return _device_model("cpu")

    monkeypatch.setattr(process.whisper, "load_model", fake_load)
    monkeypatch.setattr(process, "_DEVICE", None)
    monkeypatch.setattr(process, "_MODEL_CACHE", {})
    monkeypatch.setattr(process, "_detect_device", lambda: "cuda")

    result = process.transcribe(audio_file)
    assert loads == ["cuda", "cpu"]
    assert result["device"] == "cpu"


def test_whisper_forced_device_env_overrides_auto(monkeypatch, audio_file):
    monkeypatch.setenv("WHISPER_DEVICE", "cpu")
    loads = []

    def fake_load(name, device="cpu"):
        loads.append(device)
        return _device_model("cpu")

    monkeypatch.setattr(process.whisper, "load_model", fake_load)
    monkeypatch.setattr(process, "_DEVICE", None)
    monkeypatch.setattr(process, "_MODEL_CACHE", {})

    process.transcribe(audio_file)
    assert loads == ["cpu"]


def test_whisper_model_cache_is_per_device(monkeypatch):
    cache = {}
    loads = []

    def fake_load(name, device="cpu"):
        loads.append((device, name))
        return _device_model(device)

    monkeypatch.setattr(process.whisper, "load_model", fake_load)
    monkeypatch.setattr(process, "_DEVICE", None)
    monkeypatch.setattr(process, "_MODEL_CACHE", cache)
    monkeypatch.setattr(process, "_detect_device", lambda: "cuda")

    process._get_model("tiny")
    process._get_model("tiny", device="cpu")
    process._get_model("tiny")  # cached hit, no new load
    assert loads == [("cuda", "tiny"), ("cpu", "tiny")]
    assert len(cache) == 2


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


def test_process_video_captures_served_ads(monkeypatch, fake_model, tmp_path):
    downloaded = tmp_path / "yt.mp3"
    downloaded.write_bytes(b"x")
    monkeypatch.setattr(process, "download_youtube_audio", lambda url: str(downloaded))
    monkeypatch.setattr(process.ads, "fetch_sponsor_segments",
                        lambda vid, timeout=15.0: [])
    monkeypatch.setattr(process.ads, "fetch_ad_breaks",
                        lambda vid, timeout=20.0: None)

    captured = {
        "available": True, "captured": True, "ad_count": 1, "ad_seconds": 20.0,
        "ads": [{"advertiser": "Kurkure India", "placement": "pre-roll",
                 "duration": 20.0, "summary": "Kurkure India 20s"}],
        "note": "Captured 1 served ad(s) totalling 20.0s from a live Chrome session.",
    }
    monkeypatch.setattr(process.served_ads, "capture_served_ads",
                        lambda url, **kwargs: captured)

    summary = process.process_video(
        link="https://youtu.be/jNQXAC9IVRw", capture_served=True,
        report_path=str(tmp_path / "r.xlsx"),
    )
    assert summary["served"]["ad_count"] == 1
    assert summary["ads"]["served_ad_count"] == 1


def test_process_video_survives_served_capture_failure(monkeypatch, fake_model, tmp_path):
    downloaded = tmp_path / "yt.mp3"
    downloaded.write_bytes(b"x")
    monkeypatch.setattr(process, "download_youtube_audio", lambda url: str(downloaded))
    monkeypatch.setattr(process.ads, "fetch_sponsor_segments",
                        lambda vid, timeout=15.0: [])
    monkeypatch.setattr(process.ads, "fetch_ad_breaks",
                        lambda vid, timeout=20.0: None)
    monkeypatch.setattr(process.served_ads, "capture_served_ads",
                        lambda url, **kwargs: (_ for _ in ()).throw(RuntimeError("no chrome")))

    summary = process.process_video(
        link="https://youtu.be/jNQXAC9IVRw", capture_served=True,
        report_path=str(tmp_path / "r.xlsx"),
    )
    assert summary["served"]["ad_count"] == 0
    assert "failed" in summary["served"]["note"].lower()


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
            "ads": {"ad_count": 0, "ad_seconds": 0, "segments": [],
                    "note": "No ad segments detected."},
            "served": {"ad_count": 0, "ads": [], "note": ""},
        },
    )
    main.app.config.update(TESTING=True)
    return main.app.test_client()


def test_index_get(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode()
    # the form fields Flask reads by name must survive any redesign
    assert 'name="link"' in body
    assert 'name="file"' in body
    assert 'name="model"' in body
    assert 'name="capture_served"' in body


def test_default_model_is_marked_selected(client):
    import main

    body = client.get("/").data.decode()
    needle = f'name="model" value="{main.WHISPER_MODEL}" checked'
    assert needle in body, f"default model not selected in segmented control: {needle}"


def test_health(client):
    assert client.get("/health").get_json() == {"status": "ok"}


def test_post_no_input(client):
    r = client.post("/", data={})
    assert r.status_code == 400
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
    assert "MODEL: <span>base</span>" in body  # the model used, in the meta line


def test_post_renders_served_ads_and_ad_breaks(monkeypatch, client):
    import main

    seen = {}

    def _fake_process(**kwargs):
        seen.update(kwargs)
        return {
            "platform": "YouTube", "target": "http://x", "transcript": "hi",
            "sentiment": 0.1, "keywords": ["hi"], "report": "r.xlsx",
            "model": "small",
            "ads": {
                "ad_count": 0, "ad_seconds": 0, "segments": [],
                "note": "No ad segments detected.",
                "ad_breaks": {
                    "checked": True, "monetized": True,
                    "breaks": [
                        {"kind": "START", "placement": "pre-roll", "start": 0.0},
                        {"kind": "MID", "placement": "mid-roll", "start": 470.0},
                    ],
                    "note": "YouTube schedules 2 ad break(s).",
                },
            },
            "served": {
                "available": True, "captured": True, "ad_count": 1,
                "ad_seconds": 20.0, "note": "Captured 1 served ad(s).",
                "ads": [{
                    "advertiser": "Kurkure India", "destination": "instagram.com",
                    "cta": "Know more!", "duration": 20.0, "placement": "pre-roll",
                    "ad_index": 1, "ad_pod_size": 2, "skippable": True,
                    "skip_after_s": 5.0, "summary": "Kurkure India -> instagram.com",
                    "content_position": 312.0, "content_duration": 1661.0,
                    "wall_started_at": "2026-09-27T12:00:05", "elapsed_s": 120.0,
                    "trigger": "seek@300", "clip": "proof/x_ad01.mp4",
                    "thumbnail": "proof/x_ad01_thumb.png",
                }],
                "manifest": "proof/x_manifest.json",
                "strategy": "sweep",
            },
        }

    monkeypatch.setattr(main, "process_video", _fake_process)
    r = client.post("/", data={"link": "http://x", "capture_served": "1"})
    body = r.data.decode()
    assert r.status_code == 200
    assert seen.get("capture_served") is True
    assert "Kurkure India" in body
    assert "instagram.com" in body
    assert "Scheduled ad breaks" in body
    assert "pre-roll" in body and "mid-roll" in body
    assert "ad 1 of 2" in body
    # hour-aware video timeline position (5:12 of 27:41) and the wall clock
    assert "5:12 of 27:41" in body
    assert "2026-09-27T12:00:05" in body
    # the manifest path is linked even though the fake report has no proof dir
    assert "proof/x_manifest.json" in body


def test_post_invalid_model_falls_back(client):
    r = client.post(
        "/",
        data={"file": (io.BytesIO(b"abc"), "clip.mp3"), "model": "godzilla"},
        content_type="multipart/form-data",
    )
    assert "MODEL: <span>small</span>" in r.data.decode()


def test_uploaded_file_is_deleted(tmp_path, monkeypatch, client):
    import main

    monkeypatch.setitem(main.app.config, "UPLOAD_FOLDER", str(tmp_path))
    client.post(
        "/",
        data={"file": (io.BytesIO(b"abc"), "clip.mp3")},
        content_type="multipart/form-data",
    )
    assert os.listdir(str(tmp_path)) == []


# --------------------------------------------------------------------------- #
# Twitch routing, the fixed-window env var, and what the UI makes of a capture
# --------------------------------------------------------------------------- #
def _twitch_served_report(**overrides):
    """A served-ad report shaped like the real Twitch reader's output."""
    report = {
        "available": True,
        "captured": True,
        "ad_count": 1,
        "ad_seconds": 30.2,
        "watched_seconds": 47.0,
        "strategy": "live",
        "source": "stream-markers",
        "channel": "gon_vl",
        "polls": 22,
        "content_duration": 3725.0,
        "manifest": "proof/gon_vl_manifest.json",
        "index": "proof/index.html",
        "creatives_index": "proof/creatives.html",
        "creative_count": 1,
        "note": ("Captured 1 served ad(s) from the stream's own ad markers over 47s "
                 "of twitch.tv/gon_vl. 1 of them filled by Twitch's own house slate."),
        "ads": [{
            "platform": "twitch", "ordinal": 1, "ad_id": "stitched-ad-1",
            "placement": "pre-roll", "format": "ssai stitched in-stream",
            "roll_type": "PREROLL", "ad_format": "standard_video_ad",
            "house_ad": True, "advertiser": None, "destination": "https://www.twitch.tv",
            "creative_id": "2474283100494", "ad_index": 1, "ad_pod_size": 1,
            "duration": 30.2, "content_position": 3606.0, "content_duration": 3725.0,
            "wall_started_at": "2026-09-28T16:21:03", "elapsed_s": 2.0,
            "detection_latency_s": 4.0, "evidence": "stream-frames",
            "frames_source": "stream-segments", "trigger": "playlist-marker",
            "creative_ref": "creative-0001", "times_seen": 2,
            "known_label": None, "frame_start": "proof/gon_vl_ad01_1_start.png",
            "frame_after": "proof/gon_vl_ad01_4_after.png", "after_frame_s": 1.0,
            "summary": ("house/filler slate (Twitch's own break card, no advertiser) "
                        "30s preroll creative 2474283100494 ad 1 of 1 repeat sighting "
                        "(creative-0001, 2 times)"),
        }],
    }
    report.update(overrides)
    return report


def test_twitch_links_are_labelled_twitch_and_capture_through_one_call(
        monkeypatch, fake_model, tmp_path):
    downloaded = tmp_path / "tw.mp3"
    downloaded.write_bytes(b"x")
    monkeypatch.setattr(process, "download_youtube_audio", lambda url: str(downloaded))
    monkeypatch.setattr(process.ads, "fetch_sponsor_segments",
                        lambda vid, timeout=15.0: [])
    monkeypatch.setattr(process.ads, "fetch_ad_breaks",
                        lambda vid, timeout=20.0: None)
    seen: dict = {}
    monkeypatch.setattr(
        process.served_ads, "capture_served_ads",
        lambda url, **kwargs: seen.update({"url": url}, **kwargs)
        or _twitch_served_report(),
    )

    summary = process.process_video(
        link="https://www.twitch.tv/gon_vl", capture_served=True,
        report_path=str(tmp_path / "r.xlsx"),
    )
    assert summary["platform"] == "Twitch"
    assert seen["url"] == "https://www.twitch.tv/gon_vl"
    assert summary["served"]["ad_count"] == 1
    assert summary["ads"]["served_ad_count"] == 1

    from openpyxl import load_workbook

    sheet = load_workbook(str(tmp_path / "r.xlsx")).active
    assert sheet.cell(2, 1).value == "Twitch"
    assert sheet.cell(2, 9).value == 1
    assert "house/filler slate" in sheet.cell(2, 10).value
    assert "1:00:06" in sheet.cell(2, 10).value


@pytest.mark.parametrize("value,expected", [
    ("45", 45.0), ("12.5", 12.5), (None, None), ("", None), ("0", None),
    ("-3", None), ("soon", None),
])
def test_served_window_env_parsing(monkeypatch, value, expected):
    monkeypatch.setattr(process, "SERVED_ADS_SECONDS", value)
    assert process._served_window() == expected


def test_served_window_env_reaches_the_capture(monkeypatch, fake_model, tmp_path):
    downloaded = tmp_path / "yt.mp3"
    downloaded.write_bytes(b"x")
    monkeypatch.setattr(process, "download_youtube_audio", lambda url: str(downloaded))
    monkeypatch.setattr(process.ads, "fetch_sponsor_segments",
                        lambda vid, timeout=15.0: [])
    monkeypatch.setattr(process.ads, "fetch_ad_breaks",
                        lambda vid, timeout=20.0: None)
    monkeypatch.setattr(process, "SERVED_ADS_SECONDS", "45")
    seen: dict = {}
    monkeypatch.setattr(process.served_ads, "capture_served_ads",
                        lambda url, **kwargs: seen.update(kwargs)
                        or _twitch_served_report())

    process.process_video(link="https://youtu.be/x", capture_served=True,
                          keep_temp=True, report_path=str(tmp_path / "r.xlsx"))
    assert seen["watch_seconds"] == 45.0

    # an explicit argument still wins over the environment
    process.process_video(link="https://youtu.be/x", capture_served=True,
                          keep_temp=True, served_watch_seconds=10.0,
                          report_path=str(tmp_path / "r.xlsx"))
    assert seen["watch_seconds"] == 10.0


def test_post_renders_a_twitch_capture(monkeypatch, client):
    import main

    monkeypatch.setattr(
        main, "process_video",
        lambda **kwargs: {
            "platform": "Twitch", "target": "https://www.twitch.tv/gon_vl",
            "transcript": "hi", "sentiment": 0.0, "keywords": [],
            "report": "r.xlsx", "model": kwargs.get("model_name") or "small",
            "ads": {"ad_count": 0, "ad_seconds": 0, "segments": [],
                    "note": "No ad segments detected."},
            "served": _twitch_served_report(),
        },
    )
    body = client.post(
        "/", data={"link": "https://www.twitch.tv/gon_vl", "capture_served": "1"},
    ).data.decode()

    assert "Twitch" in body
    assert "twitch.tv/gon_vl" in body          # which channel was watched
    assert "stream-markers" in body             # where the ad reading came from
    assert "live watch" in body
    assert "Twitch house slate" in body         # named, not "Unknown advertiser"
    assert "ssai stitched in-stream" in body    # the ad format
    assert "ad 1 of 1" in body
    assert "proof/gon_vl_manifest.json" in body
    assert "1:00:06 of 1:02:05" in body           # hour-aware timeline position


# The web UI renders a served ad field by field, so anything the capture starts
# reporting has to be added there too. These three pin the fields that used to
# be missing from the old UI (they were xfails until the Broadcast Log sheet).
def _post_twitch(monkeypatch, client, report=None):
    import main
    import process as process_module

    served = report or _twitch_served_report()
    # the real pipeline runs this enrichment before rendering; mirror it
    process_module._enrich_served_ads(served)
    monkeypatch.setattr(
        main, "process_video",
        lambda **kwargs: {
            "platform": "Twitch", "target": "https://www.twitch.tv/gon_vl",
            "transcript": "hi", "sentiment": 0.0, "keywords": [],
            "report": "r.xlsx", "model": "small",
            "ads": {"ad_count": 0, "ad_seconds": 0, "segments": [], "note": "",
                    "ad_breaks": None},
            "served": served,
        },
    )
    return client.post("/", data={"link": "https://www.twitch.tv/gon_vl"}).data.decode()


def test_post_links_to_the_twitch_creative_gallery(monkeypatch, client):
    body = _post_twitch(monkeypatch, client)
    assert "proof/creatives.html" in body
    assert "creative-0001" in body
    assert "2 times" in body


def test_post_shows_a_labelled_twitch_creative(monkeypatch, client):
    report = _twitch_served_report()
    report["ads"][0]["known_label"] = "Amazon deals"
    report["ads"][0]["advertiser"] = "amazon.in"
    report["ads"][0]["house_ad"] = False
    report["ads"][0]["summary"] = "identified as Amazon deals via amazon.in 30s"
    body = _post_twitch(monkeypatch, client, report)
    assert "amazon.in" in body  # the parts that are shown today
    assert "Amazon deals" in body
    assert "identified as Amazon deals via amazon.in 30s" in body


def test_post_shows_the_decoded_ad_frames(monkeypatch, client):
    body = _post_twitch(monkeypatch, client)
    assert "proof/gon_vl_ad01_1_start.png" in body
    assert "stream-frames" in body  # how much evidence there is, and from where


def test_post_twitch_proof_images_are_served_over_http(monkeypatch, client, tmp_path):
    """Frame PNGs under proof/ must be fetchable for the report's <img>."""
    import main
    import served_ads as served_ads_module

    proof = tmp_path / "proof"
    proof.mkdir()
    (proof / "gon_vl_ad01_1_start.png").write_bytes(b"fake-png")
    monkeypatch.setattr(served_ads_module, "DEFAULT_PROOF_DIR", str(proof))

    r = client.get("/proof/gon_vl_ad01_1_start.png")
    assert r.status_code == 200
    assert r.data == b"fake-png"
    assert client.get("/proof/../main.py").status_code in (403, 404)


# --------------------------------------------------------------------------- #
# The /analyze + /status job model: the page polls real pipeline stages
# --------------------------------------------------------------------------- #
def _wait_for_job(client, job_id, timeout=15.0):
    import time

    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = client.get(f"/status/{job_id}")
        assert r.status_code == 200
        last = r.get_json()
        if last["state"] in {"done", "error"}:
            return last
        time.sleep(0.05)
    return last


def test_analyze_job_runs_to_done_and_reports_stages(monkeypatch, client):
    import main
    import process as process_module

    def _fake_process(**kwargs):
        # a real pipeline would announce these; mimic it
        process_module.report_stage(kwargs["job_id"], "fetch", "downloading")
        process_module.report_stage(kwargs["job_id"], "transcribe", "whisper")
        process_module.report_stage(kwargs["job_id"], "ads", "reading markers")
        return {
            "platform": "YouTube", "target": "http://x", "title": "t",
            "transcript": "hello from the job", "sentiment": 0.2,
            "keywords": ["k"], "report": "r.xlsx", "model": "small",
            "ads": {"ad_count": 0, "ad_seconds": 0, "segments": [],
                    "note": ""},
            "served": {"ad_count": 0, "ads": [], "note": ""},
        }

    monkeypatch.setattr(main, "process_video", _fake_process)
    r = client.post("/analyze", data={"link": "http://x"})
    assert r.status_code == 200
    job_id = r.get_json()["job"]

    # while it runs, the poller exposes the announced stage; afterwards, done
    running = client.get(f"/status/{job_id}").get_json()
    if running["state"] == "running":
        assert running["label"] in {"01/03 FETCHING AUDIO", "02/03 TRANSCRIBING",
                                    "03/03 READING ADS"}

    final = _wait_for_job(client, job_id)
    assert final["state"] == "done"
    assert "hello from the job" in final["html"]


def test_analyze_running_status_reports_stage_labels(monkeypatch, client):
    import main
    import process as process_module

    release = threading.Event()

    def _slow_process(**kwargs):
        release.wait(10)
        return {"platform": "YouTube", "target": "x", "title": "t",
                "transcript": "t", "sentiment": 0.0, "keywords": [],
                "report": "r.xlsx", "model": "small",
                "ads": {"ad_count": 0, "ad_seconds": 0, "segments": [], "note": ""},
                "served": {"ad_count": 0, "ads": [], "note": ""}}

    monkeypatch.setattr(main, "process_video", _slow_process)
    job_id = client.post("/analyze", data={"link": "http://x"}).get_json()["job"]

    process_module.report_stage(job_id, "transcribe", "whisper small on clip.mp3")
    r = client.get(f"/status/{job_id}").get_json()
    assert r["state"] == "running"
    assert r["label"] == "02/03 TRANSCRIBING"
    assert r["percent"] == 66.6
    assert r["detail"] == "whisper small on clip.mp3"

    release.set()
    final = _wait_for_job(client, job_id)
    assert final["state"] == "done"


def test_analyze_job_error_surfaces_reason(monkeypatch, client):
    import main

    def _boom(**kwargs):
        raise RuntimeError("yt-dlp exploded")

    monkeypatch.setattr(main, "process_video", _boom)
    job_id = client.post("/analyze", data={"link": "http://x"}).get_json()["job"]
    final = _wait_for_job(client, job_id)
    assert final["state"] == "error"
    assert "yt-dlp exploded" in final["error"]
    assert "Processing failed" in final["html"]


def test_analyze_rejects_bad_input(client):
    assert client.post("/analyze", data={}).status_code == 400
    assert client.post("/analyze", data={"link": "x", "model": "godzilla"}
                      ).status_code == 200  # model falls back silently


def test_status_unknown_job_404s(client):
    assert client.get("/status/nope").status_code == 404


def test_report_footer_links_the_workbook(monkeypatch, client):
    import main

    monkey_result = {
        "platform": "YouTube", "target": "x", "title": "t",
        "transcript": "t", "sentiment": 0.0, "keywords": [],
        "report": str(main.REPORT_PATH), "model": "small",
        "ads": {"ad_count": 0, "ad_seconds": 0, "segments": [], "note": ""},
        "served": {"ad_count": 0, "ads": [], "note": ""},
    }
    monkeypatch.setattr(main, "process_video", lambda **kw: monkey_result)
    body = client.post("/", data={"link": "http://x"}).data.decode()
    name = main.basename_filter(main.REPORT_PATH)
    assert f'href="/downloads/{name}"' in body
    assert "REPORT SAVED TO" in body


def test_basename_filter_handles_windows_paths():
    import main

    assert main.basename_filter(r"C:\Users\91966\Media\media_report.xlsx") \
        == "media_report.xlsx"
    assert main.basename_filter("a/b/c.xlsx") == "c.xlsx"
    assert main.basename_filter(None) == ""


# --------------------------------------------------------------------------- #
# Log history: finished analyses land in history/ and re-open from the form
# --------------------------------------------------------------------------- #
@pytest.fixture
def history_dir(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "HISTORY_DIR", str(tmp_path / "history"))
    return str(tmp_path / "history")


def _history_result():
    return {
        "platform": "Twitch", "target": "https://www.twitch.tv/gon_vl",
        "title": "gon_vl", "transcript": "t", "sentiment": 0.31,
        "keywords": ["k"], "report": "r.xlsx", "model": "small",
        "ads": {"ad_count": 2, "ad_seconds": 30, "segments": [], "note": ""},
        "served": {"ad_count": 1, "ads": [], "note": ""},
    }


def test_finished_job_is_archived_and_listed(monkeypatch, client, history_dir):
    import main

    monkeypatch.setattr(main, "process_video", lambda **kw: _history_result())
    job_id = client.post("/analyze", data={"link": "https://www.twitch.tv/gon_vl"}
                         ).get_json()["job"]
    final = _wait_for_job(client, job_id)
    assert final["state"] == "done"

    # archived on disk and listed on the input page
    assert os.path.exists(os.path.join(history_dir, f"{job_id}.html"))
    body = client.get("/").data.decode()
    assert "PREVIOUS LOGS" in body
    assert f"/logs/{job_id}" in body
    assert "gon_vl" in body

    # and the archived page opens with the report sheet in it
    page = client.get(f"/logs/{job_id}")
    assert page.status_code == 200
    assert b"BROADCAST RECORD" in page.data


def test_sync_post_is_archived_too(monkeypatch, client, history_dir):
    import main

    monkeypatch.setattr(main, "process_video", lambda **kw: _history_result())
    client.post("/", data={"link": "https://www.twitch.tv/gon_vl"})
    entries = main._history_load()
    assert len(entries) == 1
    assert entries[0]["platform"] == "Twitch"
    assert entries[0]["served"] == 1


def test_failed_job_is_archived_as_failed(monkeypatch, client, history_dir):
    import main

    def _boom(**kw):
        raise RuntimeError("no such channel")

    monkeypatch.setattr(main, "process_video", _boom)
    job_id = client.post("/analyze", data={"link": "http://x"}).get_json()["job"]
    assert _wait_for_job(client, job_id)["state"] == "error"

    body = client.get("/").data.decode()
    assert "FAILED" in body
    row = [e for e in main._history_load() if e["id"] == job_id][0]
    assert row["state"] == "error"


def test_history_keeps_only_the_newest(monkeypatch, client, history_dir):
    import main

    monkeypatch.setattr(main, "HISTORY_MAX", 3)
    monkeypatch.setattr(main, "process_video", lambda **kw: _history_result())
    kept = []
    for _ in range(5):
        job_id = client.post("/analyze", data={"link": "http://x"}
                             ).get_json()["job"]
        _wait_for_job(client, job_id)
        kept.append(job_id)

    ids = [e["id"] for e in main._history_load()]
    assert ids == list(reversed(kept[-3:]))  # newest first, capped at 3
    for gone in kept[:2]:
        assert not os.path.exists(os.path.join(history_dir, f"{gone}.html"))


def test_logs_clear_empties_the_archive(monkeypatch, client, history_dir):
    import main

    monkeypatch.setattr(main, "process_video", lambda **kw: _history_result())
    job_id = client.post("/analyze", data={"link": "http://x"}).get_json()["job"]
    _wait_for_job(client, job_id)
    assert main._history_load()

    r = client.post("/logs/clear")
    assert r.status_code == 302
    assert main._history_load() == []
    assert not os.path.exists(os.path.join(history_dir, f"{job_id}.html"))


def test_open_log_rejects_bad_ids(client):
    assert client.get("/logs/../secret").status_code in (403, 404)
    assert client.get("/logs/not-a-job-id").status_code == 404
    assert client.get("/logs/" + "a" * 32).status_code == 404  # well-formed, unknown


def test_uploaded_file_removed_after_async_job(client, tmp_path, monkeypatch):
    import main

    monkeypatch.setitem(main.app.config, "UPLOAD_FOLDER", str(tmp_path))
    done = threading.Event()

    def _fake_process(**kwargs):
        done.set()
        return {"platform": "Uploaded File", "target": "x", "title": "t",
                "transcript": "t", "sentiment": 0.0, "keywords": [],
                "report": "r.xlsx", "model": "small",
                "ads": {"ad_count": 0, "ad_seconds": 0, "segments": [], "note": ""},
                "served": {"ad_count": 0, "ads": [], "note": ""}}

    monkeypatch.setattr(main, "process_video", _fake_process)
    job_id = client.post(
        "/analyze",
        data={"file": (io.BytesIO(b"abc"), "clip.mp3")},
        content_type="multipart/form-data",
    ).get_json()["job"]
    assert _wait_for_job(client, job_id)["state"] == "done"
    assert os.listdir(str(tmp_path)) == []  # upload cleaned up by the worker
