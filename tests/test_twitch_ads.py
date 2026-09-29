"""Tests for the Twitch served-ad reader.

Everything offline: the playback session, the playlist polls, the clock and the
frame decoder are all faked, so these tests exercise the marker parsing, the
pod/placement logic and the proof-frame staging without touching Twitch.

Why this path is a playlist reader and not a browser (verified by hand against a
live channel, and asserted here only as documentation of the intent):

* Twitch stitches ads into the HLS stream (SSAI), so the player shows no ad
  overlay and no "Skip Ad" button to read.
* The Twitch player fetches its media from a worker, so neither the page's
  ``performance.getEntriesByType('resource')`` list nor the page's CDP
  ``Network`` domain ever reports a playlist request -- driving Chrome cannot see
  the ad timeline at all.
* The media playlist does report it: ``#EXT-X-DATERANGE`` with
  ``CLASS="twitch-stitched-ad"``.
"""

import io
import json
import os
import subprocess
from datetime import datetime, timezone

import pytest

import served_ads
import twitch_ads

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
# The stream's own clock: ad markers and segment dates are in epoch seconds, and
# the capture compares them with each other, never with the local clock.
T0 = 1_700_000_000.0


def _iso(when: float) -> str:
    return datetime.fromtimestamp(when, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def _ad_line(window_id, start, duration, roll="MIDROLL", position=0, pod=1,
             creative="111", url="https://www.amazon.in/deals", commercial="c1"):
    return (
        f'#EXT-X-DATERANGE:ID="{window_id}",CLASS="twitch-stitched-ad",'
        f'START-DATE="{_iso(start)}",DURATION={duration},'
        f'X-TV-TWITCH-AD-POD-LENGTH="{pod}",X-TV-TWITCH-AD-POD-POSITION="{position}",'
        f'X-TV-TWITCH-AD-ROLL-TYPE="{roll}",X-TV-TWITCH-AD-AD-FORMAT="standard_video_ad",'
        f'X-TV-TWITCH-AD-COMMERCIAL-ID="{commercial}",X-TV-TWITCH-AD-CREATIVE-ID="{creative}",'
        f'X-TV-TWITCH-AD-LINE-ITEM-ID="li-{creative}",'
        f'X-TV-TWITCH-AD-AD-SESSION-ID="session-1",X-TV-TWITCH-AD-URL="{url}"'
    )


def _playlist(segments, ads, elapsed=2000.0, server=None, endlist=False):
    """One media playlist: `segments` are ``(start, duration, title)`` tuples."""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:2",
        "#EXT-X-MEDIA-SEQUENCE:0",
        f"#EXT-X-TWITCH-ELAPSED-SECS:{elapsed:.3f}",
        f"#EXT-X-TWITCH-TOTAL-SECS:{elapsed + 6:.3f}",
    ]
    server = server if server is not None else segments[-1][0]
    lines.append(
        f'#EXT-X-DATERANGE:ID="playlist-creation-{int(server)}",CLASS="timestamp",'
        f'START-DATE="{_iso(server)}",END-ON-NEXT=YES,X-SERVER-TIME="{server}"'
    )
    lines.extend(ads)
    for start, duration, title in segments:
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_iso(start)}")
        lines.append(f"#EXTINF:{duration:.3f},{title or ''}")
        lines.append(f"https://edge.example.test/segment/{int(start)}.ts")
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


class _FakeTime:
    """Deterministic clock: every call or sleep advances virtual time."""

    def __init__(self, step=1.0):
        self.now = 0.0
        self.step = step

    def time(self):
        self.now += self.step
        return self.now

    def sleep(self, _seconds):
        self.now += self.step


@pytest.fixture
def twitch(monkeypatch, tmp_path):
    """Patch the clock, playlist source and frame decoder; return the harness."""
    script: list = []
    calls: list = []
    # one virtual second per poll (time() + sleep() each step by half a second)
    fake_time = _FakeTime(step=0.5)

    def _decode(uri, offset, target):
        calls.append((uri, round(float(offset), 2), target))
        with open(target, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n fake")
        return True

    monkeypatch.setattr(twitch_ads, "time", fake_time)
    monkeypatch.setattr(twitch_ads, "decode_frame", _decode)
    monkeypatch.setattr(twitch_ads, "_fetch_text",
                        lambda url: script.pop(0) if script else None)
    monkeypatch.setattr(twitch_ads, "media_playlist_url",
                        lambda kind, ident, quality=None: "https://media.example.test/x.m3u8")
    return {"script": script, "calls": calls, "clock": fake_time,
            "proof": str(tmp_path / "proof")}


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,expected", [
    ("https://www.twitch.tv/gon_vl", ("live", "gon_vl")),
    ("https://twitch.tv/GON_VL", ("live", "gon_vl")),
    ("https://m.twitch.tv/caedrel", ("live", "caedrel")),
    ("https://www.twitch.tv/channel/riotgames", ("live", "riotgames")),
    ("https://player.twitch.tv/?channel=monstercat", ("live", "monstercat")),
    ("https://www.twitch.tv/videos/2886229732", ("vod", "2886229732")),
    ("https://www.twitch.tv/v/1234567", ("vod", "1234567")),
    ("https://www.twitch.tv/directory", None),
    ("https://www.twitch.tv/settings/profile", None),
    ("https://www.youtube.com/watch?v=abc", None),
    ("", None),
])
def test_twitch_target_parsing(url, expected):
    assert twitch_ads.twitch_target(url) == expected


