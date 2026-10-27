# Poetry Shorts Maker

Turn buffered voice recordings and local images into vertical MP4s with timed captions.
The video worker uses the filesystem queue. Baserow ingestion and Cloudinary
publication are optional automations running separately.

```mermaid
flowchart LR
    Files[Local audio files] --> Queue[Numbered voice buffer]
    Queue --> Worker[Transcribe, plan, render]
    Images[Shared image assets] --> Worker
    Worker --> Video[Validated MP4]
    Video --> Consume[Save completion event, remove voice]
    Baserow[Baserow refill automation] -. only when queue is empty .-> Queue
    Consume -. optional listener .-> Upload[Cloudinary upload]
    Upload --> Sheet[Original Baserow row: URL and DONE]
```

## Local quick start

Use Python 3.13 or 3.14 and FFmpeg/FFprobe with H.264 and libass support.
The font, background music and film overlay are included under `assets/`.

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
mkdir -p assets/images
```

Copy still JPG, PNG, WebP or BMP images directly into `assets/images/`.
This shared pool is configured by `composition.images_directory` in `config.yaml`.
The planner randomly selects from it for each reel; images are never consumed.
Only audio is queued in the buffer.

For video footage, put MP4, MOV, MKV, WebM, M4V or AVI clips directly into
`assets/videos/` (`composition.videos_directory`). Choose a mode in the console,
or set `footage.mode` in `config.yaml`: `image` (default), `video`, or `hybrid`.
Video mode randomly selects clips; hybrid randomly selects from both pools and
always opens with a video. Hybrid requires at least one image and one video.
The existing sentence rule applies in every mode: a sentence of at least 3 words
starts a new selection by default. Shorter sentences retain the current footage.
Immediate repeats are avoided when another candidate exists. Clips start at a
random usable offset and loop when shorter than their section; their audio is muted.

Use `GET /run?mode=video` or `GET /run?mode=hybrid` for an API run, or
`python -m app.buffer run --mode hybrid` from the CLI. These overrides apply only
to that run; plain `/run` uses the configured default. The dev tool has the same
three modes with separate test uploads stored under `temp/ui-test/`.

An optional channel name appears in small 8 pt text below the captions. Enter it
in the console or dev tool, pass `channel_name` to `/run`, or use
`python -m app.buffer run --channel-name "My Channel"`. Names are limited to 80
characters; an omitted/blank name produces no label. Set `captions.channel_name`
for a project default. The label follows each full caption block and stays still
during word reveals. Its size scales with the short edge (8 pt at 1080 pixels).

Choose the output aspect ratio in the console or dev tool: **9:16** (1080×1920),
**16:9** (1920×1080), **1:1** (1080×1080), or **4:5** (1080×1350).
**Project default** keeps the custom width/height in `config.yaml`. Both poetry
and story support these choices. API callers pass `aspect_ratio=16:9` to `/run`.
The buffer CLI accepts `python -m app.buffer run --aspect-ratio 16:9`.
Format selections apply only to that run; devtool previews use a 360-pixel short
edge, such as 640×360 for landscape. Full renders use the listed dimensions.

The dev tool's **Shake editor** adjusts image zoom, movement on each axis and
cycle times. Upload one image and click **Preview shake** to render six seconds
without a voice or transcription. **Apply to project** saves the settings directly
to the `shake` section of `config.yaml` and updates new devtool runs immediately.
Restart an already-running console or API to load the saved project settings.
Changing controls alone affects test runs; saving requires clicking Apply.

When upgrading, move your existing still images from `buffer/images/` into
`assets/images/` (preserve any same-name files). Git does not relocate ignored
local media on other machines. Keep old UI test resources in `temp/`, outside
this shared pool. In custom YAML files, replace `paths.image_buffer` with
`composition.images_directory`.
Enqueue a voice with automatic, persistent numbering:

```sh
.venv/bin/python -m app.buffer add /path/to/recording.wav
# buffer/voiceovers/voice-000001.wav
```

The command copies the source, leaving your original intact. Supported voice
extensions are `.wav`, `.mp3`, `.m4a`, `.flac`, `.ogg`, `.aac`, `.opus` and `.audio`.
Directly placed files must follow the same `voice-000001.ext` pattern. Numbers
start at 1, are processed numerically, and must not be reused, even with another
extension. Prefer the add command so complete files and IDs are published together.
Gaps after a failed enqueue are allowed.

Export `WHISPER_API_KEY` securely in your shell. The default configuration uses
Groq's `whisper-large-v3` transcription API to obtain word and segment timestamps.
The local worker still needs this speech service; it needs no Baserow or Cloudinary
credentials, table IDs or cloud name. Native Python does not load `.env`.

Run one batch, open the dashboard, or keep polling for new voices:

```sh
.venv/bin/python -m app.buffer run
.venv/bin/python main.py
.venv/bin/python -m app.buffer run --watch
```

Only choose one worker at a time. The dashboard and API share the same buffer
lock. In the dashboard, choose the input source before **Start Processing**:

- **Local buffer** (default) processes already-buffered voices. An empty buffer
  is rejected with a message to add local voices or choose Baserow.
- **Baserow** imports ready voices into an empty buffer, then processes them with
  the same local worker. If local voices are waiting, process those first. If no
  new `NOT_PROCESSED` rows are available, the console reports that instead of
  starting an empty render run.

**Refresh Buffer** reads local counts. Source selection is locked while importing
or processing, and **Stop Processing** cancels either operation cooperatively.
Choosing local mode never automatically falls back to Baserow. Cloudinary
publication remains a separate optional automation.

The buffer CLI and API retain their empty-queue no-op behavior for polling.
`--watch` checks again every five seconds. Both buffer commands accept
`--config /path/to/local.yaml`.

The default filesystem layout is:

```text
buffer/
  voiceovers/
    voice-000001.wav       # Pending voice; consumed after rendering
    .queue/               # Persistent numbering, mappings and completion events
