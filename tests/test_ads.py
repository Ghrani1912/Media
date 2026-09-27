"""Tests for ad / sponsor detection (SponsorBlock lookup is mocked)."""

import io
import json
import urllib.error

import pytest

import ads


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://youtu.be/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://www.youtube.com/shorts/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://www.youtube.com/embed/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://www.youtube.com/watch?list=PL123&v=jNQXAC9IVRw&t=5", "jNQXAC9IVRw"),
        ("https://www.twitch.tv/someone/clip/abc", None),
        ("not a url", None),
        (None, None),
    ],
)
def test_extract_video_id(url, expected):
    assert ads.extract_video_id(url) == expected


# --------------------------------------------------------------------------- #
# SponsorBlock fetch
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_parses_segments(monkeypatch):
    payload = [
        {"segment": [10.0, 25.5], "category": "sponsor", "votes": 4},
        {"segment": [60.0, 62.0], "category": "intro", "votes": 1},
        {"segment": [30.0, 30.0], "category": "sponsor"},  # dropped: zero length
    ]
    monkeypatch.setattr(ads.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))

    segments = ads.fetch_sponsor_segments("vid12345678")
    assert len(segments) == 2
    assert segments[0]["category"] == "sponsor"
    assert segments[0]["duration"] == pytest.approx(15.5)
    assert segments[0]["source"] == "sponsorblock"


def test_fetch_404_means_no_segments(monkeypatch):
    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("u", 404, "Not Found", None, io.BytesIO())

    monkeypatch.setattr(ads.urllib.request, "urlopen", _raise)
    assert ads.fetch_sponsor_segments("vid12345678") == []


def test_fetch_network_error_returns_none(monkeypatch):
    def _raise(*_a, **_k):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(ads.urllib.request, "urlopen", _raise)
    assert ads.fetch_sponsor_segments("vid12345678") is None


def test_fetch_without_video_id_returns_none():
    assert ads.fetch_sponsor_segments("") is None


# --------------------------------------------------------------------------- #
# Transcript heuristic
# --------------------------------------------------------------------------- #
def test_transcript_detects_sponsor_read():
    segments = [
        {"start": 0, "end": 5, "text": "Welcome back everyone to the stream."},
        {"start": 5, "end": 12, "text": "This video is sponsored by Acme VPN."},
        {"start": 12, "end": 20, "text": "Use my code SAVE10 for a discount."},
        {"start": 20, "end": 30, "text": "Alright, back to the gameplay."},
    ]
    found = ads.detect_ad_segments_from_transcript(segments)
    assert len(found) == 1
    assert found[0]["category"] == "sponsor-read"
    assert found[0]["start"] == 5
    assert found[0]["end"] == 20
    assert found[0]["source"] == "heuristic"


def test_transcript_merges_nearby_segments():
    segments = [
        {"start": 0, "end": 10, "text": "brought to you by Acme."},
        {"start": 30, "end": 40, "text": "use code SAVE10 at checkout."},
    ]
    found = ads.detect_ad_segments_from_transcript(segments)
    assert len(found) == 1  # 20s gap <= merge threshold
    assert found[0]["start"] == 0 and found[0]["end"] == 40


def test_transcript_ignores_normal_speech():
    segments = [
        {"start": 0, "end": 5, "text": "Let's talk about the new update today."},
        {"start": 5, "end": 10, "text": "The map layout changed completely."},
    ]
    assert ads.detect_ad_segments_from_transcript(segments) == []


def test_transcript_handles_empty():
    assert ads.detect_ad_segments_from_transcript([]) == []
    assert ads.detect_ad_segments_from_transcript(None) == []


# --------------------------------------------------------------------------- #
# detect_ads orchestration
# --------------------------------------------------------------------------- #
def test_detect_ads_uses_sponsorblock(monkeypatch):
    monkeypatch.setattr(
        ads, "fetch_sponsor_segments",
        lambda vid, timeout=15.0: [
            {"category": "sponsor", "start": 100.0, "end": 140.0,
             "duration": 40.0, "source": "sponsorblock", "votes": 5},
            {"category": "intro", "start": 0.0, "end": 8.0,
             "duration": 8.0, "source": "sponsorblock", "votes": 3},
        ],
    )
    report = ads.detect_ads(link="https://youtu.be/jNQXAC9IVRw")
    assert report["verified"] is True
    assert report["ad_count"] == 1          # only "sponsor" counts as an ad
    assert report["ad_seconds"] == 40.0
    assert report["segment_count"] == 2
    assert report["by_category"] == {"sponsor": 1, "intro": 1}
    assert "SponsorBlock" in report["note"]


def test_detect_ads_sponsorblock_empty_note(monkeypatch):
    monkeypatch.setattr(ads, "fetch_sponsor_segments", lambda vid, timeout=15.0: [])
    report = ads.detect_ads(link="https://youtu.be/jNQXAC9IVRw")
    assert report["verified"] is True
    assert report["ad_count"] == 0
    assert "no sponsor" in report["note"].lower()


def test_detect_ads_falls_back_to_transcript(monkeypatch):
    monkeypatch.setattr(ads, "fetch_sponsor_segments", lambda vid, timeout=15.0: [])
    segments = [{"start": 5, "end": 30, "text": "This episode is sponsored by Acme."}]
    report = ads.detect_ads(link="https://youtu.be/jNQXAC9IVRw",
                            transcript_segments=segments)
    assert report["verified"] is True
    assert report["ad_count"] == 1
    assert report["source"] == "heuristic"
    # must not claim SponsorBlock verified segments it did not supply
    assert "Verified via SponsorBlock" not in report["note"]
    assert "unverified" in report["note"]


def test_detect_ads_upload_uses_heuristic():
    segments = [{"start": 0, "end": 15, "text": "Thanks to our sponsor, Acme."}]
    report = ads.detect_ads(link=None, transcript_segments=segments)
    assert report["ad_count"] == 1
    assert report["verified"] is False
    assert report["video_id"] is None


def test_detect_ads_lookup_failure_is_unknown(monkeypatch):
    monkeypatch.setattr(ads, "fetch_sponsor_segments", lambda vid, timeout=15.0: None)
    report = ads.detect_ads(link="https://youtu.be/jNQXAC9IVRw")
    assert report["verified"] is None
    assert report["ad_count"] == 0


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def test_format_segments():
    segments = [
        {"category": "sponsor", "start": 10.0, "end": 30.0, "duration": 20.0, "source": "sponsorblock"},
        {"category": "intro", "start": 0.0, "end": 5.0, "duration": 5.0, "source": "sponsorblock"},
    ]
    assert ads.format_segments(segments) == "sponsor 10-30s; intro 0-5s"
    assert ads.format_segments([]) == ""