# --------------------------------------------------------------------------- #
# Playlist parsing
# --------------------------------------------------------------------------- #
def test_parse_playlist_reads_ad_markers_and_segments():
    ads = [
        _ad_line("stitched-ad-1", T0, 30.235, roll="PREROLL", position=0, pod=1,
                 creative="111"),
        _ad_line("stitched-ad-2", T0 + 40, 20.0, roll="MIDROLL", position=0, pod=2,
                 creative="222", commercial="c2"),
        _ad_line("stitched-ad-3", T0 + 60, 15.0, roll="MIDROLL", position=1, pod=2,
                 creative="333", commercial="c3"),
    ]
    # a real playlist's segments are contiguous, so the elapsed counter and the
    # running EXTINF total agree
    segments = [(T0, 2.0, "Amazon|111"), (T0 + 2, 2.0, "Amazon|222"),
                (T0 + 4, 2.0, "Amazon|333")]
    parsed = twitch_ads.parse_playlist(_playlist(segments, ads, elapsed=1000.0))

    assert [ad["id"] for ad in parsed["ads"]] == [
        "stitched-ad-1", "stitched-ad-2", "stitched-ad-3"
    ]
    assert parsed["ads"][0]["duration"] == 30.235
    assert parsed["ads"][0]["pod_length"] == 1
    assert parsed["ads"][1]["pod_position"] == 0
    assert parsed["ads"][2]["pod_length"] == 2
    assert parsed["ads"][1]["creative_id"] == "222"
    assert parsed["ads"][1]["click_url"] == "https://www.amazon.in/deals"
    assert parsed["ads"][2]["end"] == pytest.approx(T0 + 75)
    assert parsed["endlist"] is False
    # offsets are stream seconds: the elapsed counter plus each segment's own
    # running EXTINF total, which is what a viewer's timeline shows
    assert parsed["segments"][0]["offset"] == pytest.approx(1000.0)
    assert parsed["segments"][1]["offset"] == pytest.approx(1002.0)
    assert parsed["segments"][2]["offset"] == pytest.approx(1004.0)
    assert parsed["content_duration"] == pytest.approx(1006.0)
    assert parsed["server_time"] == pytest.approx(T0 + 4)


def test_playlist_edge_and_ad_lookup():
    ads = [
        _ad_line("ad-1", T0, 30.0),
        _ad_line("ad-2", T0 + 30, 20.0, position=1, pod=2, creative="222"),
    ]
    parsed = twitch_ads.parse_playlist(
        _playlist([(T0, 2.0, ""), (T0 + 30, 2.0, "Amazon|222")], ads)
    )
    edge = twitch_ads.playlist_edge(parsed)
    assert edge == pytest.approx(T0 + 30)
    # Twitch's ad windows are contiguous, so "which ad is airing" has one answer
    assert twitch_ads.ad_at(parsed["ads"], T0 + 10)["id"] == "ad-1"
    assert twitch_ads.ad_at(parsed["ads"], T0 + 35)["id"] == "ad-2"
    assert twitch_ads.ad_at(parsed["ads"], T0 + 90) is None


def test_segments_for_ad_matches_by_air_time_then_by_creative():
    window = {"id": "ad-1", "start": T0, "duration": 10.0, "creative_id": "777"}
    by_time = [
        {"uri": "a", "start": T0 + 1, "duration": 2.0, "title": None, "offset": 0},
        {"uri": "b", "start": T0 + 40, "duration": 2.0, "title": None, "offset": 0},
    ]
    assert [s["uri"] for s in twitch_ads.segments_for_ad(window, by_time)] == ["a"]

    # no air times (a playlist that dropped PROGRAM-DATE-TIME): fall back to the
    # creative id Twitch stamps on the stitched segments
    by_title = [{"uri": "c", "start": None, "duration": 2.0, "title": "Amazon|777",
                 "offset": 0}]
    assert [s["uri"] for s in twitch_ads.segments_for_ad(window, by_title)] == ["c"]


def test_segment_at_falls_back_to_the_nearest_earlier_segment():
    segments = [
        {"uri": "a", "start": T0, "duration": 2.0},
        {"uri": "b", "start": T0 + 2, "duration": 2.0},
    ]
    assert twitch_ads.segment_at(segments, T0 + 1)["uri"] == "a"
    assert twitch_ads.segment_at(segments, T0 + 2.5)["uri"] == "b"
    assert twitch_ads.segment_at(segments, T0 + 9)["uri"] == "b"


