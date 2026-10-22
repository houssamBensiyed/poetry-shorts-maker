# Video Builder Test

A local testing UI for the real video builder. Kept on the `test/video-builder-ui`
branch, under `devtools/`; it is not included in the production Docker image and
is not loaded by `main.py` or `api.py`.

From the repository root, with the project's existing Python dependencies and
FFmpeg/FFprobe installed:

```sh
.venv/bin/python -m devtools.video_test
```

Open **http://127.0.0.1:8765**. Use `--port 8766` if that port is busy.
No Node build, new runtime dependency, Baserow or Cloudinary setup is needed.
The launcher only listens on your own computer.

1. Upload 3–10 JPG, PNG or WebP images. Each is decoded, its EXIF orientation is
   applied, and it is saved as PNG directly in the image buffer.
2. Upload one voice recording. It is saved unchanged in the voice buffer and
   checked with the project's FFprobe helper. You can listen to it in the UI.
3. Choose fast preview or the full video size from `config.yaml`.
4. Choose caption timing and click **Run test**.
5. Watch progress, play/download the validated MP4, or save the JSON test report.

**Sample text** works without credentials. Its words are spread evenly across the
voice duration, so these are test timings, not a transcription of the recording.
Edit the text to test captions. Sentences with at least three words trigger image
changes with the default configuration. The UI uses the real `build_plan()` image
selection, so a run may not select every buffered image. The seed makes selection
repeatable. **Speech recognition** uses the existing configured transcription
provider and requires its endpoint, model and environment key. The recording is
sent to that provider, which may charge for the request. The launcher does not
load `.env`; export credentials in your shell if using that option.

The UI calls `sentences_from_words()`, `build_plan()` and
`app.video_builder.render()` directly. It checks the same timeline, caption,
codec, audio, duration and output invariants as the core renderer. It tests local
media preparation and rendering; production queue, remote downloads, publication
and repository status updates are outside its scope. It does not write to
`config.yaml`.

Uploads are limited to 20 MB per image (40 million pixels) and 100 MB / 10 minutes
per voice recording. A session supports one voice recording and up to ten images.
Only one test runs at a time across browser sessions. Uploads and removals are
blocked while that session is rendering. **Stop test** requests cancellation of
FFmpeg; the UI waits for the worker to finish before allowing edits.

Files use unique session and run folders under the configured paths:

```text
buffer/images/ui-test/<session>/          Decoded images
buffer/voiceovers/ui-test/<session>/      Original voice recording
output/ui-test/<session>/<run>.mp4        Completed videos
temp/ui-test/<session>/<run>/             Plans, ASS captions, FFmpeg logs, report
```

These files are already covered by the repository's Git ignore rules. Test files
remain on disk until you remove them. The UI only removes files selected with its
remove buttons. Refreshing the page restores the session while the server is
running. Restarting the server creates a new session; old outputs remain on disk.
The preview and download refer to the most recent run; earlier runs remain in the
output folder. The UI is a local developer tool and should not be exposed through
a public proxy or deployed.

Run the UI integration tests separately from the production tests:

```sh
.venv/bin/python -m pytest -q devtools/video_test/tests
.venv/bin/ruff check devtools/video_test
.venv/bin/ruff format --check devtools/video_test
node --check devtools/video_test/static/app.js
```

Keep this testing work on its dedicated branch; do not merge it into `main`.
