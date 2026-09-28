# Media Analyzer

Point it at a YouTube/Twitch link (or upload a media file) and it will:

1. download the audio (`yt-dlp` + `ffmpeg`),
2. transcribe it with OpenAI Whisper,
3. score the sentiment and pull out keywords,
4. append one row to an Excel report (`media_report.xlsx`),
5. report the ads it can honestly prove are there — see below, because "ads" here
   means three different things.

```
python main.py            # -> http://127.0.0.1:5000
```

The web UI takes the link (or an upload), the transcription accuracy, and an
opt-in "Capture the served ads" checkbox.

## The three kinds of ad

| | What it is | Where it comes from | Portable? |
|---|---|---|---|
| **In-video sponsor read** | The creator reading a sponsor script. Part of the media file. | SponsorBlock first, transcript heuristic as fallback (`ads.py`) | Yes — you can cut it out |
| **Ad-break schedule (YouTube)** | Where YouTube has reserved breaks, i.e. the yellow segments on the progress bar. | `ytInitialPlayerResponse.adPlacements` (`ads.fetch_ad_breaks`) | No — a schedule, not the ads |
| **Served ads** | The spot the platform actually injected while playing. | YouTube: a real browser session. Twitch: the stream's own stitched-ad markers. | YouTube: never in the download. Twitch: baked into the stream itself |

Served ads are the interesting ones, and each platform needs a completely
different reader, because they are delivered differently.

### YouTube: the ad is an overlay, so watch it in a browser

A YouTube served ad is injected by the player on top of your video. It is never in
the downloaded stream (measured: duration deltas between the served and
downloaded media are ~0s), so no amount of audio analysis can recover it. It can
only be *watched*.

`served_ads.py` drives a real Chrome with Selenium and reads the player's ad
overlay — "1 of 2" pod counters, the advertiser card, the Skip Ad button, the
player's own playhead. Headless Chrome is never served ads, so a visible window is
required. Watching 30 minutes in real time is slow, so the default is a **seek
sweep**: watch the opening for the pre-roll, then jump along the timeline, since
seeking past a mid-roll break makes the player serve that break's ad.

Every poll is screenshotted, so each ad is saved as a labelled strip of the
transition — `before`, `start`, `mid`, `end`, `after` — and stitched into a short
mp4. Read with `detection_latency_s` (how far into the ad the first poll that
admitted it already was), that shows whether detection fires the instant the ad
starts or part-way through. Ad pods are split into separate records on three
signals: the "N of M" counter, a changed advertiser card, and the ad's own
playhead restarting (the only one that works when both spots share a card).

### Twitch: the ad *is* the stream (SSAI), so read the stream

Twitch stitches ads into the HLS stream server-side. There is no overlay, no Skip
button, and nothing for a browser to read — verified the hard way: a real Chrome
playing a channel reports **zero** media requests, neither through the page's
`performance` entries nor through the page-level CDP `Network` domain, because the
player fetches from a worker target.

What *is* authoritative is the stream's own ad timeline. The media playlist
carries `#EXT-X-DATERANGE` tags with `CLASS="twitch-stitched-ad"`, one per spot in
a break, naming its length, its position in the pod, the roll type
(`PREROLL`/`MIDROLL`), the creative id and the click-through URL. `twitch_ads.py`
opens the same playback session a viewer would (GraphQL playback token → usher
master playlist), polls the media playlist while the channel is live, and reports
what the markers say.

Because the ad is media, its **frames can be pulled out of the stream**. For each
ad the five moments — a second before it, half a second in, its midpoint, just
before it ends, and a second after — are decoded from whichever segment aired
then, using ffmpeg. That is stronger evidence than a screenshot: it is the ad's
own picture, at a known point in its own playback.

Two things worth knowing about the results:

* A fresh, logged-out session is usually given Twitch's own **"Commercial break in
  progress"** slate rather than a paid spot. Those breaks are flagged
  `house_ad: true` and reported as a filler slate, not dressed up as an advertiser.
* Twitch's markers name **no brand**. The read-through URL's host is reported as a
  stand-in, and every creative is filed in a local registry so the same ad coming
  round again is recognised (next section).

## The Twitch creative registry

