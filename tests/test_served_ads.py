"""Tests for live served-ad capture.

The browser, clock and screenshots are all faked: these tests exercise the
capture state machine (pod splitting, skippable detection, placement
classification, timeline/seek sweep, proof clips, graceful failure) without
opening Chrome or hitting YouTube.
"""

import json
import os

import pytest

import served_ads


class _FakeTime:
    """Deterministic clock: every call/sleep advances virtual time."""

    def __init__(self, step=1.0):
        self.now = 0.0
        self.step = step

    def time(self):
        self.now += self.step
        return self.now

    def sleep(self, _seconds):
        self.now += self.step


class _FakeDriver:
    def __init__(self, shot_root=None):
        self.quit_called = False
        self.visited = None
        self.shots = 0
        self.shot_root = shot_root

    def set_page_load_timeout(self, _seconds):
        pass

    def get(self, url):
        self.visited = url

    def execute_script(self, *_args):
        return None

    def find_elements(self, *_args):
        return []

    def save_screenshot(self, path):
        self.shots += 1
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n fake")

    def quit(self):
        self.quit_called = True


def _snap(showing, advertiser=None, destination=None, cta=None, index=None,
          pod=None, duration=None, skippable=False, content_time=0.0,
          content_duration=None, rect=None, ad_time=None, paused=False):
    return {
        "showing": showing,
        "advertiser": advertiser,
        "destination": destination,
        "cta": cta,
        "ad_index": index,
        "ad_pod_size": pod,
        "ad_time": ad_time,
        "ad_duration": duration,
        "skippable": skippable,
        "skip_text": None,
        "content_time": content_time,
        "content_duration": content_duration,
        "ended": False,
        "paused": paused,
        "player_rect": rect,
    }


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Wire up a fake browser + clock and return the scripted snapshots list."""
    script: list = []
    driver = _FakeDriver()
    fake_time = _FakeTime()

    monkeypatch.setattr(served_ads, "availability", lambda: (True, ""))
    monkeypatch.setattr(served_ads, "_new_driver", lambda headless: driver)
    monkeypatch.setattr(served_ads, "time", fake_time)
    monkeypatch.setattr(served_ads, "DEFAULT_PROOF_DIR", str(tmp_path / "proof"))
    monkeypatch.setattr(
        served_ads, "_poll",
        lambda _driver: script.pop(0) if script else None,
    )
    return script, driver


# --------------------------------------------------------------------------- #
# availability / graceful degradation
# --------------------------------------------------------------------------- #
def test_availability_returns_tuple():
    ok, reason = served_ads.availability()
    assert isinstance(ok, bool)
    assert isinstance(reason, str)


def test_capture_reports_unavailable_without_raising(monkeypatch):
    monkeypatch.setattr(served_ads, "availability", lambda: (False, "no chrome"))
    report = served_ads.capture_served_ads("https://youtu.be/x")
    assert report["available"] is False
    assert report["captured"] is False
    assert report["ad_count"] == 0
    assert "unavailable" in report["note"].lower()
    assert "no chrome" in report["note"]


def test_capture_handles_browser_failure(monkeypatch):
    monkeypatch.setattr(served_ads, "availability", lambda: (True, ""))
    monkeypatch.setattr(served_ads, "_new_driver",
                        lambda headless: (_ for _ in ()).throw(RuntimeError("chrome busy")))
    report = served_ads.capture_served_ads("https://youtu.be/x")
    assert report["available"] is True
    assert report["captured"] is False
    assert "failed" in report["note"].lower()


# --------------------------------------------------------------------------- #
# core capture behaviour (window strategy)
# --------------------------------------------------------------------------- #
def test_capture_records_pre_roll_ad(patched):
    script, driver = patched
    script.extend([
        _snap(True, "Kurkure India", "instagram.com", "Know more!",
              index=1, pod=2, duration=20.0, content_time=0.0,
              content_duration=1661.0, ad_time=0.4),
        _snap(True, "Kurkure India", "instagram.com", "Know more!",
              index=1, pod=2, duration=20.0, skippable=True, content_time=0.0),
        _snap(False, content_time=0.4, content_duration=1661.0),
        _snap(False, content_time=15.0, content_duration=1661.0),
    ])

    report = served_ads.capture_served_ads("https://youtu.be/x", watch_seconds=200,
                                           record=False)

    assert driver.visited == "https://youtu.be/x"
    assert driver.quit_called is True
    assert report["captured"] is True
    assert report["ad_count"] == 1
    assert report["ad_seconds"] == 20.0
    assert report["strategy"] == "window"

    ad = report["ads"][0]
    assert ad["advertiser"] == "Kurkure India"
    assert ad["destination"] == "instagram.com"
    assert ad["cta"] == "Know more!"
    assert ad["placement"] == "pre-roll"
    assert ad["skippable"] is True
    assert ad["skip_after_s"] is not None
    assert ad["ad_pod_size"] == 2
    assert ad["duration"] == 20.0
    assert ad["content_duration"] == 1661.0
    # detection latency: the player was already 0.4s into the ad when we saw it
    assert ad["first_seen_media_time"] == 0.4
    assert ad["detection_latency_s"] == 0.4
    assert "noticed 0.4s into the ad" in ad["summary"]
    # timestamps: wall clock, session offset and video timeline
    assert ad["wall_started_at"]
    assert ad["elapsed_s"] >= 0
    assert ad["content_position"] == 0.0
    assert "Kurkure India" in ad["summary"]
    assert "Captured 1 served ad" in report["note"]


def test_first_seen_media_time_keeps_the_first_poll(monkeypatch):
    """Detection latency is measured once, at the moment the ad is first seen."""
    tracker = served_ads._Tracker()
    tracker.update(_snap(True, "Acme", "acme.com", ad_time=0.6, content_time=0.0), 1.0)
    tracker.update(_snap(True, "Acme", "acme.com", ad_time=2.5), 2.0)
    tracker.update(_snap(False, content_time=1.0), 3.0)
    assert tracker.ads[0]["first_seen_media_time"] == 0.6
    assert tracker.ads[0]["detection_latency_s"] == 0.6


def test_detection_latency_rejects_a_content_playhead_reading():
    """A seek-served break can be spotted while the playhead still reads the
    content position; that is not an ad latency and must not be reported as one."""
    assert served_ads._detection_latency(
        {"first_seen_media_time": 120.0, "duration": 6.2}
    ) is None
    assert served_ads._detection_latency(
        {"first_seen_media_time": 1.2, "duration": 20.3}
    ) == 1.2
    # a reading that is exactly the break's position is the content playhead
    assert served_ads._detection_latency(
        {"first_seen_media_time": 120.0, "duration": 1661.0,
         "content_position": 120.0, "placement": "mid-roll"}
    ) is None
    # a pre-roll starts at 0: caught at the start, which is a real latency
    assert served_ads._detection_latency(
        {"first_seen_media_time": 0.0, "duration": 20.0,
         "content_position": 0.0, "placement": "pre-roll"}
    ) == 0.0
    assert served_ads._detection_latency({"first_seen_media_time": None}) is None
    # no duration to compare against: report what the player said
    assert served_ads._detection_latency({"first_seen_media_time": 2.0}) == 2.0


def test_capture_splits_an_ad_pod_into_separate_ads(patched):
    script, _ = patched
    script.extend([
        _snap(True, "Advertiser A", "a.com", index=1, pod=2, duration=10.0),
        _snap(True, "Advertiser B", "b.com", index=2, pod=2, duration=15.0),
        _snap(False, content_time=1.0),
    ])

    report = served_ads.capture_served_ads("https://youtu.be/x", watch_seconds=200,
                                           record=False)
    assert report["ad_count"] == 2
    assert [a["advertiser"] for a in report["ads"]] == ["Advertiser A", "Advertiser B"]
    assert report["ad_seconds"] == 25.0


def test_capture_classifies_midroll_from_playback_position(patched):
    script, _ = patched
    script.extend([
        _snap(False, content_time=0.0),
        _snap(False, content_time=312.0),
        _snap(True, "Midroll Co", "mid.com", duration=8.0, content_time=312.0),
        _snap(False, content_time=320.0),
    ])

    report = served_ads.capture_served_ads("https://youtu.be/x", watch_seconds=200,
                                           record=False)
    assert report["ad_count"] == 1
    assert report["ads"][0]["placement"] == "mid-roll"
    assert report["ads"][0]["content_position"] == 312.0
    assert served_ads.format_timeline(report["ads"][0]) == "5:12"


def test_capture_reports_no_ads_served(patched):
    script, _ = patched
    script.extend([_snap(False, content_time=float(i)) for i in range(5)])

    report = served_ads.capture_served_ads("https://youtu.be/x", watch_seconds=200,
                                           record=False)
    assert report["captured"] is False
    assert report["ad_count"] == 0
    assert report["ad_seconds"] == 0.0
    assert "no ad" in report["note"].lower()


# --------------------------------------------------------------------------- #
# sweep strategy
# --------------------------------------------------------------------------- #
def test_sweep_watch_seeks_across_the_whole_timeline(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(served_ads, "time", fake)
    seeks = []
    monkeypatch.setattr(served_ads, "_seek", lambda driver, pos: seeks.append(pos))
    monkeypatch.setattr(
        served_ads, "_poll",
        lambda _d: _snap(False, content_time=0.0, content_duration=360.0),
    )

    tracker = served_ads._Tracker()
    started = fake.time()
    info = served_ads._sweep_watch(object(), tracker, started + 100000, started)

    assert info["content_duration"] == 360.0
    # stepped positions, plus a final stop near the end so the tail is covered
    assert seeks == [120.0, 240.0, 340.0]
    assert info["seek_stops"] == len(seeks)


def test_sweep_stays_put_until_a_served_ad_finishes(monkeypatch):
    """The sweep must not seek away mid-ad: the return to content is evidence."""
    fake = _FakeTime()
    monkeypatch.setattr(served_ads, "time", fake)
    monkeypatch.setattr(served_ads, "_seek", lambda driver, pos: None)
    monkeypatch.setattr(served_ads, "PRE_ROLL_SECONDS", 6.0)
    monkeypatch.setattr(served_ads, "SEEK_DWELL_SECONDS", 4.0)

    seen = {"n": 0}

    def _poll(_driver):
        seen["n"] += 1
        # an ad that outlasts the plain dwell window, then content resumes
        return _snap(seen["n"] <= 8, "Acme", "acme.com", content_time=120.0,
                     content_duration=600.0, duration=30.0, ad_time=0.2)

    monkeypatch.setattr(served_ads, "_poll", _poll)
    tracker = served_ads._Tracker()
    started = fake.time()
    served_ads._sweep_watch(object(), tracker, started + 100000, started)

    assert tracker.ads, "the served ad should have been recorded"
    assert tracker.ads[0]["observed_seconds"] > served_ads.SEEK_DWELL_SECONDS


def test_watch_window_gives_up_on_an_ad_that_never_finishes(monkeypatch):
    """A stuck ad-showing state must not hold the window (and the budget) open."""
    fake = _FakeTime()
    monkeypatch.setattr(served_ads, "time", fake)
    monkeypatch.setattr(served_ads, "AD_WAIT_SECONDS", 6.0)
    monkeypatch.setattr(
        served_ads, "_poll",
        lambda _d: _snap(True, "Acme", "acme.com", content_time=0.0,
                         duration=30.0, paused=True),
    )
    plays = []
    monkeypatch.setattr(served_ads, "_play", lambda driver: plays.append(1))

    tracker = served_ads._Tracker()
    started = fake.time()
    served_ads._watch_window(object(), tracker, started + 60.0, started, 3.0)

    assert fake.time() - started < 40, "the window should have closed on its own"
    assert plays, "the stuck ad should have been nudged back into playing"


def test_sweep_positions_cover_the_tail():
    assert served_ads._sweep_positions(360.0, 120.0) == [120.0, 240.0, 340.0]
    # exactly divisible runtime still gets an end-of-video stop
    assert served_ads._sweep_positions(240.0, 120.0) == [120.0, 220.0]
    # very short videos get a single near-end stop
    assert served_ads._sweep_positions(30.0, 120.0) == [10.0]


def test_sweep_marks_seek_triggered_ads_as_midroll(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(served_ads, "time", fake)
    tracker = served_ads._Tracker()
    monkeypatch.setattr(served_ads, "_seek", lambda driver, pos: None)
    queue = [_snap(False, content_time=0.0, content_duration=600.0)]
    monkeypatch.setattr(served_ads, "_poll",
                        lambda _d: queue.pop(0) if queue else _snap(
                            False, content_time=0.0, content_duration=600.0))

    started = fake.time()
    served_ads._sweep_watch(object(), tracker, started + 100000, started)
    tracker.trigger = "seek@120"
    tracker.last_content_time = 120.0   # the sweep sought past the 2:00 break
    tracker.update(_snap(True, "Seek Ad Co", "seek.com", content_time=120.0,
                         duration=10.0), 100.0)
    tracker.close(110.0)

    assert tracker.ads[0]["placement"] == "mid-roll"
    assert tracker.ads[0]["trigger"] == "seek@120"
    assert tracker.ads[0]["content_position"] == 120.0


# --------------------------------------------------------------------------- #
# proof clips
# --------------------------------------------------------------------------- #
def _record_ad(tmp_path, monkeypatch, *, post=(), freeze=False):
    """Drive a recorder through a content -> ad -> content sequence."""
    monkeypatch.setattr(served_ads, "_ffmpeg_available", lambda: False)
    driver = _FakeDriver()
    recorder = served_ads._ClipRecorder(str(tmp_path / "proof"), "vid12345678")
    rect = {"x": 10, "y": 10, "w": 1280.0, "h": 720.0}

    for t in (0.0, 1.0, 2.0):              # content before the ad
        recorder.tick(driver, t, False, rect)
    recorder.begin(rect, 3.0)              # the ad is first noticed here
    for t in (3.0, 4.0, 5.0):              # the ad plays
        recorder.tick(driver, t, True, rect)
    ad = {"advertiser": "Kurkure India"}
    recorder.finish(ad, 1, 5.0)            # the ad is gone
    if freeze:                             # the sweep is about to seek away
        recorder.freeze()
    for t in post:                         # filming continues past the ad
        recorder.tick(driver, t, False, rect)
    recorder.flush_ready(99.0, force=True)
    return recorder, ad


def test_recorder_keeps_frames_before_during_and_after_an_ad(tmp_path, monkeypatch):
    _, ad = _record_ad(tmp_path, monkeypatch, post=(6.0, 6.5))

    assert os.path.basename(ad["frame_before"]) == "vid12345678_ad01_kurkure-india_0_before.png"
    assert os.path.basename(ad["frame_start"]) == "vid12345678_ad01_kurkure-india_1_start.png"
    assert os.path.basename(ad["frame_mid"]) == "vid12345678_ad01_kurkure-india_2_mid.png"
    assert os.path.basename(ad["frame_end"]) == "vid12345678_ad01_kurkure-india_3_end.png"
    assert os.path.basename(ad["frame_after"]) == "vid12345678_ad01_kurkure-india_4_after.png"
    for label in served_ads._LABELS:
        assert os.path.exists(ad["frame_" + label])
    # the labelled thumbnail is the ad itself, not the content that preceded it
    assert ad["thumbnail"] == ad["frame_start"]
    # one second of lead-in was kept, and the return was caught a second later
    assert ad["before_frame_s"] == 1.0
    assert ad["after_frame_s"] == 1.0
    assert ad["ad_started_at"] == 3.0 and ad["ad_ended_at"] == 5.0
    # three lead-in frames, three ad frames and two after it
    assert ad["frames"] == 8 and ad["ad_frames"] == 3
    assert ad["clip"] is None             # ffmpeg disabled in this test


def test_checkpoint_gives_a_seek_triggered_ad_a_lead_in_frame(tmp_path, monkeypatch):
    """A break served by a seek never draws a content frame first: keep the last one."""
    monkeypatch.setattr(served_ads, "_ffmpeg_available", lambda: False)
    driver = _FakeDriver()
    recorder = served_ads._ClipRecorder(str(tmp_path / "proof"), "v")
    tracker = served_ads._Tracker(recorder=recorder, driver=driver)
    tracker.last_content_time = 40.0

    tracker.checkpoint(driver, 1.0)          # content frame, just before the jump
    tracker.trigger = "seek@120"
    tracker.freeze()
    tracker.update(_snap(True, "Acme", "acme.com", content_time=120.0,
                         duration=10.0, ad_time=0.0), 2.0)
    tracker.update(_snap(False, content_time=121.0), 3.5)
    tracker.close(6.0)

    ad = tracker.ads[0]
    assert ad["content_position"] == 120.0
    assert ad["frame_before"].endswith("_0_before.png")
    assert ad["before_frame_s"] == 1.0
    assert ad["frame_after"]                 # the content that followed the ad


def test_tracker_checkpoints_only_when_no_ad_is_on_screen(tmp_path, monkeypatch):
    monkeypatch.setattr(served_ads, "_ffmpeg_available", lambda: False)
    driver = _FakeDriver()
    recorder = served_ads._ClipRecorder(str(tmp_path / "proof"), "v")
    tracker = served_ads._Tracker(recorder=recorder, driver=driver)
    tracker.update(_snap(True, "Acme", "acme.com", content_time=0.0), 1.0)

    tracker.checkpoint(driver, 2.0)          # mid-ad: must be ignored
    assert recorder.checkpoint_frame is None


def test_recorder_freeze_stops_post_roll_frames_after_a_seek(tmp_path, monkeypatch):
    """Frames from a different timeline position must not be sold as "after"."""
    _, ad = _record_ad(tmp_path, monkeypatch, post=(6.0,), freeze=True)
    assert ad.get("frame_after") is None
    assert ad["after_frame_s"] is None
    assert os.path.exists(ad["frame_before"])


def test_recorder_prunes_scratch_frames_it_no_longer_needs(tmp_path, monkeypatch):
    monkeypatch.setattr(served_ads, "_ffmpeg_available", lambda: False)
    monkeypatch.setattr(served_ads, "BUFFER_FRAMES", 2)
    driver = _FakeDriver()
    recorder = served_ads._ClipRecorder(str(tmp_path / "proof"), "v")
    for t in range(6):
        recorder.tick(driver, float(t), False)
    assert len(recorder.tracked) <= 3      # buffer + the frame just written


def test_recorder_saves_manifest_with_frame_timings(tmp_path, monkeypatch):
    monkeypatch.setattr(served_ads, "_ffmpeg_available", lambda: False)
    recorder, ad = _record_ad(tmp_path, monkeypatch, post=(6.0,))
    ad["first_seen_media_time"] = 0.3
    ad["detection_latency_s"] = 0.3

    manifest = served_ads._write_manifest(
        recorder, "https://youtu.be/x", "vid12345678",
        [ad], {"watched_seconds": 12.0}, "sweep",
    )
    assert manifest and os.path.exists(manifest)
    payload = json.loads(open(manifest, encoding="utf-8").read())
    assert payload["video_id"] == "vid12345678"
    recorded = payload["ads"][0]
    assert recorded["advertiser"] == "Kurkure India"
    assert recorded["frame_before"] and recorded["frame_after"]
    assert recorded["detection_latency_s"] == 0.3


def test_write_index_strips_the_labelled_frames(tmp_path):
    recorder = served_ads._ClipRecorder(str(tmp_path / "proof"), "vid12345678")
    ads = [{
        "placement": "pre-roll", "content_position": 0.0, "summary": "Acme 10s",
        "wall_started_at": "2026-09-27T12:00:00",
        "clip": str(tmp_path / "proof" / "vid12345678_ad01_acme.mp4"),
        "thumbnail": str(tmp_path / "proof" / "vid12345678_ad01_acme_1_start.png"),
        "frame_before": "vid12345678_ad01_acme_0_before.png",
        "frame_start": "vid12345678_ad01_acme_1_start.png",
        "frame_after": "vid12345678_ad01_acme_4_after.png",
        "before_frame_s": 1.0, "after_frame_s": 1.0, "detection_latency_s": 0.4,
    }]
    path = served_ads._write_index(recorder, "https://youtu.be/x", ads)
    assert path and os.path.exists(path)
    body = open(path, encoding="utf-8").read()
    assert "vid12345678_ad01_acme.mp4" in body
    assert "pre-roll at 0:00" in body
    assert "vid12345678_ad01_acme_0_before.png" in body
    assert "content 1.0s before" in body
    assert "content 1.0s after" in body
    assert "first ad frame detected (0.4s into the ad)" in body
    assert served_ads._write_index(recorder, "u", []) is None


def test_recorder_frames_are_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr(served_ads, "_ffmpeg_available", lambda: False)
    recorder = served_ads._ClipRecorder(str(tmp_path / "proof"), "v")
    driver = _FakeDriver()
    recorder.tick(driver, 0.0, False)
    recorder.begin(None, 1.0)
    recorder.tick(driver, 1.0, True)
    frames_dir = recorder.frames_dir
    recorder.finish({"advertiser": "X"}, 1, 2.0)
    recorder.close(3.0)
    assert not os.path.exists(frames_dir)


def test_capture_with_recording_writes_manifest(patched, tmp_path, monkeypatch):
    monkeypatch.setattr(served_ads, "_ffmpeg_available", lambda: False)
    script, _ = patched
    script.extend([
        _snap(False, content_time=0.0, content_duration=100.0),   # page settles
        _snap(False, content_time=0.5, content_duration=100.0),
        _snap(True, "Acme", "acme.com", duration=6.0, content_time=0.5,
              content_duration=100.0, rect={"x": 0, "y": 0, "w": 1200.0, "h": 675.0}),
        _snap(False, content_time=1.0, content_duration=100.0),   # content returns
        _snap(False, content_time=2.0, content_duration=100.0),
    ])
    report = served_ads.capture_served_ads("https://youtu.be/x", watch_seconds=200,
                                           record=True, video_id="vid12345678")
    assert report["proof_dir"]
    assert report["manifest"] and os.path.exists(report["manifest"])
    ad = report["ads"][0]
    assert ad["thumbnail"] and ad["thumbnail"].endswith("_1_start.png")
    assert ad["frame_before"]                 # content that the ad interrupted
    assert ad["frame_after"]                  # the content that followed it
    body = open(report["manifest"], encoding="utf-8").read()
    assert "frame_start" in body and "frame_before" in body


def test_warmup_films_the_opening_and_retries_a_late_consent_dialog(tmp_path, monkeypatch):
    """The pre-roll starts while the page settles: those frames must be filmed."""
    monkeypatch.setattr(served_ads, "_ffmpeg_available", lambda: False)
    fake = _FakeTime()
    monkeypatch.setattr(served_ads, "time", fake)
    monkeypatch.setattr(served_ads, "WARMUP_SECONDS", 6.0)
    dismissed = []
    monkeypatch.setattr(served_ads, "_dismiss_consent",
                        lambda driver: dismissed.append(1))

    driver = _FakeDriver()
    recorder = served_ads._ClipRecorder(str(tmp_path / "proof"), "v")
    tracker = served_ads._Tracker(recorder=recorder, driver=driver)
    monkeypatch.setattr(
        served_ads, "_poll",
        lambda _d: _snap(False, content_time=0.0, rect=None),
    )

    started = fake.time()
    served_ads._warmup(driver, tracker, started + 100000, started)

    assert dismissed, "a page with no player yet means the dialog may still be up"
    assert [f["showing"] for f in recorder.buffer] == [False, False]
    assert tracker.ads == []           # the warmup only watches, it detects nothing new


def test_crop_filter_rounds_and_guards():
    assert served_ads._crop_filter({"x": 1, "y": 2, "w": 1279.0, "h": 719.0}) == "crop=1278:718:1:2"
    assert served_ads._crop_filter(None) is None
    assert served_ads._crop_filter({"x": 0, "y": 0, "w": 20.0, "h": 10.0}) is None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def test_extract_script_is_a_bare_body_with_return():
    """Regression: an IIFE wrapper makes Selenium discard the value (always None)."""
    script = served_ads._EXTRACT_JS
    assert "(function" not in script
    assert "})()" not in script
    assert script.strip().startswith("const q")
    assert "return JSON.stringify" in script


def test_poll_rejects_non_string_results():
    class _BadDriver:
        def execute_script(self, *_args):
            return None

    assert served_ads._poll(_BadDriver()) is None


def test_poll_parses_json_text():
    class _GoodDriver:
        def execute_script(self, *_args):
            return '{"showing": true}'

    assert served_ads._poll(_GoodDriver()) == {"showing": True}


def test_low_level_helpers(monkeypatch):
    assert served_ads.format_timeline({"content_position": 72.0}) == "1:12"
    assert served_ads.format_timeline({}) == ""


def test_summarize_includes_skip_note():
    text = served_ads._summarize({
        "advertiser": "Acme", "destination": "acme.com", "cta": "Shop",
        "duration": 12.0, "skippable": True, "skip_after_s": 5.0,
    })
    assert "Acme" in text and "acme.com" in text and "skippable after 5.0s" in text


def test_should_split_merges_a_late_rendering_overlay():
    current = {"_trigger": "seek@120", "_signature": (None, None, "Visit site", None)}
    # the very same ad, whose advertiser text has just finished loading
    assert served_ads._should_split(
        current, ("Acme", "acme.com", "Visit site", None), "seek@120"
    ) is False


def test_should_split_on_new_pod_member_or_break():
    one = {"_trigger": "playback", "_signature": ("A", "a.com", "Go", 1)}
    # pod advanced to the second ad
    assert served_ads._should_split(one, ("B", "b.com", "Go", 2), "playback") is True
    # same ad but a different break position
    assert served_ads._should_split(one, ("A", "a.com", "Go", 1), "seek@240") is True
    # no change at all
    assert served_ads._should_split(one, ("A", "a.com", "Go", 1), "playback") is False


def test_late_overlay_does_not_split_one_ad_in_tracking(monkeypatch):
    tracker = served_ads._Tracker()
    tracker.trigger = "seek@120"
    tracker.last_content_time = 118.0
    tracker.update(_snap(True, cta="Visit site", content_time=118.0), 1.0)
    tracker.update(_snap(True, "be10X", "be10x.com", "Visit site", index=2,
                         pod=2, duration=30.0, content_time=118.0), 2.0)
    tracker.update(_snap(False, content_time=119.0), 3.0)

    assert len(tracker.ads) == 1
    ad = tracker.ads[0]
    assert ad["advertiser"] == "be10X"
    assert ad["ad_index"] == 2 and ad["ad_pod_size"] == 2
    # seek-triggered ads report the break they belong to, not the stalled playhead
    assert ad["content_position"] == 120.0
    assert ad["placement"] == "mid-roll"


def test_signature_distinguishes_pod_members():
    a = _snap(True, "A", "a.com", index=1)
    b = _snap(True, "A", "a.com", index=2)
    assert served_ads._signature(a) != served_ads._signature(b)


def test_format_served_ads_includes_timeline():
    ads = [
        {"placement": "pre-roll", "content_position": 0.0, "summary": "Acme 10s"},
        {"placement": "mid-roll", "content_position": 300.0, "summary": "Beta 5s"},
    ]
    assert served_ads.format_served_ads(ads) == (
        "pre-roll @ 0:00: Acme 10s; mid-roll @ 5:00: Beta 5s"
    )
    assert served_ads.format_served_ads([]) == ""


def test_clean_collapses_whitespace():
    assert served_ads._clean("  hello   world \n") == "hello world"
    assert served_ads._clean("   ") is None
    assert served_ads._clean(None) is None