def test_media_playlist_url_uses_the_channel_api_path(monkeypatch):
    """Regression: Twitch's channel master playlist lives under /api/channel/hls.

    Asking for it without the /api prefix 404s, which is exactly what the first
    live run did -- so the URL shape is pinned here.
    """
    seen = {}
    master = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=1280x720,VIDEO="720p60"\n'
        "https://video.example.test/720.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=300,RESOLUTION=640x360,VIDEO="360p"\n'
        "https://video.example.test/360.m3u8\n"
    )

    def _fetch(url):
        seen["url"] = url
        return master

    monkeypatch.setattr(twitch_ads, "playback_access_token", lambda kind, ident: ("tok", "sig"))
    monkeypatch.setattr(twitch_ads, "_fetch_text", _fetch)
    url = twitch_ads.media_playlist_url("live", "gon_vl")
    assert "/api/channel/hls/gon_vl.m3u8?" in seen["url"]
    assert "token=tok" in seen["url"] and "sig=sig" in seen["url"]
    assert url == "https://video.example.test/360.m3u8"


def test_variant_picker_prefers_a_legible_cheap_rendition():
    variants = [
        {"name": "1080p60", "height": 1080, "url": "hi"},
        {"name": "160p", "height": 144, "url": "tiny"},
        {"name": "720p60", "height": 720, "url": "mid"},
        {"name": "360p", "height": 360, "url": "low"},
    ]
    assert twitch_ads._pick_variant(variants) == "low"
    # a channel that only offers tiny renditions still gets a frame
    assert twitch_ads._pick_variant([{"name": "160p", "height": 144, "url": "tiny"}]) == "tiny"
    assert twitch_ads._pick_variant(variants, quality="720p60") == "mid"


def test_house_ad_slate_is_not_attributed_to_an_advertiser():
    window = {
        "id": "stitched-ad-1", "start": T0, "duration": 30.0, "roll_type": "PREROLL",
        "pod_position": 0, "pod_length": 1, "creative_id": "2474283100494",
        "click_url": "https://www.twitch.tv",
    }
    assert twitch_ads.is_house_ad(window) is True
    assert twitch_ads.is_house_ad({"click_url": "https://www.amazon.in/x"}) is False

    collector = twitch_ads.AdCollector(None, "gon_vl", frames=False)
    record = collector.open(window, {"segments": []}, now=1.0)
    collector.finalize_all()
    ad = collector.ads[0]
    assert ad["house_ad"] is True
    assert ad["advertiser"] is None
    assert ad["placement"] == "pre-roll"
    assert "house/filler slate" in ad["summary"]
    assert ad["creative_id"] == "2474283100494"


def test_placement_and_advertiser_hint():
    assert twitch_ads._placement({"roll_type": "PREROLL"}) == "pre-roll"
    assert twitch_ads._placement({"roll_type": "MIDROLL"}) == "mid-roll"
    assert twitch_ads._placement({}) == "unknown"
    assert twitch_ads._host_of("https://www.amazon.in/deals") == "amazon.in"
    assert twitch_ads._host_of("https://ad.doubleclick.net/x") is None
    assert twitch_ads._host_of(None) is None


# --------------------------------------------------------------------------- #
# The collector: frames pinned to the stream's own moments
# --------------------------------------------------------------------------- #
def test_collector_decodes_a_frame_per_moment(monkeypatch, tmp_path):
    recorded = []

    def _decode(uri, offset, target):
        recorded.append((uri, round(float(offset), 2)))
        with open(target, "wb") as handle:
            handle.write(b"png")
        return True

    monkeypatch.setattr(twitch_ads, "decode_frame", _decode)
    collector = twitch_ads.AdCollector(str(tmp_path), "gon_vl", frames=True)
    window = {
        "id": "ad-1", "start": T0 + 10, "duration": 20.0, "roll_type": "MIDROLL",
        "pod_position": 0, "pod_length": 2, "creative_id": "42",
        "click_url": "https://www.kotak.com/x", "commercial_id": "c", "ad_format": "std",
        "line_item_id": "li", "ad_session_id": "s",
    }
    parsed = {"segments": [
        {"uri": "content-before", "start": T0 + 8, "duration": 2.0, "offset": 0},
        {"uri": "ad-first", "start": T0 + 10, "duration": 2.0, "offset": 0},
        {"uri": "ad-middle", "start": T0 + 20, "duration": 2.0, "offset": 0},
        {"uri": "ad-last", "start": T0 + 28, "duration": 2.0, "offset": 0},
        {"uri": "content-after", "start": T0 + 30, "duration": 2.0, "offset": 0},
    ]}
    record = collector.open(window, parsed, now=5.0)
    collector.capture_targets(record, parsed, T0 + 9)   # only "before" is due
    collector.capture_targets(record, parsed, T0 + 31)  # the rest
    collector.close(record)
    collector.finalize_all()

    assert len(collector.ads) == 1
    ad = collector.ads[0]
    assert ad["placement"] == "mid-roll"
    assert ad["advertiser"] == "kotak.com"
    assert ad["ad_index"] == 1 and ad["ad_pod_size"] == 2
    assert ad["duration"] == 20.0
    assert ad["evidence"] == "stream-frames"
    assert ad["ad_frames"] == 5
    assert ad["before_frame_s"] == 1.0
    assert ad["after_frame_s"] == 1.0
    # the content frames bracket the ad; the ad frames come from the stitched
    # segments, which is the whole point of decoding rather than screenshotting
    assert {uri for uri, _ in recorded} == {
        "content-before", "ad-first", "ad-middle", "ad-last", "content-after"
    }
    assert os.path.exists(ad["frame_start"])
    assert ad["thumbnail"] == ad["frame_start"]
    assert "ad 1 of 2" in ad["summary"]
    assert ad["frame_before"].endswith("_0_before.png")