Twitch rotates a small pool of creatives and re-serves them, so the same ad keeps
coming back. Every captured ad's own first frame is reduced to a perceptual hash
(dHash via ffmpeg — no extra dependency) and filed in
`proof/twitch_ad_creatives.json` with everything the marker disclosed. A later
sighting is matched by creative id first, then by nearest fingerprint, so the same
ad stays one entry even if Twitch hands out a fresh id for it. Each entry keeps
the hashes of every sighting, because the house slate's background animates
(measured: ~9 bits of drift between two frames of the *same* ad).

`proof/creatives.html` is the resulting gallery: one card per creative, with how
often it has come round, on which channels, in which placements, first/last seen,
and a frame of the ad. Repeat sightings are called out in the report line too
(`repeat sighting (creative-0001, 2 times)`), which flows into the Excel *Served
Ad Details* column.

Each entry has a `label` field **meant to be edited by hand**. Name a creative
once — `"label": "Kotak Neo"` — and every later report, gallery entry and summary
says *identified as Kotak Neo* instead of re-describing an anonymous ad.

## Setup

```
python -m venv --system-site-packages mediaenv311
mediaenv311/Scripts/python.exe -m pip install -r requirements.txt   # Windows
```

System requirements that pip cannot provide:

* **ffmpeg** on `PATH` — audio extraction, yt-dlp post-processing, and Twitch's
  proof frames.
* A **JavaScript runtime** (Node, Deno or Bun) on `PATH` — recent yt-dlp versions
  need one for YouTube format extraction.
* **Google Chrome or Edge** — only for YouTube served-ad capture (Selenium
  downloads a matching driver on first use).
* `numpy<2.2` is pinned in `requirements.txt` because `openai-whisper` pulls in
  numba, which does not work with newer NumPy.

## Configuration

Everything is environment variables; the defaults are sane.

**Analysis**

| Variable | Default | Meaning |
|---|---|---|
| `WHISPER_MODEL` | `small` | `tiny`/`base`/`small`/`medium`/`large` |
| `MEDIA_REPORT` | `./media_report.xlsx` | Where the Excel report lives |
| `YTDLP_COOKIES` | – | A `cookies.txt`, for when YouTube blocks the bot |
| `YTDLP_COOKIES_FROM_BROWSER` | – | `chrome`/`firefox`/`edge` instead |
| `CAPTURE_SERVED_ADS` | off | Enable served-ad capture without the UI checkbox |
| `SERVED_ADS_SECONDS` | – | Fixed capture window; on YouTube it replaces the sweep with a plain watch |

**YouTube served ads**

| Variable | Default | Meaning |
|---|---|---|
| `SERVED_ADS_HEADLESS` | off | Headless never gets ads; leave off |
| `SERVED_ADS_FULL` | off | Play the whole video in real time instead of sweeping |
| `SERVED_ADS_MAX_SECONDS` | `900` | Hard ceiling for one capture |
| `SERVED_ADS_SEEK_STEP` | `120` | Seconds between sweep stops (mid-roll accuracy) |
| `SERVED_ADS_SEEK_DWELL` | `14` | Seconds parked at each stop |
| `SERVED_ADS_AD_WAIT` | `120` | Cap on how long one ad may hold the window open |
| `SERVED_ADS_WARMUP` | `8` | Seconds filmed before the watch window starts |
| `SERVED_ADS_PRE_ROLL_SECONDS` | `35` | Opening window where pre-rolls land |
| `SERVED_ADS_BUMPER_SECONDS` | `7` | At or below this, an ad counts as a bumper |
| `SERVED_ADS_POD_RESTART`, `_TOLERANCE` | `5`, `3` | Playhead-restart pod split |
| `SERVED_ADS_BUFFER_FRAMES` | `3` | Rolling lead-in frames kept |
| `SERVED_ADS_POST_ROLL_SECONDS` | `2` | How long filming continues after an ad |
| `SERVED_ADS_BOUNDARY_SECONDS` | `1` | Offset of the `before`/`after` frames |
| `SERVED_ADS_RECORD` | on | `0` to skip proof clips/manifests |
| `SERVED_ADS_PROOF_DIR` | `./proof` | Where proof artifacts are written |
| `SERVED_ADS_PROFILE` | temp dir | Chrome profile (a returning visitor gets ads) |
| `CHROME_BINARY` | auto | Explicit Chrome/Edge path |