assets/
  images/                 # Shared still images, randomly selected and retained
  videos/                 # Shared video clips, randomly selected and retained
  music/                  # Background music pool
  overlays/               # Film overlay
output/
  voice-000001.mp4         # Finished reel with the same voice ID
temp/
  voice-000001/            # Per-voice working files and failure diagnostics
```

## Story workflow (V2)

Choose **Poetry** for the existing random-footage workflow or **Story** for
script-aligned numbered frames. Story runs one section at a time. Prepare:

```text
my-section/
  script.txt       # One frame per nonempty line; numbering starts at 1
  audio.wav        # Exactly one audio.* narration file
  frames/
    1.png
    2.jpg
```

In the console choose **Story** and enter the section directory. Through the API:

```sh
curl -G -H "X-API-Key: $VIDEO_BUILDER_API_KEY" \
  --data-urlencode 'style=story' \
  --data-urlencode 'section=/absolute/path/to/my-section' \
  http://127.0.0.1:8000/run
```

The directory must be accessible to the API process (mount it into Docker when
using containers). Poll `/status` for completion, output location and diagnostic
work directory. A repeated request explicitly creates a new section render.

In the dev tool choose **Story**, paste the script, upload matching numbered
images and the narration. Upload order does not matter; names establish the
mapping. Test uploads remain in `temp/ui-test/`. Download the line alignment to
inspect matched times and scores, including after an alignment failure.

The parser writes `lines.json`, transcription writes `transcript.json`, and ordered
alignment writes `alignment.json`. Each line requires matched boundary words and
at least 70% word coverage; confidence is a matching score, not a probability.
Uncertain lines stop the build and preserve diagnostics and inputs for correction.
Limits are 200 nonempty lines and 10,000 normalized words per section.

The first frame begins at zero. Later frames follow matched line starts, with
dissolves finishing at those times. Dissolves last up to 0.4 seconds and shorten
for closely spaced lines. Opening/closing black fades also last up to 0.4 seconds.
Audio remains continuous; final duration differs by at most one output frame.
Story uses narration and full-brightness still frames. Poetry's music, grain,
shake, captions and channel-label controls are not applied to this story preset.
Choose landscape in any client or pass `aspect_ratio=16:9` to the API; omitted
formats and the frame rate use the `video` settings. Story never consumes the poetry voice queue or
contacts Baserow/Cloudinary. Section inputs and diagnostic files are retained.

## Video composition

The renderer automatically uses the bundled assets for both normal processing
and the dev tool. Inputs are a voice recording and the selected image/video pools.

- Choose one random track from `assets/music/` per reel; loop it as needed at
  **−12.0 dB** and mix it with the original voice at **+3.4 dB**.
- Set source images to **80% brightness**, then loop the film-grain video from
  `assets/overlays/` over the footage at **70% opacity** with the **difference**
  blend mode. Its audio is excluded; captions are drawn afterwards.
- Give still images a gentle, shallow shake at a moderate pace, with overscan so edges
  stay covered. The overlay and captions remain steady.

Set `composition.music_directory` and `composition.overlay_file` in `config.yaml`
to change asset locations. `footage.random_seed` also reproduces music selection.
Gains are applied before mixing, without automatic mix normalization. Audio and
video are trimmed to the voice timeline. `composition.json` in retained working
files records the selected assets and mix settings; dev-tool reports include it.

## Completion and failures

- The planner randomly selects local images while composing the video. Images
  remain reusable; `footage.random_seed` can make selections repeatable.
- A successful render saves `output/voice-000001.mp4`, persists a completion event,
  and removes only that numbered voice from `buffer/voiceovers/`.
- Failed or cancelled rendering keeps the voice for the next explicit run. Watch
  mode retries on later polls. Failed work stays under `temp/voice-000001/`.
- A process interrupted during removal can finish it from the saved receipt on
  restart without rendering again. Unrelated manual deletions produce no success event.
- Keep `buffer/voiceovers/.queue/`: it contains sequence state, source mappings,
  completion events and optional delivery receipts. These are recovery state,
  not a disposable download cache. JSON replacement protects against partial
  reads; this is not a power-loss transaction across files and remote APIs.

## Optional provider automations

Enable these later, when you want remote ingestion and publication:

```sh
.venv/bin/python -m app.automations --watch
```

This separate service never renders. It listens to persisted completion events
and refills from Baserow only when there are no pending local voices. Start the
local worker separately (use its `--watch` mode for unattended processing).

For refill, configure `baserow.table_id` and supply `BASEROW_API_TOKEN` securely.
Images always come from the configured local assets; voice import never fetches
or changes them. For publication, configure `cloudinary.cloud_name` and supply
`CLOUDINARY_API_KEY` and the actual `CLOUDINARY_API_SECRET` securely. Cloudinary
signing happens locally. Source/transcription media hosts must be reachable.
Baserow ingestion needs no Cloudinary credentials; publication is independent.

The audio table needs editable columns `ID`, `Audio URL`, `Status`, `Output File`,
`Output URL`, and `Error`. Ready rows use `NOT_PROCESSED`. Use a Baserow database
token with Read/Update access to audio rows. No image table is required.
Downloads must be direct HTTP(S) media links.

Refill imports pending voices into numbered files and remembers their original
server, table and internal row ID. Local filenames never depend on editable sheet labels, and rows already
imported are not imported again while awaiting delivery.

For example, importing internal sheet row `92` as `voice-000001.audio` produces
`voice-000001.mp4`; the saved mapping sends its upload URL back to row `92`.
The voice sequence is a local queue ID, separate from the original sheet row ID.

For each consumed voice with a Baserow mapping, the listener:

1. Uploads its finished reel to Cloudinary.
2. Saves the confirmed upload URL locally.
3. Writes the URL and `DONE` to the original row in one acknowledged update.

An upload or sheet outage leaves local output and events available. A failed sheet
write retries using the saved URL, without another upload. An ambiguous upload
requires reconciliation using its delivery receipt; it is not blindly retried.
Unmapped local voices never cause a sheet update or an upload. Changed table/server
configuration is rejected for older events, protecting against the wrong sheet.

## Docker and API

The image includes Python 3.13, dependencies, FFmpeg and the bundled assets. Compose passes
values from `.env`; the native application does not. On Linux set `LOCAL_UID` and
`LOCAL_GID` to your user and group IDs before starting containers.

```sh
docker compose build dashboard
docker compose run --rm dashboard
# Alternative unattended local worker:
docker compose --profile worker up worker
# Optional, separate provider automation:
docker compose --profile automations up automations
```

Set `VIDEO_BUILDER_API_KEY` for the optional API, then run `python api.py` or
`docker compose --profile api up api`. Authenticated `GET /run` starts the same
local worker; `GET /status` reports its state. Both require `X-API-Key`.
See [operations](docs/operations.md) for configuration and recovery details.

## Development checks

```sh
.venv/bin/ruff check app tests devtools/video_test api.py main.py
.venv/bin/ruff format --check app tests devtools/video_test api.py main.py
.venv/bin/mypy
node --check devtools/video_test/static/app.js
.venv/bin/python -m pytest -q tests devtools/video_test/tests
```

Tests exercise real FFmpeg output and local HTTP downloads, including numbered
queue consumption, event recovery, provider isolation, upload ordering and sheet
retry behavior. External transcription, Baserow and Cloudinary are mocked; tests
do not establish successful paid transcription or live publication.
Renderer tests measure decoded audio gains, RGB difference blending, looping and
smooth image movement. CI includes the dev-tool suite as well as the core suite.
Container startup and live provider processing require separate validation.

See [architecture](docs/ARCHITECTURE.md) for the composition boundaries. The
[local media test tool](devtools/video_test/README.md) remains available for manual
voice/image testing. Historical [n8n examples](n8n/README.md) are optional integrations.
