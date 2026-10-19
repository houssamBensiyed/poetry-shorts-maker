# Poetry Shorts Maker — AI Video Builder

Turn voiceovers in Google Drive into vertical videos using your own background clips and images. Google Sheets is the job list. Open the terminal dashboard, press **Start Processing**, and the app handles jobs one at a time. There is no scheduler and no web frontend.

**The two independent rules:** sentences of at least three words start new footage; shorter sentences keep the current clip playing. Every sentence gets captions. Words appear progressively, and the completed caption stays visible through silence until the next sentence starts.

## 1. Install Python and FFmpeg

Install **Python 3.11 or newer** from [python.org](https://www.python.org/downloads/). On Windows, select “Add Python to PATH” during installation.

Install **FFmpeg**, including `ffprobe`, with the **libass** subtitle filter and **libx264** encoder:

- Ubuntu/Debian: `sudo apt install ffmpeg`
- macOS with Homebrew: `brew install ffmpeg`
- Windows: install a full FFmpeg build linked from [ffmpeg.org/download.html](https://ffmpeg.org/download.html) and add its `bin` folder to PATH.

Open a terminal and check:

```sh
python --version
ffmpeg -version
ffprobe -version
```

Open the repository folder in your terminal. All subsequent commands run there:

```sh
python -m venv .venv
```

Activate it on macOS/Linux:

```sh
source .venv/bin/activate
```

Or Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install the tested dependency versions:

```sh
python -m pip install -r requirements.lock
```

`requirements.txt` lists the supported dependency ranges; `requirements.lock` records the exact versions tested on Python 3.12/Linux. If a different supported platform cannot install those exact versions, use `python -m pip install -r requirements.txt`, then run the tests below.

For the prepared Codex cloud machine, activate the existing environment with `source /workspace/poetry-shorts-maker/.venv/bin/activate` and work in `/workspace/poetry-shorts-maker`.

## 2. Set up Google access

In [Google Cloud Console](https://console.cloud.google.com/), create or select a project. Under **APIs & Services → Library**, enable both **Google Sheets API** and **Google Drive API**.

Choose one authentication method:

### Option A: Service account with a Google Workspace Shared Drive

1. Open **IAM & Admin → Service Accounts → Create service account**.
2. Open the account, select **Keys → Add key → Create new key → JSON**.
3. Save the downloaded JSON as `credentials/service-account.json`. Keep it private.
4. Find `client_email` in that file. Share the spreadsheet with that email as **Editor**.
5. Give that account permission to read the input voiceover files.
6. Add the service account to your **Shared Drive** with permission to upload into the output folder. Your Workspace administrator may need to do this.

**A service account has no personal Drive storage quota.** Merely sharing a folder in a personal “My Drive” with it does not make uploads work. Use a Workspace Shared Drive, or choose Option B.

### Option B: Sign in to your own Google account (personal My Drive supported)

1. In the Cloud Console, configure the OAuth consent screen. If the app is in Testing mode, add your Google account as a test user.
2. Under **APIs & Services → Credentials**, create an **OAuth client ID → Desktop app**.
3. Download the client JSON and save it as `credentials/oauth-client.json`.
4. On your own computer, where a browser can open, run this one-time helper:

   ```sh
   python authorize_google.py
   ```

5. Sign in and approve Sheets and Drive access. The helper writes `credentials/google-user.json`.
6. Set `google.credentials_file: credentials/google-user.json` in `config.yaml`.

For a cloud machine, securely place the resulting token file in its `credentials` directory; never paste it into chat or commit it. OAuth test-mode refresh tokens may expire after seven days; reauthorize or configure the OAuth app appropriately. Both authentication methods refresh access tokens automatically.

All files under `credentials/` are ignored by Git. Do not place credential values in YAML. Google permissions determine which input files can be read and who can view outputs; the app never makes uploads public.

## 3. Prepare the spreadsheet

Put these exact headers in the first row (their order may vary):

| ID | Voice Drive Link | Status | Output Link | Error |
|---|---|---|---|---|
| 009 | https://drive.google.com/file/d/YOUR_FILE_ID/view | NOT_PROCESSED | | |

Use one row per voiceover. `ID` is a display label, not a unique database key; duplicate labels are supported. The app updates the actual worksheet row. **Do not sort, insert, or delete rows while a run is active.** It takes a snapshot of pending row positions when a run begins.

Supported statuses are `NOT_PROCESSED`, `PROCESSING`, `COMPLETED`, and `FAILED`. Only `NOT_PROCESSED` rows are selected. Unknown statuses are logged and skipped.

In `config.yaml`, set:

```yaml
google:
  credentials_file: credentials/service-account.json  # or credentials/google-user.json
  spreadsheet_id: YOUR_SPREADSHEET_ID
  worksheet_name: Sheet1
  output_drive_folder_id: YOUR_FOLDER_ID
```

The spreadsheet ID is between `/d/` and `/edit` in its URL. The folder ID is after `/folders/` in the output folder URL. Input links can use `/file/d/ID/view`, `/open?id=ID`, or `/uc?id=ID`. Bare file IDs are accepted too.

## 4. Add footage and check the font

Place videos in `footage/videos/` and images in `footage/images/`. Supported extensions:

- Videos: `.mp4`, `.mov`, `.mkv`, `.webm`, `.avi`, `.m4v`
- Images: `.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`

At least one readable item is required. Corrupted files are skipped with a warning. Use footage you have permission to use.

**Special Elite is included** at `assets/fonts/SpecialElite-Regular.ttf`, with its Apache 2.0 license. If it is missing, restore it from this repository or [Google Fonts](https://fonts.google.com/specimen/Special+Elite). You do not need to install the font system-wide; FFmpeg loads the supplied file. If changing fonts, set both `font_file` and the font's internal `font_name` in YAML.

## 5. Configure quality, Whisper, and captions

Edit `config.yaml` with a text editor; keep YAML indentation consistent. Paths are relative to that file, regardless of the terminal's current directory.

Defaults are vertical 1080×1920, 30 FPS, H.264 video, AAC voiceover audio, and Special Elite white captions with a black outline. Footage is scaled proportionally and center-cropped, never stretched. Source footage audio is discarded. Voiceover playback speed and silence are preserved; audio is encoded to AAC, not copied bit-for-bit.

Transcription uses **your hosted OpenAI-compatible API**, not a local model. No Whisper model or weights are downloaded, and no GPU is needed. Fill in your provider's exact HTTPS endpoint and model name:

```yaml
whisper:
  model: "YOUR_PROVIDER_MODEL_NAME"
  api_url: "https://YOUR_PROVIDER_HOST/v1/audio/transcriptions"
  api_key_env: WHISPER_API_KEY
  language: null
  word_timestamps: true
  timeout_seconds: 120
```

Set `WHISPER_API_KEY` securely in the process environment; do not put the key in YAML or chat. On macOS/Linux:

```sh
read -rsp 'Transcription API key: ' WHISPER_API_KEY
export WHISPER_API_KEY
```

The provider must accept a Bearer-authenticated multipart transcription request with `response_format=verbose_json` and `timestamp_granularities[]=word` plus `timestamp_granularities[]=segment`. The API must return `text`, `words` (word/start/end), and `segments` (text/start/end). Merely accepting an OpenAI-compatible request does not guarantee timestamp support: choose a model that supports these fields. Missing or inconsistent timestamps cause a clear failure, never estimated timing.

A temporary mono 16 kHz FLAC copy is sent to the API. This preserves the voiceover clock and avoids unsupported downloaded file extensions. The original downloaded audio is still used for the final video, with its silence and playback speed unchanged. The app conservatively limits the transcription upload to 24 MB and reports an error for longer files; it does not silently trim them. Your provider may impose a smaller limit or charge for transcription. Repeated transient requests may also be billed. There is no local inference fallback.

Some APIs omit punctuation from individual words. The app restores punctuation from the full transcript only when every letter/digit aligns with the timestamped words; otherwise it fails rather than silently changing the text. Sentence grouping still crosses API segment boundaries.

Other useful settings:

- `footage.random_seed`: an integer for reproducible choices, or `null` for randomness. Reproducibility assumes unchanged source files and transcription.
- `footage.minimum_words_for_new_clip`: defaults to **3**, implementing the specified short-sentence behavior.
- `footage.avoid_immediate_repetition`: defaults to `true`; repetition is allowed with only one source.
- `captions.position`: `top`, `center`, or `bottom`.
- `captions.font_size`, `safe_margin`, `max_words_per_line`, `color`, `outline_color`, `outline_width`: appearance and wrapping. Font size automatically shrinks when necessary to fit the safe area; an impossible layout fails clearly.
- `video.crf`: lower is higher quality/larger files; default 20.
- `video.preset`: faster settings trade file size for render speed.
- `video.threads`: bounds FFmpeg CPU use; default 2.

This version deliberately requires `animation: word_by_word`, `persist_during_silence: true`, and `word_timestamps: true`. Disabling them would violate the core caption behavior and is rejected.

## 6. Open the dashboard

With the virtual environment active:

```sh
python main.py
```

Click buttons with the mouse, or use Tab and Enter:

- **Refresh Jobs** reads counts without processing.
- **Start Processing** selects pending jobs and processes them sequentially.
- **Stop Processing** requests cancellation and prevents the next job from starting. FFmpeg terminates promptly. Google operations stop between requests/chunks (network timeout is 30 seconds); Transcription stops between API requests; an in-flight request waits for its response or the configured timeout (120 seconds by default). The dashboard stays responsive while waiting.
- **View Logs** focuses the live log panel and shows the full log-file location.
- **Exit** or **q/Ctrl+C** asks for confirmation when this dashboard owns an active run.

The dashboard can open before Google is configured. Start/Refresh then reports the missing settings clearly. Rendering progress comes from FFmpeg media timestamps, weighted across normalization (first half) and final caption/audio encoding (second half). Other stages show indeterminate progress.

Outputs are saved in `output/` and uploaded to your Drive folder. The spreadsheet receives the output link and `COMPLETED` status. A failure marks the row `FAILED` where possible and processing continues with the next row.

Live logs and `logs/video_builder.log` share the same logger. Files rotate at 5 MB with three backups. Detailed footage/caption decisions are DEBUG entries in the file log. Treat logs as private: they contain job information and Drive file links, but the app does not deliberately log credential contents or API keys.

## 7. Optional HTTP triggering

The API is independent of the terminal dashboard and uses the exact same processing engine. It does not start processing until requested.

Set a private API key in the environment, **not** in YAML. On macOS/Linux, read it without displaying it or placing it in shell history:

```sh
read -rsp 'API key: ' VIDEO_BUILDER_API_KEY
export VIDEO_BUILDER_API_KEY
```

On Windows PowerShell 7, you can use `$env:VIDEO_BUILDER_API_KEY = Read-Host -MaskInput 'API key'`.

Start the server:

```sh
python api.py
```

It binds to localhost on port 8000 by default. Both endpoints require `X-API-Key`. From another terminal with the same key configured:

```sh
curl -H "X-API-Key: $VIDEO_BUILDER_API_KEY" http://127.0.0.1:8000/status
curl -H "X-API-Key: $VIDEO_BUILDER_API_KEY" http://127.0.0.1:8000/run
```

`/run` returns HTTP 202 immediately with `accepted`, or `already_running` if a run owns the lock. `/status` reports real progress and errors. A lock keyed by spreadsheet identity prevents the API and TUI (or another process using this app) from running that spreadsheet simultaneously on the same machine. An idle instance reads the active instance's shared status. Stop the process that owns a run to cancel it; an observer dashboard's Stop button is disabled. Locks are released by the OS on process exit. There is no distributed lock across different machines: use only one host per spreadsheet.

For remote automation, keep Uvicorn bound to localhost and put a TLS reverse proxy in front of it. Forward `X-API-Key`, restrict callers as appropriate, and disable caching/prefetching of `/run`. Never put keys in query strings. Do not expose an unprotected raw Uvicorn port. Remote proxy provisioning is outside this application.

## 8. Recover interrupted or failed jobs

No failed row is retried automatically. After a crash or forced shutdown, a row can remain `PROCESSING` because the process was unable to contact Sheets. On the next run the app warns about these rows rather than silently processing them twice.

1. Make sure no instance is still processing that spreadsheet.
2. Inspect the row's Output Link, Drive folder, and local `output/` folder.
3. Check `logs/video_builder.log` and the job directory under `temp/`.
4. If an upload finished but the Sheets update failed, `uploaded.json` contains its URL. Set the row to `COMPLETED` and copy that URL manually; do not render/upload it again.
5. Otherwise fix the error, set the row's Status to `NOT_PROCESSED`, clear its Error, and press Start Processing.

Cancelled jobs are marked `FAILED` with a recovery message where Sheets is reachable. Failed directories retain the downloaded voiceover, transcript, plan, ASS subtitles, FFmpeg logs, and failure metadata as available. Partial MP4 files are removed. Successful job directories are cleaned up; the finished local MP4 remains. Original Google Drive voiceovers are never changed or deleted.

Uploads and Sheets updates cannot be made into one atomic transaction. A network failure exactly as Drive completes an upload can leave a file in Drive without a recorded URL. Inspect the Drive folder before manually retrying to avoid duplicates. Filenames contain the worksheet row and a unique job token.

## How the timeline works

`app/timeline.py` is pure Python and has no dependency on Google, FFmpeg or Whisper. API word timestamps are validated; missing, unordered, out-of-range, or overlapping sentence timestamps fail rather than being invented.

Words are grouped across Whisper segment boundaries using `.`, `?`, `!` (and their full-width equivalents). Text with no sentence-ending punctuation (including a final unpunctuated fragment) is split every **20 words**. A long sentence that ends in punctuation stays intact, even when it crosses Whisper segment boundaries. It is a simple punctuation heuristic, not a language parser: abbreviations can cause extra sentence breaks. Every word is retained.

An initial footage item starts at time zero. Each qualifying sentence beginning after zero starts a new interval. Short sentences cause no footage reset. Sources that are too short loop; longer videos get a random valid source offset; images fill their entire interval. Cut boundaries are quantized to output frames, so speech-aligned cuts can differ by up to half a frame. Intervals shorter than a frame may not have a representable visible frame. The final frame count covers the voiceover within one output frame.

Caption events are independent. Each event contains the **same complete prewrapped sentence**, with unrevealed words transparent in both fill and outline. This reserves the final layout while visible words accumulate. It is not karaoke highlighting. ASS timing has centisecond precision, and display is limited by the output frame rate; words with the same representable start time appear together. Before the first word, captions are absent; after it, captions remain until audio end. Silence-only audio produces footage without captions.

## Testing and project layout

```sh
python -m pytest -q
```

Tests cover the short-sentence threshold, leading short sentences, no qualifying sentences, random repetition, coverage, progressive reveal, silence persistence, timestamp rejection, and final captions. Real FFmpeg tests generate synthetic audio/footage, encode MP4s, inspect FFprobe output and decoded pixel masks to verify progressive visibility, fixed word positions, silence persistence, and final captions. They also exercise looping video and still images. API, mocked Google/Whisper adapters, processor failure recovery, cross-process locking, cancellation, and a headless Textual dashboard are tested without credentials.

Core files:

- `app/timeline.py`, `app/captions.py`: deterministic plans and stable ASS captions.
- `app/footage.py`, `app/video_builder.py`: media inspection and FFmpeg rendering.
- `app/transcription.py`: hosted transcription API client and normalized timestamped transcript.
- `app/sheets.py`, `app/drive.py`, `app/google_client.py`: authenticated Google operations and bounded retries.
- `app/processor.py`, `app/state.py`: sequential processing, shared state and OS locking.
- `app/dashboard.py`, `app/logger.py`: interactive UI and rotating/live logs.
- `main.py`, `api.py`: the two normal entry points.
- `authorize_google.py`: optional one-time desktop OAuth setup.

## Troubleshooting

| Problem | What to do |
|---|---|
| Missing spreadsheet/folder setting | Fill in the IDs in `config.yaml`; use IDs, not full URLs. |
| Credentials or permission error | Check the configured JSON path, enabled APIs, spreadsheet sharing, and input/output folder permissions. |
| Drive storage quota error with service account | Use a Workspace Shared Drive or user OAuth. |
| OAuth token expired/revoked | Move the old user token aside and rerun the desktop authorization helper. |
| Missing or corrupt audio | Check the input link and read access; download the file yourself and confirm it plays. |
| Missing word timestamps | Use a provider/model that returns verbose JSON with word and segment timestamps; inspect the retained logs. No replacement timestamps are generated. |
| Transcription API gets HTTP 401/403 | Check your provider API key, account permissions, and allowed endpoint hostname. |
| No footage | Add supported videos/images to the configured folders. Read warnings for corrupt files. |
| Missing font/libass | Restore the supplied font; install a full FFmpeg build with the `ass` filter. |
| Render fails | Inspect `temp/row-…/*.log` and `plan.json`; retry only after fixing the cause. |
| Very slow render/transcription | Choose a faster hosted transcription model with timestamp support, a faster video preset, or a lower output resolution. |
| API refuses to start | Set `VIDEO_BUILDER_API_KEY` in the same terminal that launches it. |
| Run already active | Check the other TUI/API process; do not delete lock files to bypass an active run. |

Cloud egress needs package registries for installation, your configured transcription API hostname, `sheets.googleapis.com`, `www.googleapis.com`, and `oauth2.googleapis.com` for Google requests. Model-download hosts are not needed. The transcription key belongs to your chosen provider; no OpenAI account is assumed.

### Where Google Drive is used

The Sheet is the job list, not the audio storage. For each pending row, `app/sheets.py` reads the **Voice Drive Link** column. `app/processor.py` passes that row's exact link to `Drive.download`, which extracts its file ID and downloads the audio through authenticated Google Drive API access. The audio goes to your transcription API. After rendering, Drive is used again to upload the MP4 to your configured output folder. Sheets then receives the resulting URL in **Output Link**. A Drive share link alone does not grant this application access to a private file.

### Validation status

Local tests exercise actual FFmpeg rendering and the TUI/API. Google and transcription API tests use controlled fakes, including multipart payload and timestamp validation. No live Google credentials or transcription provider endpoint/model/key were supplied, so external downloads, uploads, Sheet updates, and paid transcription requests have **not** been verified. No local Whisper model is downloaded or used.
