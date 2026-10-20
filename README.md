# Poetry Shorts Maker — AI Video Builder

Use **[Baserow](https://baserow.io)** as your online spreadsheet, paste direct audio links into it, and generate vertical captioned videos on your own computer. No Google Cloud project, Google account, Drive integration, or OAuth setup is required.

The workflow is:

**Baserow audio + image tables → download audio and randomly selected images → local buffers → transcription API + FFmpeg → upload MP4 to Cloudinary → save its URL and mark the audio row DONE.**

Jobs run sequentially only when you press Start Processing or send an authenticated HTTP trigger. There is no scheduler, database server, or web frontend to install.

**Core rules:** sentences of at least three words start new footage; one- and two-word sentences keep the current footage playing uninterrupted. Every sentence gets captions. Words appear progressively at their timestamps, and the completed caption stays visible through silence until the next sentence begins.

## 1. Install Docker

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) on Windows/macOS, or Docker Engine with the Compose plugin on Linux. Start Docker and open a terminal in this project folder.

The container includes **Python 3.13**, FFmpeg/FFprobe with libass/H.264, Special Elite, and the pinned Python dependencies. You do not need Python or FFmpeg installed on your computer. Transcription uses your API provider; no model or GPU is required.

```sh
docker compose build dashboard
```

On Linux, match your account's file ownership before running containers:

```sh
export LOCAL_UID=$(id -u)
export LOCAL_GID=$(id -g)
```

Docker Desktop normally works with the defaults. The app runs as a non-root user.

If your corporate/cloud network intercepts HTTPS and the build reports a certificate error, use your administrator-approved CA bundle without disabling verification:

```sh
docker build --secret id=build_ca_bundle,src=/path/to/approved-ca-bundle.pem -t poetry-shorts-maker:local .
```

The bundle is used only during installation and is not copied into the image. A proxy intercepting runtime HTTP clients also requires the trusted CA to be configured for those clients.

## 2. Create two Baserow tables

1. Create an account at **https://baserow.io**.
2. Create a database and an audio-job table named `Video Jobs`.
3. Add these fields with the exact names below. Use ordinary editable fields, not formulas.

| Field | Recommended Baserow type | Example |
|---|---|---|
| ID | Single line text | 009 |
| Audio URL | URL | https://your-file-host.example/voiceover.wav |
| Status | Single line text | NOT_PROCESSED |
| Output File | Single line text | Leave empty; the app records the local MP4 path |
| Output URL | URL | Leave empty; the app records the uploaded Cloudinary link |
| Error | Long text | Leave empty |

`Status` may alternatively be a Single select field with all four options: `NOT_PROCESSED`, `PROCESSING`, `DONE`, `FAILED`. Existing legacy `COMPLETED` rows are still treated as finished and skipped. Single line text is simpler to configure. Use these exact uppercase values. Blank/unrecognized statuses are skipped with a warning.

The `ID` field is just your display label; duplicates are allowed. Updates use Baserow's internal row ID, so changing the row order will not send an update to the wrong record. Avoid editing/deleting pending rows during a run, because the app takes a snapshot when it starts.

4. Create a **separate table** named `Images`, with an optional `ID` text field and a required **`Image URL`** URL field. Put one direct image link in each row. No status column is needed for images: they are reusable.
5. In Baserow settings, open **Database tokens**, create a token, and grant **Read + Update** on `Video Jobs` and **Read** on `Images`. Create/delete permissions are not needed.
6. Find the numeric **table IDs** in each table URL or Baserow's generated API documentation. Use table IDs, not database or view IDs.
7. Set both IDs in `config.yaml`:

```yaml
baserow:
  api_url: https://api.baserow.io
  table_id: 123456        # Video Jobs
  images_table_id: 654321 # Images
  api_key_env: BASEROW_API_TOKEN
```

`api_url` is the Baserow server root, not the browser table link. Self-hosted Baserow can use its HTTPS server URL. No Google project or service-account JSON file is involved.

## 3. Set up Cloudinary and your transcription provider

