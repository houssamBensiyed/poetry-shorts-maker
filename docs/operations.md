# Operations guide

[README](../README.md) · [Architecture and extensions](ARCHITECTURE.md) · [Critical code review](CODE_REVIEW.md)

[File links](#file-links-and-buffers) · [Transcription](#transcription-provider) · [Configuration](#configuration) · [HTTP API](#http-api) · [Recovery](#recovery) · [Troubleshooting](#troubleshooting)

## File links and buffers

The default worker reads local `voice-NNNNNN.ext` files from `buffer/voiceovers/`
and still images directly from `buffer/images/`. Use `python -m app.buffer add
/path/to/audio.wav` to copy a complete voice into the next available number.
Selection is random during planning; images are retained. An empty voice queue is
a successful no-op without provider validation.

| Location | Retention |
| --- | --- |
| `buffer/voiceovers/voice-*.ext` | Removed only after a successful render and saved receipt |
| `buffer/voiceovers/.queue/` | Retain sequence, source mappings and completion/delivery receipts |
| `buffer/images/` | Reusable local images |
| `output/voice-*.mp4` | Finished reels; retained independently of upload success |
| `temp/voice-*/` | Successful work is cleaned; failed work remains for diagnosis |
| `logs/` | Rotating logs |

Optional `python -m app.automations --watch` imports direct HTTP(S) audio links
from Baserow only when the local queue is empty, and seeds an empty image pool.
Provider downloads reject incomplete files, login pages and invalid media. Nested
legacy and test-tool buffer directories are not added to the numbered queue.
See [the local quick start](../README.md#local-quick-start).

## Transcription provider

Set `whisper.api_url` to the full HTTPS transcription endpoint and `whisper.model` to the provider's model name. The `whisper` configuration name refers to the API interface. No model weights are installed or downloaded.

Groq is the selected provider: `https://api.groq.com/openai/v1/audio/transcriptions` with `whisper-large-v3`. Both this model and `whisper-large-v3-turbo` returned word and segment timestamps in live checks. Supply your Groq key through `WHISPER_API_KEY`. See [Groq's speech-to-text documentation](https://console.groq.com/docs/speech-to-text).

The provider must accept Bearer-authenticated multipart requests with:

| Request field | Value |
| --- | --- |
| `file` | Mono 16 kHz FLAC audio |
| `model` | Your configured model |
| `response_format` | `verbose_json` |
| `timestamp_granularities[]` | Both `word` and `segment` |
| `language` | Optional configured language code |

For speech, the response must include `text`, `words` with word/start/end fields, and `segments` with text/start/end fields. The app validates timestamp order and alignment, and restores punctuation from the transcript text. Missing or inconsistent timestamps fail the job rather than being fabricated.

Whisper may estimate a word or segment end slightly beyond the recording. Ends up to 0.5 seconds beyond the actual audio duration are capped at that duration, provided speech starts inside the recording. Larger overruns, invalid timestamps and unordered words still fail validation. Word order is checked before clipping.

The temporary FLAC upload is limited to 24 MB and removed on completion, failure or cancellation, including cancellation during audio preparation. The original buffered audio supplies the final video's audio track. Your provider may impose a smaller limit and may charge for retries.

## Configuration

Edit existing values in [`config.yaml`](../config.yaml). Restart the dashboard or API to reload YAML changes. Rebuild with `docker compose build dashboard` after code or dependency changes. The local image pool is snapshotted at the start of each nonempty run. Baserow/Cloudinary settings apply only to the optional automation service.

| Setting | Purpose |
| --- | --- |
| `baserow.api_url` | Baserow server root, such as `https://api.baserow.io`; self-hosted HTTPS servers are supported |
| `baserow.table_id` | Audio-job table ID, not a database or view ID |
| `baserow.images_table_id` | A distinct image table ID |
| `downloads.timeout_seconds` | Per-request download timeout, default 60 seconds |
| `downloads.max_file_mb` | Maximum source download size, default 256 MB |
| `cloudinary.cloud_name` | Cloudinary account cloud name |
| `cloudinary.folder` | Uploaded video prefix, default `poetry-shorts` |
| `cloudinary.chunk_mb` | Upload chunk size, 5 to 20 MB; default 10 |
| `cloudinary.timeout_seconds` | Upload request timeout, default 120 seconds |
| `whisper.timeout_seconds` | Transcription request timeout, default 120 seconds |
| `video.width` / `video.height` / `video.fps` | Output dimensions and frame rate; dimensions must be even |
| `video.crf` | Smaller values increase quality and output size; default 20 |
| `video.preset` / `video.threads` | FFmpeg encoding speed and CPU use |
| `footage.minimum_words_for_new_clip` | Minimum sentence length for an image change; default 3 |
| `footage.avoid_immediate_repetition` | Avoid choosing the same image consecutively when possible |
| `footage.random_seed` | Integer for reproducible plans, or `null` for randomness |
| `captions.position` | `top`, `center` or `bottom` |
| `captions.font_size` / `captions.safe_margin` | Caption size and distance from video edges |
| `captions.max_words_per_line` | Caption line wrapping |
| `captions.color` / `captions.outline_color` / `captions.outline_width` | Caption colors and outline |
| `paths.buffer` / `paths.image_buffer` | Audio and image buffer directories |
| `paths.output` / `paths.temp` / `paths.logs` | Output, working files and log directories |

Paths resolve relative to `config.yaml`. Compose persists the default buffer/output/temp/logs locations and mounts `config.yaml` read-only. If you move paths outside those locations, update the Compose mounts too.

### Credentials and uploads

Docker Compose reads `.env` and passes credentials into the containers. The Python application itself does not load `.env`. Native Python and shell clients need exported environment variables.

Cloudinary's API secret signs requests locally and is never sent as a raw upload field. Supply the actual secret to the process; a network-proxy placeholder cannot perform local signing. Default delivery URLs are public HTTPS links. Cloudinary account storage, bandwidth and upload-size limits apply.

Do not commit `.env` or include it in shared logs. Signed input URLs may appear in authenticated dashboard/API state; download error messages intentionally avoid logging those URLs.

### Timeline and captions

Punctuation groups timestamped words across transcription segments. A long punctuated sentence stays intact. Text without sentence-ending punctuation is split every 20 words. This is a punctuation heuristic, so abbreviations can cause extra breaks.

The first image begins at zero. Sentences meeting the word threshold start a new image selection; shorter sentences retain the current image. Images are scaled and center-cropped while preserving aspect ratio. Shared frame boundaries avoid gaps, and the output duration matches the voiceover within one frame.

Caption layout reserves space for the entire sentence before revealing words at their timestamps. Words do not shift as new words appear. The completed sentence remains through silence until the next sentence begins; the final caption persists until the audio ends. Before the first word and for silence-only audio, captions can be absent.

Progressive word reveal, word timestamps and silence persistence are required by this version. Output uses H.264, AAC, yuv420p and fast-start metadata. AAC preserves timing and meaningful silence but does not copy the original audio bytes unchanged.

## Console input source

Run `python main.py` and choose **Local buffer** or **Baserow** before starting.
Local mode processes only numbered voices already on disk. If none are waiting,
the console rejects the request and asks you to add local voices or import them
from Baserow. It never contacts Baserow automatically to resolve an empty selection.

Baserow mode imports `NOT_PROCESSED` rows into an empty local voice buffer, then
runs the local worker. Configure the source table and database token first;
Cloudinary credentials are not required for this step. A populated local queue
must be processed first. No eligible source rows, missing configuration and import
errors appear in console status. Successfully imported files remain available if
a later import fails; switch to local mode to process them after fixing the cause.

**Refresh Buffer** refreshes disk counts regardless of the selected source.
**Stop Processing** also stops an active import at its cancellation checkpoints.
The source selector is disabled until import/processing finishes. Completion
publication and sheet updates remain in the separate automation listener.

## HTTP API

Set `VIDEO_BUILDER_API_KEY` in `.env`, then start the API:

```sh
docker compose --profile api up -d api
```

Both endpoints require an `X-API-Key` header.

| Endpoint | Behavior |
| --- | --- |
| `GET /run` | Starts pending jobs and returns HTTP 202; returns `already_running` with HTTP 200 if a run is active |
| `GET /status` | Returns current status, job counts, stage and progress |

Export the same key in your client shell before running these commands. Compose reading `.env` does not export it for curl.

```sh
curl -H "X-API-Key: $VIDEO_BUILDER_API_KEY" http://127.0.0.1:8000/status
curl -H "X-API-Key: $VIDEO_BUILDER_API_KEY" http://127.0.0.1:8000/run
```

Never put the key in a URL. Docker publishes the API only on the host's loopback interface. Change its host port with `VIDEO_BUILDER_HTTP_PORT`. Remote access requires your own HTTPS reverse proxy forwarding the authentication header; disable caching and prefetching for `/run`.

```sh
docker compose logs -f api
docker compose stop api
```

There is no scheduler or automatic processing at startup.

## Running safely

The dashboard, API and `python -m app.buffer run` share a lock keyed by the resolved
voice buffer path. Compose services share the `processing-locks` volume. Use a
single mount layout and lock directory for all processes touching the same queue;
do not mix native and container workers against it. This is not a distributed lock
across computers. Do not remove the lock volume while processing.

`python -m app.buffer run --watch` checks for new local voices every five seconds.
The automation listener is a separate optional process and never starts rendering.
Its own lock prevents two polling listeners from publishing the same event together.

Stop the process that owns a run to cancel it. Failed or cancelled renders keep
the voice. The dashboard/API can be started without any provider credentials. A
nonempty render requires the configured transcription API key and usable local images.
A complete render is recorded and consumed even if Stop arrives at finalization.

## Recovery

Each successful local render saves a completion event under
`buffer/voiceovers/.queue/completed/voice-NNNNNN.json`, then removes its input and
marks the event `consumed`. An interruption at the removal boundary is recovered
from the `rendered` event on the next queue read, without rendering again. A
manual input deletion alone never signals successful completion. Preserve .queue
and completed MP4s; never reuse consumed IDs.

Rendering failures leave the voice and temporary diagnostics. Fix the cause and
start a new run; watch mode retries automatically on subsequent polls. If output
was finalized but the completion event could not be saved, the source is retained
and the next attempt may rerender. Atomic replacement avoids partial JSON reads;
these checkpoints are not a power-loss or distributed transaction.

Optional provider delivery has separate receipts under `.queue/delivery/`:

| State | Meaning and recovery |
| --- | --- |
| `uploading` | Intended public ID saved before upload; outcome may be ambiguous. Reconcile Cloudinary before retrying. |
| `uploaded` | Confirmed upload saved; retry the listener to update the sheet without reuploading. |
| `done` | Original Baserow row acknowledged the URL and DONE; skip on later polls. |

For an `uploading` receipt, check its public ID and actual local/remote video. If
the complete upload exists, verify its HTTPS URL, public ID and byte count before
recording `uploaded` with those fields in an `uploaded` object. If it is confirmed
absent, remove only that delivery receipt to permit a fresh upload. Never delete
the completion event or restore/requeue a consumed source to retry publication.
Do reconciliation while the automation listener is stopped.

The listener uses the imported internal row ID, not its editable `ID` label or
local sequence number. It writes `Output URL` and `DONE` together only after
upload confirmation. Provider failure does not reverse local completion. A lost
sheet acknowledgement can repeat the same update safely. Unmapped local files
have no provider side effects. A changed Baserow server/table is rejected for
older mappings; restore the original configuration before delivering them.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Baserow reports an error | Numeric table IDs, exact field names, token permissions and server URL |
| No jobs run | Check numbered local voices; optional refill imports rows with `NOT_PROCESSED` |
| No usable images | Put still image files directly in buffer/images; table settings apply only to optional refill |
| Audio or image download fails | Link expiry, login/preview pages, file size and supported media format |
| Transcription fails | Exact endpoint and model, API key, account access and timestamp support |
| Cloudinary upload fails | Cloud name, API key/secret, account limits and saved upload checkpoints |
| API returns HTTP 401 | `X-API-Key` must match the API container's `VIDEO_BUILDER_API_KEY` |
| Files cannot be written on Linux | Export `LOCAL_UID` and `LOCAL_GID` to match your account before starting containers |
| Requests fail on a restricted network | Allow the Baserow, transcription, Cloudinary and source-file hosts, including redirect destinations |

### Build certificate errors

If your corporate or cloud network intercepts HTTPS, use your administrator-approved CA bundle while retaining TLS verification:

```sh
docker build --secret id=build_ca_bundle,src=/path/to/approved-ca-bundle.pem -t poetry-shorts-maker:local .
```

The bundle is used only during dependency installation and is not copied into the image. Runtime HTTP clients also need a trusted CA configuration if their traffic is intercepted.

## Native development

Docker is the preferred runtime and uses Python 3.13. Native development supports Python 3.13 or 3.14 with FFmpeg/FFprobe, libass/H.264 support and a virtual environment.

Install `requirements-dev.txt` for the pinned runtime and development tools, or `requirements.lock` for runtime dependencies alone. Export the required environment variables, then run `python main.py` or `python api.py` from the environment. See [development checks](ARCHITECTURE.md#development-checks) for lint, formatting, type checks and tests. The application does not load `.env` automatically. Keep native and Docker workers separate when they resolve the same queue through different mount paths.

## Validation scope

Tests use real local HTTP downloads and FFmpeg renders. They cover caption frames, image selection, Baserow pagination and updates, API authentication, cancellation, upload signatures and chunk retries, byte preservation and recovery.

External service responses are mocked. Live Baserow, paid transcription and Cloudinary integration require configured credentials and network access. The current validation does not establish that a real paid request or upload has succeeded.