**Twitch served ads**

| Variable | Default | Meaning |
|---|---|---|
| `SERVED_ADS_TWITCH_WATCH` | `300` | Seconds to watch a live channel for a break |
| `SERVED_ADS_TWITCH_POLL` | `2` | Seconds between playlist reads |
| `SERVED_ADS_TWITCH_FRAMES` | `5` | Proof frames per ad (`0` disables them) |
| `SERVED_ADS_TWITCH_HEIGHT` | `360` | Rendition height those frames come from |
| `SERVED_ADS_TWITCH_AFTER_POLLS` | `3` | Polls spent chasing the "content after" frame |
| `SERVED_ADS_TWITCH_TIMEOUT` | `20` | Per-request HTTP timeout |
| `SERVED_ADS_TWITCH_FINGERPRINT_TOLERANCE` | `10` | Bits of dHash drift still counted as the same creative |
| `SERVED_ADS_TWITCH_FINGERPRINTS_KEPT` | `8` | Hashes remembered per creative |
| `TWITCH_CLIENT_ID` | public web id | Override the client id used to open the session |

## The Excel report

One row per run, columns in order: `Platform`, `Link/File`, `Transcript`,
`Sentiment`, `Keywords`, `Ads Detected`, `Ad Time (s)`, `Ad Details`
(in-video sponsors), `Served Ads` (count), `Served Ad Details` (one line per
served ad, e.g. `pre-roll @ 1:00:06: house/filler slate (Twitch's own break
card, no advertiser) 30s preroll creative 2474283100494 ad 1 of 1 repeat
sighting (creative-0001, 2 times)`). Existing files are appended to, and columns
added by later versions are back-filled by header name.

## Proof artifacts

Everything lands in `proof/` (gitignored — it records what the platforms served
to this machine, not source):

```
proof/
  index.html                     browsable page for the last capture
  creatives.html                 gallery of every Twitch creative seen so far
  twitch_ad_creatives.json       the Twitch creative registry (hand-editable)
  <video>_manifest.json          the metadata for one capture
  <video>_ad01_<slug>_1_start.png  the labelled frame strip (before/start/mid/end/after)
  <video>_ad01_<slug>.mp4          the stitched proof clip (YouTube path)
  <channel>_ad01_<slug>_*.png      Twitch frames, decoded from the ad's own segments
```

## Tests

```
mediaenv311/Scripts/python.exe -m pytest -q      # 171 passed
```

Whisper, yt-dlp, Chrome, ffmpeg and the network are all faked, so the suite runs
offline in seconds. The browser-driven capture is tested through a fake driver and
a deterministic clock; the Twitch reader through scripted playlists.

## Known limits, stated plainly

* **YouTube**: served ads are personalised and frequency-capped, so a repeat run
  can legitimately see none; headless never gets them; mid-roll positions are
  accurate only to the seek step; some pod spots never render an advertiser card.
* **Twitch**: markers exist only while a break is in the playlist window, so this
  is a live watch (mid-rolls appear when the streamer runs a break); a fresh
  session is usually given Twitch's own slate instead of a paid spot; VOD playlists
  carry no ad markers (checked against a real archive), so `/videos/<id>` links are
  declined with that reason; positions come from the playlist's own clock and
  ~2s segment boundaries.
* Neither platform's ads can be *skipped* from here — the capture observes and
  proves, it does not modify playback.
* The web UI (the Broadcast Log report sheet) shows everything a capture
  returns: the creative registry/gallery link, the ad's summary line (labels,
  repeat sightings, evidence, decoded start/mid/end frames) and hour-aware
  positions. Former UI gaps are now pinned by tests.

## Layout

```
main.py         Flask front-end (/, /analyze + /status/<id> job API, /health,
                /proof/<file>, /downloads/<file>)
process.py      the pipeline: download -> transcribe -> analyse -> Excel
ads.py          in-video sponsor detection (SponsorBlock + transcript) + YouTube break schedule
served_ads.py   YouTube served ads, read from a driven Chrome
twitch_ads.py   Twitch served ads, read from the stream's own stitched-ad markers
templates/      the single page UI
static/         its stylesheet
tests/          offline suite (pipeline, ads, YouTube capture, Twitch capture)
proof/          generated proof artifacts and the Twitch creative registry
```
