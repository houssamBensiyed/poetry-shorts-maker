# Video Builder Test

A local testing UI for the real video builder, under `devtools/`. It is not
included in the production Docker image and is not loaded by `main.py` or `api.py`.

From the repository root, with the project's existing Python dependencies and
FFmpeg/FFprobe installed:

```sh
.venv/bin/python -m devtools.video_test
```

Open **http://127.0.0.1:8765**. Use `--port 8766` if that port is busy.
No Node build, new runtime dependency, Baserow or Cloudinary setup is needed.
The launcher only listens on your own computer.

1. Choose **Images**, **Videos**, or **Hybrid**. Image mode needs 3–10 images;
   video mode needs 1–10 videos; hybrid needs at least one of each and starts with
   a video. Upload JPG/PNG/WebP images and MP4/MOV/MKV/WebM/M4V/AVI videos for your
   selected mode. Images are decoded, orientation-corrected and saved as PNG;
   clips are probed and saved unchanged in the temporary session directory.
2. Upload one voice recording. It is saved unchanged in the temporary session and
   checked with the project's FFprobe helper. You can listen to it in the UI.
3. Choose fast preview or the full video size from `config.yaml`.
4. Click **Run test**. Whisper transcribes the voice, then the builder creates the video.
5. Watch progress, play/download the validated MP4, or save the JSON test report.

The **Shake editor** controls zoom, horizontal/vertical movement and cycle times.
Smaller cycle times increase speed; movement percentages use the travel available
inside the zoomed image. Defaults match the existing effect. **Preview shake**
renders six seconds from the first uploaded image using the actual core filter;
it needs no voice, captions, music, overlay or transcription credentials. Changing
a control marks the previous preview as outdated. Replay the clip to compare
settings. All shake controls affect still images only.

Click **Export shake settings** to download `shake-settings.json`, containing
`version: 1` and the exact `shake` config. Share that file to describe your desired
effect, or copy its `shake` values into `config.yaml`. **Run test** uses the same
values and records them in the run report. The editor does not save to project
config automatically.

Enter an optional **Channel name** to display a steady 8 pt label below each
caption block. Leave it blank to omit it. The label scales with output width,
so its proportions match between fast preview and full-size reels. It appears
only while captions are present and accepts up to 80 characters.

Use **Play with sound** under the video to start playback with sound enabled and
volume at 100%. The label below it shows the current mute/volume state. The native
video controls can still change the volume. This only controls browser playback;
the downloaded MP4 contains the voice at +3.4 dB mixed with music at −12.0 dB.

The shared renderer automatically picks one track from `assets/music/`, loops the
film overlay from `assets/overlays/` using difference blending at 70% opacity over
images at 80% brightness, and adds gentle, shallow motion to still images.
Its 2.4/3.2-second cycles increase shake speed while
keeping image movement small. No extra uploads are required. Music and overlay
paths come from `composition` in `config.yaml`; missing assets block the test
before transcription. Overlay audio is excluded and captions are applied after
blending. Clip audio is also excluded; videos keep their natural motion and
brightness. Short clips loop to fill their section. The selection seed reproduces
footage choices, video offsets and the music choice.
The downloadable run report records the chosen assets and composition settings.

Every run uses the project's `Transcriber` and the `whisper` settings in
`config.yaml`. Set the API endpoint and model there, and put the configured API
key (normally `WHISPER_API_KEY`) in the project's `.env` file or export it in your
shell. The launcher reads only that key from `.env`; exported values take priority.
Plain and quoted values are supported, without shell commands or variable expansion.
Restart the server after changing credentials or configuration. The UI shows missing
settings and prevents a run until the API is configured.

The uploaded voice is sent to the configured Whisper service. Its returned words
and timestamps control the captions and footage changes. A transcription error fails
the test before rendering; no sample text or estimated timing is substituted.
The transcript is saved as `transcript.json` in the run's temporary folder.
With the default configuration, sentences of at least three words trigger footage
changes. The planner uses the session's selected media types and avoids immediate
repeats when another candidate exists. Hybrid starts with video and randomly picks
from both pools afterward; a one-section reel remains video-only. A run may not
use every uploaded file. The seed makes selection repeatable.

The UI calls `Transcriber.transcribe()`, `sentences_from_words()`, `build_plan()` and
`app.video_builder.render()` directly. It checks the same timeline, caption,
codec, audio, duration and output invariants as the core renderer. It tests local
media preparation and rendering; production queue, remote downloads, publication
and repository status updates are outside its scope. It does not write to
`config.yaml`.

Uploads are limited to 20 MB per image (40 million pixels) and 100 MB / 10 minutes
per voice recording, and 256 MB / 10 minutes per video. A session supports one voice
recording, up to ten images and up to ten videos.
Only one test runs at a time across browser sessions. Uploads and removals are
blocked while that session is rendering. **Stop test** requests cancellation of
FFmpeg; the UI waits for the worker to finish before allowing edits.

All uploads, generated videos and diagnostics use unique session/run folders under
`paths.temp` (normally `temp/`). The production voice buffer, shared image/video assets and output are not changed:

```text
temp/ui-test/<session>/images/               Decoded test images
temp/ui-test/<session>/videos/               Uploaded test clips
temp/ui-test/<session>/shake-previews/<id>/   Short shake previews and diagnostics
temp/ui-test/<session>/voiceovers/           Uploaded test voice
temp/ui-test/<session>/runs/<run>/video.mp4   Test video
temp/ui-test/<session>/runs/<run>/            Transcript, plan, captions, logs, report
```

For files left by older versions, stop the old server and move the legacy
`buffer/images/ui-test/`, `buffer/voiceovers/ui-test/` and `output/ui-test/` folders
into separate `images/`, `voiceovers/` and `output/` folders under
`temp/legacy-ui-test/`. Updating Git does not move those ignored local files.

These files are already covered by the repository's Git ignore rules. Test files
remain on disk until you remove them. The UI only removes files selected with its
remove buttons. Refreshing the page restores the session while the server is
running. Restarting the server creates a new session; old outputs remain on disk.
The preview and download refer to the most recent run; earlier runs remain in the
temporary session directory. The UI is a local developer tool and should not be exposed through
a public proxy or deployed.

Run the UI integration tests separately from the production tests:

```sh
.venv/bin/python -m pytest -q devtools/video_test/tests
.venv/bin/ruff check devtools/video_test
.venv/bin/ruff format --check devtools/video_test
node --check devtools/video_test/static/app.js
```

CI runs this suite alongside the core tests so renderer changes also validate the UI.