Create an account at **[Cloudinary](https://cloudinary.com)**. In its dashboard/API Keys settings, find your **cloud name**, **API key**, and **API secret**. Set the cloud name in YAML:

```yaml
cloudinary:
  cloud_name: "YOUR_CLOUD_NAME"
  api_key_env: CLOUDINARY_API_KEY
  api_secret_env: CLOUDINARY_API_SECRET
  folder: poetry-shorts
  chunk_mb: 10
  timeout_seconds: 120
```

No buckets, IAM policies, Google credentials, or upload preset are needed. The app uses signed video uploads. The API secret stays in the application process and is used to sign requests; it is never sent in an upload request. Uploaded videos use **public HTTPS delivery URLs**. Cloudinary account storage/bandwidth/file-size limits still apply; check your plan for your intended video sizes.

Copy `.env.example` to `.env` in the project folder. Open `.env` in a text editor and enter your values:

```dotenv
BASEROW_API_TOKEN=your_baserow_database_token
CLOUDINARY_API_KEY=your_cloudinary_api_key
CLOUDINARY_API_SECRET=your_cloudinary_api_secret
WHISPER_API_KEY=your_transcription_provider_key
VIDEO_BUILDER_API_KEY=your_private_http_trigger_key
```

The HTTP trigger key is needed only if you run the optional API. `.env` is ignored by Git and excluded from the Docker image. Never share or commit it. Compose passes the keys to the containers. Native Python users should export these variables in their shell; the app itself does not read `.env` files.

In `config.yaml`, set your transcription provider's exact HTTPS endpoint and model:

```yaml
whisper:
  model: "YOUR_PROVIDER_MODEL"
  api_url: "https://YOUR_PROVIDER_HOST/v1/audio/transcriptions"
  api_key_env: WHISPER_API_KEY
  language: null
  word_timestamps: true
  timeout_seconds: 120
```

You selected **Naga (naga.ac)**. Its exact endpoint/model and timestamp support have not yet been verified, so those values are deliberately blank in the supplied configuration.

The provider must accept Bearer-authenticated multipart requests with `response_format=verbose_json` and `timestamp_granularities[]=word` plus `timestamp_granularities[]=segment`. It must return `text`, `words` (word/start/end), and `segments` (text/start/end). Not every OpenAI-compatible audio model supports these fields. Missing timestamps cause a clear failure; the app never invents them.

Only a temporary mono 16 kHz FLAC copy is submitted to the transcription API. The original buffered audio is used for the final video. The upload is limited to 24 MB; the provider may impose a smaller limit or charge for requests/retries. There is no local model fallback.

## 4. Add your audio and image links

Put an accessible **direct HTTP/HTTPS audio URL** in each audio row's `Audio URL` field, and set its status to `NOT_PROCESSED`. The host can be any service that supplies the actual file, including an object-storage URL, your own server, or a signed download link. Normal redirects are followed.

A browser preview/share page is not necessarily a download link. Links that return HTML/login pages, require browser cookies, or require a private provider SDK will not work. Use the provider's direct-download or signed URL instead. The application does not bypass a host's access controls. The file must contain audio that FFprobe can read; the URL does not need a file extension.

Downloads are streamed to disk and finalized only after completion. The original audio bytes are kept in **`buffer/voiceovers/`**, even after successful processing.

The app reads the separate `Images` table and logs the total row count and number of distinct usable links. After transcription determines the footage intervals, it randomly selects from the full image pool, avoiding immediate repetition when possible. **Only selected images are downloaded** into `buffer/images/row-…/`; an image reused within the same video is downloaded once. Duplicate links in the image table count once. All downloads finish and image decoding is verified before rendering begins. Broken selected images fail that job visibly; they are not silently substituted.

Images must be still JPEG, PNG, WebP or BMP files; animated images are rejected. Originals stay buffered after success/failure. Each new job downloads its selected images afresh, so a changed URL is not silently served from a stale cross-job cache. You can clear old buffer files when no run needs them. Only the finished MP4 is uploaded to Cloudinary; the buffered source images/audio are not uploaded there.

Optional download settings:

```yaml
downloads:
  timeout_seconds: 60
  max_file_mb: 256
```

The Baserow image table must contain at least one usable link. All background images come from this table and are downloaded into `buffer/images/`. Special Elite is bundled in `assets/fonts/` with its license. No online font service is used.

## 5. Start the dashboard

```sh
docker compose run --rm dashboard
```

Use an interactive terminal, not detached mode. Buttons support mouse clicks and Tab/Enter:

- **Refresh Jobs:** read Baserow job counts without downloading or rendering.
- **Start Processing:** process all pending rows sequentially.
- **Stop Processing:** prevent new jobs and cancel the current job safely. FFmpeg stops promptly; HTTP operations stop at the next chunk/request boundary or timeout. The UI remains responsive.
- **View Logs:** focus live logs and display the full log-file location.
- **Exit** or **q/Ctrl+C:** confirm stopping an active run before exiting.

The dashboard opens before credentials are configured, and reports missing settings when you start/refresh. Rendering progress comes from FFmpeg timestamps; other stages use an indeterminate indicator.

The app marks an audio row `PROCESSING`, downloads/checks its audio, transcribes, plans its random image choices, downloads the selected images, renders and uploads the MP4. **Only after a complete upload is confirmed** does it update that same row to **`DONE`**, set **`Output URL`** to the uploaded HTTPS link, and keep **`Output File`** as a local path such as `output/row-42-….mp4`. The URL is clickable from Baserow; the local copy remains on the computer running Docker. Uploads use 10 MB chunks with bounded retries and progress messages in the live logs. Failures are marked `FAILED` where possible, with a readable `Error`; the next pending row still runs.

## 6. Files and configuration

These folders remain on your computer when containers are rebuilt or removed:

| Folder | Contents |
|---|---|
| `buffer/voiceovers/` | Original downloaded audio, retained after success/failure |
| `buffer/images/` | Selected images downloaded for each video, retained after success/failure |
| `output/` | Completed MP4 files |
| `temp/` | Failed-job diagnostics; successful render work directories are removed |
| `logs/` | Rotating logs (`video_builder.log`, 5 MB × 4 files) |

Compose mounts `config.yaml` read-only, and buffer/output/temp/logs read-write. Credentials and personal media are excluded from the image build context. Logs include job IDs and file paths; avoid sharing them indiscriminately. Signed input URLs may appear in the authenticated dashboard/API state but are not intentionally written into download error messages.

Useful YAML settings:

- `video.width`/`height`/`fps`: default 1080×1920, 30 FPS; dimensions must be even.
- `video.crf`: default 20; smaller means higher quality/larger output.
- `video.preset`/`threads`: render speed and CPU use.
- `footage.minimum_words_for_new_clip`: default **3**, preserving short-sentence continuity.
- `footage.random_seed`: integer for reproducible plans, or `null` for randomness.
- `captions.position`: `top`, `center`, or `bottom`.
- `captions.font_size`, `safe_margin`, `max_words_per_line`, colors and outline: caption appearance.
- `paths.buffer`, `image_buffer`, `output`, `temp`, `logs`: local directories, relative to `config.yaml`.

Captions always use progressive word reveal and persistence during silence. Disabling those settings is rejected. The final MP4 uses H.264 video, AAC voiceover audio, yuv420p, and fast-start metadata. AAC encoding preserves timing and meaningful silence but is not bit-for-bit audio copying.

Rebuild after code/dependency changes with `docker compose build dashboard`. Editing YAML does not require rebuilding; restart the running entry point to reload settings. Image links are read from Baserow at the start of each processing run.

## 7. Optional HTTP automation

Set `VIDEO_BUILDER_API_KEY` in `.env`, then:

```sh
docker compose --profile api up -d api
```

Both endpoints require an `X-API-Key` header:

- `GET /run`: return HTTP 202 immediately and start pending jobs; return `already_running` if a run is active.
- `GET /status`: report real status, current job and progress.

Set the same key in your client's environment, then use:

```sh
curl -H "X-API-Key: $VIDEO_BUILDER_API_KEY" http://127.0.0.1:8000/status
curl -H "X-API-Key: $VIDEO_BUILDER_API_KEY" http://127.0.0.1:8000/run
```

A `.env` file read by Compose is not automatically exported into your terminal for curl. Never put the key in a URL.

The service listens on all interfaces inside the container, but Docker publishes it only on your computer's localhost. To change the host port, set `VIDEO_BUILDER_HTTP_PORT`. Remote access requires your own HTTPS reverse proxy forwarding the header; disable caching/prefetching for `/run`.

Inspect logs with `docker compose logs -f api`; stop with `docker compose stop api`. The default stop grace period is 180 seconds. Increase it if you increase HTTP request timeouts. Cancellation stops uploads between chunks/retries; a final confirmed upload is still committed to Baserow if Stop arrives with its response.

TUI and API share the same processing engine and a Docker lock volume keyed by Baserow server/table ID. Run them from the **same Compose project**. Do not run a different project, native process, or another computer against the same table simultaneously; this is not a distributed lock. Stop the instance that owns a run to cancel it. An observing dashboard cannot stop the other instance. Do not remove lock volumes during a run.

## 8. Recovery

Failed rows are not retried automatically. To retry, fix the problem and set `Status` to `NOT_PROCESSED`, then start again. A retry downloads a fresh copy into the local buffer.

After a crash, a row may remain `PROCESSING`. The next run warns about it and skips it. Ensure no process is still running, then inspect the row and local output before resetting it.

A video can render successfully while upload or Baserow update fails. The complete local MP4 is retained:

- `temp/row-…/rendered.json` records the local output and audio path.
- `upload.json` records the intended Cloudinary **public ID** before upload begins.
- `uploaded.json` records a confirmed upload URL/public ID.
- `failure.json` records the error, local output, and known uploaded URL.

If `uploaded.json` exists, copy its URL into `Output URL`, its path into `Output File`, and set `Status` to `DONE` manually. If the network failed exactly when Cloudinary finished uploading, check Cloudinary for the public ID in `upload.json` before retrying: a remote video may exist even if no final response was received. Never assume a timeout means the upload failed. Uploads and Baserow updates cannot be one atomic transaction. Resetting a failed job starts a new render/upload with a new public ID; inspect recovery metadata first to avoid duplicates.

Partial MP4s and incomplete downloads are removed. Failed-job transcripts, plans, subtitles and FFmpeg logs remain in `temp/`; the original audio and selected images remain in their buffers.

## Timeline behavior

Timeline planning is pure Python and independent of the online table or file host. Punctuation groups timestamped words across API segment boundaries. A long punctuated sentence stays intact; text without sentence-ending punctuation is split every 20 words. This is a punctuation heuristic, so abbreviations can cause extra breaks. All words are retained.

The first background image starts at zero. A qualifying sentence selects a new random image; shorter sentences keep the current image. Each image fills its interval. Aspect-preserving scale/center crop normalizes resolution and FPS. Shared frame boundaries prevent gaps; the final duration matches the voiceover within one output frame.

Caption layouts are precomputed for the full sentence. Unrevealed words are fully transparent but reserve their layout space, so displayed words do not jump around. This is progressive reveal, not karaoke highlighting. The full sentence stays visible until the next sentence starts; the final caption stays until audio end. ASS timing uses centiseconds and output frame precision. Before the first word, or for silence-only audio, captions can be absent.

## Tests and validation

The Python 3.13 Docker image builds successfully and **67 tests passed inside it**.

```sh
docker compose run --rm --no-deps dashboard python -m pytest -q
```

Tests cover timeline rules, real FFmpeg renders with decoded-frame caption checks, downloads through a real local HTTP server, redirects and rejected HTML/oversized/empty files, Baserow pagination/row updates with mocked responses, cancellation, recovery, TUI and authenticated API behavior. An integration test downloads real audio and a randomly selected image through a local HTTP server, retains their buffers, renders a real MP4, submits it to a simulated uploader, and verifies DONE rows are not processed twice. Online Baserow, paid transcription, and Cloudinary use controlled mocks. Upload tests check multipart signatures, chunk ranges/retries, byte preservation, cancellation, missing completion responses, and failure recovery without marking rows DONE.

Live Baserow access still requires both table IDs and your database token. Live Cloudinary uploads require the cloud name, API key and API secret; no real upload has been attempted without them. This environment's proxy currently blocks `api.baserow.io`; its network setting must be applied before live testing. Naga transcription is also unverified until its endpoint/model/key and timestamp support are configured. No claim is made that these external integrations have run successfully.

Native development remains possible with Python 3.13, FFmpeg and a virtual environment: install `requirements.lock`, export the required environment variables, then run `python main.py` or `python api.py`. No Google SDK is required.