def test_collector_records_a_marker_only_ad_without_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(twitch_ads, "decode_frame", lambda *a, **k: False)
    collector = twitch_ads.AdCollector(str(tmp_path), "gon_vl", frames=True)
    window = {"id": "ad-1", "start": T0, "duration": 30.0, "roll_type": "MIDROLL",
              "pod_position": 0, "pod_length": 1, "creative_id": "42"}
    parsed = {"segments": [{"uri": "x", "start": T0 + 32, "duration": 2.0, "offset": 0}]}
    record = collector.open(window, parsed, now=1.0)
    collector.capture_targets(record, parsed, T0 + 40)
    collector.close(record)
    collector.finalize_all()

    ad = collector.ads[0]
    assert ad["evidence"] == "marker-only"
    assert ad["frames"] == 0
    assert ad["detection_latency_s"] is None
    assert "marker only" in ad["summary"]


# --------------------------------------------------------------------------- #
# Frame decoding
# --------------------------------------------------------------------------- #
class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _prepare_decode(monkeypatch, results):
    """Fake ffmpeg + the segment download; return the commands it was given."""
    commands: list[list[str]] = []

    def _run(command, **_kwargs):
        commands.append(list(command))
        ok = results.pop(0) if results else True
        target = command[-1]
        if ok:
            with open(target, "wb") as handle:
                handle.write(b"png")
        return subprocess.CompletedProcess(command, 0 if ok else 1, "", "boom")

    monkeypatch.setattr(twitch_ads.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(twitch_ads.subprocess, "run", _run)
    monkeypatch.setattr(twitch_ads.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResponse(b"segment"))
    return commands


def test_decode_frame_selects_the_moment_on_the_output_side(monkeypatch, tmp_path):
    commands = _prepare_decode(monkeypatch, [True])
    target = str(tmp_path / "frame.png")
    assert twitch_ads.decode_frame("https://edge/seg.ts", 1.2, target) is True
    assert os.path.exists(target)
    # seeking the *input* into a short stitched segment lands mid-GOP and yields
    # no picture at all, so the offset has to be a filter instead
    assert "select=gte(t\\,1.20)" in commands[0]
    assert "-ss" not in commands[0]


def test_decode_frame_takes_the_first_picture_when_the_offset_is_zero(monkeypatch, tmp_path):
    commands = _prepare_decode(monkeypatch, [True])
    assert twitch_ads.decode_frame("https://edge/seg.ts", 0.0, str(tmp_path / "f.png"))
    assert not any(arg.startswith("select=") for arg in commands[0])


def test_decode_frame_falls_back_to_the_first_picture(monkeypatch, tmp_path):
    commands = _prepare_decode(monkeypatch, [False, True])
    target = str(tmp_path / "frame.png")
    assert twitch_ads.decode_frame("https://edge/seg.ts", 1.5, target) is True
    assert len(commands) == 2
    assert "select=gte(t\\,1.50)" in commands[0]
    assert not any(arg.startswith("select=") for arg in commands[1])
    assert os.path.exists(target)


def test_decode_frame_gives_up_without_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(twitch_ads.shutil, "which", lambda name: None)

    def _no_download(*_args, **_kwargs):
        raise AssertionError("nothing should be downloaded without ffmpeg")

    monkeypatch.setattr(twitch_ads.urllib.request, "urlopen", _no_download)
    assert twitch_ads.decode_frame("https://edge/seg.ts", 1.0, str(tmp_path / "f.png")) is False


# --------------------------------------------------------------------------- #
# Recognising the same creative coming round again
# --------------------------------------------------------------------------- #
class _PixelResult:
    def __init__(self, pixels):
        self.stdout = pixels
        self.returncode = 0
        self.stderr = ""


def _pixels(value_at, rows=8, columns=9):
    """A 9x8 grayscale grid, as ffmpeg would hand it over for a dHash."""
    data = bytearray()
    for row in range(rows):
        for column in range(columns):
            data.append(value_at(row, column) & 0xFF)
    return bytes(data)


def test_frame_fingerprint_is_stable_and_content_sensitive(monkeypatch, tmp_path):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")
    # brighter to the left: every horizontal comparison is "greater", so every
    # dHash bit is set
    brightening = _pixels(lambda row, column: 240 - column * 30)
    monkeypatch.setattr(twitch_ads.shutil, "which", lambda name: f"/fake/{name}")

    monkeypatch.setattr(twitch_ads.subprocess, "run", lambda *a, **k: _PixelResult(brightening))
    first = twitch_ads.frame_fingerprint(str(frame))
    second = twitch_ads.frame_fingerprint(str(frame))
    assert first == second and len(first) == 16
    assert first == "f" * 16

    # a flat slate has no horizontal edge at all, so it hashes differently
    monkeypatch.setattr(twitch_ads.subprocess, "run",
                        lambda *a, **k: _PixelResult(_pixels(lambda row, column: 200)))
    assert twitch_ads.frame_fingerprint(str(frame)) == "0" * 16
    assert twitch_ads.frame_fingerprint(str(frame)) != first


def test_frame_fingerprint_gives_up_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(twitch_ads.shutil, "which", lambda name: None)
    assert twitch_ads.frame_fingerprint(str(tmp_path / "missing.png")) is None

    frame = tmp_path / "frame.png"
    frame.write_bytes(b"png")
    monkeypatch.setattr(twitch_ads.shutil, "which", lambda name: "/fake/ffmpeg")
    # ffmpeg that produced nothing (the input seek trap) must not be a crash
    monkeypatch.setattr(twitch_ads.subprocess, "run",
                        lambda *a, **k: _PixelResult(b""))
    assert twitch_ads.frame_fingerprint(str(frame)) is None


def test_fingerprint_distance_counts_differing_bits():
    assert twitch_ads.fingerprint_distance("ffffffffffffffff", "ffffffffffffffff") == 0
    assert twitch_ads.fingerprint_distance("0000000000000000", "0000000000000001") == 1
    assert twitch_ads.fingerprint_distance(None, "00") == 64
    assert twitch_ads.fingerprint_distance("abc", "abcdef") == 64


def _twitch_ad(**overrides):
    ad = {
        "creative_id": "111", "destination": "https://www.amazon.in/deals",
        "house_ad": False, "placement": "pre-roll", "channel": "gon_vl",
        "duration": 30.0, "roll_type": "PREROLL", "evidence": "stream-frames",
        "ad_index": 1, "ad_pod_size": 1,
    }
    ad.update(overrides)
    return ad


def test_registry_recognises_a_repeat_by_creative_id(monkeypatch, tmp_path):
    monkeypatch.setattr(twitch_ads, "frame_fingerprint", lambda path, size=8: "aaaa0000aaaa0000")
    registry = twitch_ads.CreativeRegistry(str(tmp_path))
    first = _twitch_ad()
    entry = registry.record(first, None)
    assert entry["times_seen"] == 1
    assert first["creative_ref"] == "creative-0001"
    assert first["times_seen"] == 1

    later = _twitch_ad(destination=None)  # same ad, days later, no click URL
    registry.record(later, None)
    assert len(registry.entries) == 1
    assert later["times_seen"] == 2
    assert later["creative_ref"] == first["creative_ref"]
    summary = twitch_ads._summarize(later)
    assert "repeat sighting" in summary and "2 times" in summary


def test_registry_falls_back_to_the_fingerprint(monkeypatch, tmp_path):
    # Twitch sometimes hands out a fresh creative id for the same picture, so the
    # fingerprint is what actually ties the sightings together.
    monkeypatch.setattr(twitch_ads, "frame_fingerprint", lambda path, size=8: "0000ffff0000ffff")
    registry = twitch_ads.CreativeRegistry(str(tmp_path))
    registry.record(_twitch_ad(creative_id="111"), None)
    later = _twitch_ad(creative_id="999", destination=None)
    registry.record(later, None)
    assert len(registry.entries) == 1
    assert later["times_seen"] == 2
    assert registry.entries[0]["creative_ids"] == ["111", "999"]


def test_registry_tracks_a_drifting_fingerprint_of_the_same_creative(monkeypatch, tmp_path):
    # the house slate's animated background moves the hash a few bits between
    # sightings; remembering each of them keeps the creative one entry
    hashes = iter(["0000000000000000", "000000000000000f"])
    monkeypatch.setattr(twitch_ads, "frame_fingerprint", lambda path, size=8: next(hashes))
    registry = twitch_ads.CreativeRegistry(str(tmp_path))
    registry.record(_twitch_ad(creative_id="111"), None)
    later = _twitch_ad(creative_id="999", destination=None)
    registry.record(later, None)
    assert len(registry.entries) == 1
    assert later["times_seen"] == 2
    assert registry.entries[0]["fingerprints"] == ["0000000000000000", "000000000000000f"]
    assert twitch_ads.fingerprint_distance("0000000000000000", "000000000000000f") <= 10


def test_registry_keeps_distinct_creatives_apart(monkeypatch, tmp_path):
    hashes = iter(["0000000000000000", "ffffffffffffffff"])
    monkeypatch.setattr(twitch_ads, "frame_fingerprint", lambda path, size=8: next(hashes))
    registry = twitch_ads.CreativeRegistry(str(tmp_path))
    registry.record(_twitch_ad(creative_id=None, destination=None), None)
    other = _twitch_ad(creative_id=None, destination=None)
    registry.record(other, None)
    assert len(registry.entries) == 2
    assert other["times_seen"] == 1
    assert other["creative_ref"] == "creative-0002"
    # far-apart frames must not be merged just because both are plain ads
    assert twitch_ads.fingerprint_distance("0000000000000000", "ffffffffffffffff") == 64


def test_registry_persists_and_honours_a_hand_written_label(monkeypatch, tmp_path):
    monkeypatch.setattr(twitch_ads, "frame_fingerprint", lambda path, size=8: "1234567890abcdef")
    registry = twitch_ads.CreativeRegistry(str(tmp_path))
    registry.record(_twitch_ad(), None)

    with open(registry.path, encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["creatives"][0]["click_hosts"] == ["amazon.in"]
    assert saved["creatives"][0]["channels"] == ["gon_vl"]
    assert saved["creatives"][0]["placements"] == ["pre-roll"]
    assert "label" in saved["creatives"][0]

    # naming a creative by hand is the whole point: later runs use the name
    saved["creatives"][0]["label"] = "Amazon deals"
    with open(registry.path, "w", encoding="utf-8") as handle:
        json.dump(saved, handle)

    reopened = twitch_ads.CreativeRegistry(str(tmp_path))
    ad = _twitch_ad()
    reopened.record(ad, None)
    assert ad["known_label"] == "Amazon deals"
    assert ad["times_seen"] == 2
    assert reopened.seen_label(reopened.entries[0]) == "Amazon deals"
    assert twitch_ads._summarize(ad).startswith("identified as Amazon deals")


def test_creatives_gallery_lists_each_creative_once(monkeypatch, tmp_path):
    monkeypatch.setattr(twitch_ads, "frame_fingerprint", lambda path, size=8: "abcdef0123456789")
    registry = twitch_ads.CreativeRegistry(str(tmp_path))
    registry.record(_twitch_ad(house_ad=True, destination="https://www.twitch.tv"),
                    str(tmp_path / "gon_vl_ad01_twitch-tv_1_start.png"))
    registry.record(_twitch_ad(house_ad=True, destination="https://www.twitch.tv"), None)
    path = twitch_ads.write_creatives_index(registry, str(tmp_path))

    assert path and os.path.exists(path)
    html = open(path, encoding="utf-8").read()
    assert "Twitch house slate" in html
    assert "seen 2x" in html
    assert "abcdef0123456789" in html
    assert "gon_vl_ad01_twitch-tv_1_start.png" in html
    assert html.count("<li>") == 1


# --------------------------------------------------------------------------- #
# End-to-end capture (faked network)
# --------------------------------------------------------------------------- #
# One session's worth of playlists: a pre-roll at the very start of the session,
# then a two-ad mid-roll pod, then content. Twitch's playlist is a session
# playlist -- it grows in place rather than sliding -- so each poll lists every
# segment since the session opened, which is also what lets an ad's frames still
# be pulled out of the stream once the break is over.
_STREAM = [
    {"id": "stitched-ad-pre", "start": T0, "duration": 30.235, "roll": "PREROLL",
     "position": 0, "pod": 1, "creative": "111", "commercial": "c1"},
    {"id": "stitched-ad-pod-a", "start": T0 + 30, "duration": 20.0, "roll": "MIDROLL",
     "position": 0, "pod": 2, "creative": "222", "commercial": "c2"},
    {"id": "stitched-ad-pod-b", "start": T0 + 50, "duration": 15.0, "roll": "MIDROLL",
     "position": 1, "pod": 2, "creative": "333", "commercial": "c3"},
]
_EDGES = (0, 4, 10, 28, 32, 40, 52, 62, 66, 68)


def _creative_at(when: float):
    for window in _STREAM:
        if window["start"] <= when < window["start"] + window["duration"]:
            return window["creative"]
    return None


def _single_ad_stream():
    """Build the successive playlists one capture session would read."""
    polls = []
    for edge in _EDGES:
        at = T0 + edge
        # a marker appears once its break has started on the stream
        ads = [
            _ad_line(w["id"], w["start"], w["duration"], roll=w["roll"],
                     position=w["position"], pod=w["pod"], creative=w["creative"],
                     commercial=w["commercial"])
            for w in _STREAM if w["start"] <= at
        ]
        segments = []
        moment = T0
        while moment <= at:
            creative = _creative_at(moment)
            segments.append((moment, 2.0, f"Amazon|{creative}" if creative else None))
            moment += 2.0
        polls.append(_playlist(segments, ads))
    return polls


def test_capture_reads_a_pre_roll_and_splits_a_pod(twitch):
    twitch["script"].extend(_single_ad_stream())
    report = twitch_ads.capture_twitch_ads(
        "https://www.twitch.tv/gon_vl", watch_seconds=12, proof_dir=twitch["proof"],
        video_id="gon_vl",
    )

    assert report["available"] is True
    assert report["source"] == "stream-markers"
    assert report["strategy"] == "live"
    assert report["channel"] == "gon_vl"
    assert report["captured"] is True
    assert report["ad_count"] == 3
    assert [ad["placement"] for ad in report["ads"]] == ["pre-roll", "mid-roll", "mid-roll"]
    assert [ad["format"] for ad in report["ads"]] == [twitch_ads.TWITCH_FORMAT] * 3
    assert [ad["ad_pod_size"] for ad in report["ads"]] == [1, 2, 2]
    assert [ad["ad_index"] for ad in report["ads"]] == [1, 1, 2]
    assert [ad["duration"] for ad in report["ads"]] == [30.2, 20.0, 15.0]
    # the pod's two spots are separate records even though one pod marker covers both
    assert report["ads"][1]["creative_id"] == "222"
    assert report["ads"][2]["creative_id"] == "333"
    assert report["ad_seconds"] == pytest.approx(65.2)
    # an ad's position on the broadcast comes from the playlist's own counters
    assert report["ads"][0]["content_position"] == pytest.approx(2000.0)
    assert all(ad["trigger"] == "playlist-marker" for ad in report["ads"])
    assert all(ad["wall_started_at"] for ad in report["ads"])
    assert report["polls"] >= 8
    assert "stitched" in report["note"] and "markers" in report["note"]


def test_capture_writes_proof_frames_manifest_and_index(twitch, monkeypatch):
    monkeypatch.setattr(twitch_ads, "frame_fingerprint", lambda path, size=8: None)
    twitch["script"].extend(_single_ad_stream())
    report = twitch_ads.capture_twitch_ads(
        "https://www.twitch.tv/gon_vl", watch_seconds=12, proof_dir=twitch["proof"],
        video_id="gon_vl",
    )

    proof = twitch["proof"]
    for ad in report["ads"]:
        assert ad["frames_source"] == "stream-segments"
        assert ad["frame_start"] and os.path.exists(ad["frame_start"])
        assert ad["thumbnail"] == ad["frame_start"]
        assert ad["frame_start"].endswith("_1_start.png")
    # the pre-roll is the session's first segment, so nothing precedes it
    assert report["ads"][0].get("frame_before") is None
    # the mid-roll pod's ads are bracketed by content on both sides
    assert report["ads"][1]["frame_before"].endswith("_0_before.png")
    assert report["ads"][2]["frame_after"].endswith("_4_after.png")
    assert os.path.exists(report["manifest"])
    assert os.path.exists(report["index"])
    with open(report["manifest"], encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["ad_count"] == 3
    with open(report["index"], encoding="utf-8") as handle:
        html = handle.read()
    assert "the ad's own first frame" in html
    assert "content segment just before the ad" in html
    # proof frames are named and placed like the YouTube path's, so one folder
    # can hold both kinds of capture
    names = os.listdir(proof)
    assert any(name.startswith("gon_vl_ad01_") for name in names)
    assert all(not name.endswith(".tmp") for name in names)


def test_capture_files_creatives_and_spots_repeats_across_runs(twitch, monkeypatch):
    """Twitch re-serves the same few creatives, so a repeat run must say so."""
    # no frames to hash in this test, so the pool is tied together by the creative
    # ids the markers did give (the fingerprint path is covered separately)
    monkeypatch.setattr(twitch_ads, "frame_fingerprint", lambda path, size=8: None)
    twitch["script"].extend(_single_ad_stream())
    first = twitch_ads.capture_twitch_ads(
        "https://www.twitch.tv/gon_vl", watch_seconds=12, proof_dir=twitch["proof"],
        video_id="gon_vl",
    )

    assert first["creative_count"] == 3
    assert first["creatives_index"] and os.path.exists(first["creatives_index"])
    assert [ad["creative_ref"] for ad in first["ads"]] == [
        "creative-0001", "creative-0002", "creative-0003"
    ]
    assert all(ad["times_seen"] == 1 for ad in first["ads"])
    assert all("new creative" in ad["summary"] for ad in first["ads"])
    assert os.path.exists(os.path.join(twitch["proof"], twitch_ads.REGISTRY_FILE))

    # a later session sees the same three ads again
    twitch["script"].extend(_single_ad_stream())
    second = twitch_ads.capture_twitch_ads(
        "https://www.twitch.tv/gon_vl", watch_seconds=12, proof_dir=twitch["proof"],
        video_id="gon_vl",
    )
    assert second["creative_count"] == 3
    assert [ad["times_seen"] for ad in second["ads"]] == [2, 2, 2]
    assert [ad["creative_ref"] for ad in second["ads"]] == [
        ad["creative_ref"] for ad in first["ads"]
    ]
    assert "repeat sighting" in second["ads"][0]["summary"]
    assert "the local registry has seen before" in second["note"]
    assert "3 of them" in second["note"]

    html = open(second["creatives_index"], encoding="utf-8").read()
    assert html.count("<li>") == 3
    assert "seen 2x" in html
    # the per-session proof page points at the gallery once it exists
    index = open(second["index"], encoding="utf-8").read()
    assert served_ads.CREATIVES_PAGE in index


def test_capture_reports_no_ads_without_raising(twitch):
    twitch["script"].extend([
        _playlist([(T0, 2.0, None)], []),
        _playlist([(T0 + 2, 2.0, None)], []),
    ])
    report = twitch_ads.capture_twitch_ads(
        "https://www.twitch.tv/quiet_channel", watch_seconds=5,
        proof_dir=twitch["proof"],
    )
    assert report["available"] is True
    assert report["captured"] is False
    assert report["ad_count"] == 0
    assert "no stitched ad was served" in report["note"]


def test_capture_handles_a_channel_that_is_offline(monkeypatch, twitch):
    def _boom(kind, ident, quality=None):
        raise twitch_ads.TwitchError("This channel is offline")

    monkeypatch.setattr(twitch_ads, "media_playlist_url", _boom)
    report = twitch_ads.capture_twitch_ads(
        "https://www.twitch.tv/quiet_channel", watch_seconds=5,
    )
    assert report["available"] is False
    assert report["captured"] is False
    assert "offline" in report["note"]


def test_vod_links_resolve_to_the_live_channel_and_watch(monkeypatch, twitch):
    """A /videos link now resolves to its channel and runs the live watcher."""
    monkeypatch.setattr(twitch_ads, "_channel_for_vod", lambda vid: "gon_vl")
    twitch["script"].extend(_single_ad_stream())
    report = twitch_ads.capture_twitch_ads(
        "https://www.twitch.tv/videos/2886229732", watch_seconds=12,
        proof_dir=twitch["proof"], video_id="2886229732",
    )
    assert report["strategy"] == "live"
    assert report["channel"] == "gon_vl"
    assert report["vod_resolved_to"] == "gon_vl"
    assert report["captured"] is True
    assert report["ad_count"] == 3


def test_vod_resolution_failure_still_declines_with_a_reason(monkeypatch):
    def _never(*args, **kwargs):  # nothing to watch if the channel can't be told
        raise AssertionError("no session should open without a channel")

    monkeypatch.setattr(twitch_ads, "_channel_for_vod", lambda vid: None)
    monkeypatch.setattr(twitch_ads, "media_playlist_url", _never)
    report = twitch_ads.capture_twitch_ads("https://www.twitch.tv/videos/2886229732")
    assert report["available"] is True
    assert report["captured"] is False
    assert "no ad markers" in report["note"]
    assert "could not be" in report["note"]


def test_channel_for_vod_parses_gql_owner_login(monkeypatch):
    def fake_gql(query):
        assert 'video(id:"2886229732")' in query
        return {"data": {"video": {"owner": {"login": "Caedrel"}}}}

    monkeypatch.setattr(twitch_ads, "_gql", fake_gql)
    assert twitch_ads._channel_for_vod("2886229732") == "caedrel"


def test_channel_for_vod_survives_gql_errors(monkeypatch):
    def boom(query):
        raise twitch_ads.TwitchError("gql down")

    monkeypatch.setattr(twitch_ads, "_gql", boom)
    assert twitch_ads._channel_for_vod("2886229732") is None


def test_playback_token_errors_surface_as_a_note(monkeypatch, twitch):
    def _offline(kind, ident, quality=None):
        raise twitch_ads.TwitchError("streamPlaybackAccessToken is null")

    monkeypatch.setattr(twitch_ads, "media_playlist_url", _offline)
    report = twitch_ads.capture_twitch_ads("https://www.twitch.tv/gon_vl")
    assert report["available"] is False
    assert "Could not open a Twitch playback session" in report["note"]


# --------------------------------------------------------------------------- #
# Wiring into the shared entry point
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=abc", "youtube"),
    ("https://youtu.be/abc", "youtube"),
    ("https://m.youtube.com/watch?v=abc", "youtube"),
    ("https://www.twitch.tv/gon_vl", "twitch"),
    ("https://player.twitch.tv/?channel=x", "twitch"),
    ("https://example.com/video", "unknown"),
    ("", "unknown"),
])
def test_detect_platform(url, expected):
    assert served_ads.detect_platform(url) == expected


def test_capture_served_ads_dispatches_twitch_without_a_browser(monkeypatch):
    seen = {}

    def _fake(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return {"available": True, "captured": False, "ads": [], "ad_count": 0,
                "note": "twitch path", "source": "stream-markers"}

    # the YouTube path must not be consulted at all: no Chrome, no selenium
    def _no_browser():
        raise AssertionError("the Twitch path must not require a browser")

    monkeypatch.setattr(twitch_ads, "capture_twitch_ads", _fake)
    monkeypatch.setattr(served_ads, "availability", _no_browser)
    report = served_ads.capture_served_ads(
        "https://www.twitch.tv/gon_vl", watch_seconds=90, proof_dir="proof",
        video_id="gon_vl",
    )
    assert report["note"] == "twitch path"
    assert seen["url"] == "https://www.twitch.tv/gon_vl"
    assert seen["watch_seconds"] == 90
    assert seen["proof_dir"] == "proof"
    assert seen["video_id"] == "gon_vl"


def test_format_timeline_adds_hours_only_for_long_streams():
    assert served_ads.format_timeline({"content_position": 312}) == "5:12"
    assert served_ads.format_timeline({"content_position": 3725}) == "1:02:05"
    assert served_ads.format_timeline({"content_position": None}) == ""


def test_format_served_ads_describes_each_twitch_ad():
    ads = [{
        "placement": "mid-roll", "content_position": 3725.0,
        "summary": "stitched (ssai) ad via amazon.in 20s midroll ad 2 of 2",
    }]
    text = served_ads.format_served_ads(ads)
    assert text == ("mid-roll @ 1:02:05: stitched (ssai) ad via amazon.in 20s "
                    "midroll ad 2 of 2")
