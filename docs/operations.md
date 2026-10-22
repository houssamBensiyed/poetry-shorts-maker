# Operations guide

[README](../README.md) · [Architecture and extensions](ARCHITECTURE.md) · [Critical code review](CODE_REVIEW.md)

[File links](#file-links-and-buffers) · [Transcription](#transcription-provider) · [Configuration](#configuration) · [HTTP API](#http-api) · [Recovery](#recovery) · [Troubleshooting](#troubleshooting)

## File links and buffers

Put a direct HTTP(S) audio URL in each Baserow audio row and set `Status` to `NOT_PROCESSED`. The URL may come from an object store, your own server or any host that serves the actual file. Normal redirects are followed. A file extension is not required; FFprobe verifies that the download contains usable audio.

Browser preview pages, HTML login pages and links that require browser cookies or a private provider SDK are unsupported. Use a direct-download or signed URL with enough time before expiry for the job to finish.

The image table must contain at least one usable link. Supported images are still JPEG, PNG, WebP and BMP files. Animated images are rejected.

At the start of a run, the app reads the image table and logs its total row count and number of distinct usable links. Blank or invalid HTTP(S) links are skipped. Duplicate image URLs count once. After transcription, the planner selects images for the timeline and downloads only those selections. An image reused within one video downloads once; each new job downloads fresh copies. A selected image that cannot download or decode fails the job visibly.

| Location | Retention |
| --- | --- |
| `buffer/voiceovers/` | Original downloaded audio remains after processing |
| `buffer/images/row-*/` | Selected images remain after processing |
| `output/` | Complete MP4 files remain, including after an upload failure |
| `temp/row-*/` | Successful jobs remove their work directory; failed jobs keep diagnostics |
| `logs/` | Rotating `video_builder.log`, approximately 5 MB per file with 3 backups |

Downloads are finalized only after completion. Incomplete downloads and partial MP4 files are removed. You can clear old buffers when no run needs them. Only the finished MP4 is uploaded to Cloudinary; source audio goes to the transcription API, and source images stay local.

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

Edit existing values in [`config.yaml`](../config.yaml). Restart the dashboard or API to reload YAML changes. Rebuild with `docker compose build dashboard` after code or dependency changes. Baserow image links are read afresh at the start of each run.

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

The dashboard and API use the same processing engine and a lock keyed by Baserow server and audio table ID. Run them from the same Compose project so they share the `processing-locks` volume. Avoid simultaneous processing from another Compose project, native process or computer against the same table. The lock is local, not distributed.

Stop the instance that owns the run to cancel it. A dashboard observing another process cannot stop that process. Do not remove the lock volume while processing.

**Stop Processing** prevents new jobs and cancels the current job at a safe checkpoint. FFmpeg stops promptly; HTTP operations stop between requests or chunks, or after their timeout. A final confirmed Cloudinary upload is still recorded in Baserow if Stop arrives with the response. Compose allows 180 seconds for shutdown; increase this if you increase request timeouts.

Baserow updates use the stable internal row ID, not the editable `ID` label. Avoid changing or deleting pending rows during a run because the app works from a snapshot.

| Status | Meaning |
| --- | --- |
| `NOT_PROCESSED` | Eligible for the next run |
| `PROCESSING` | Job has started; a stale row needs inspection |
| `DONE` | Cloudinary upload was confirmed and the row was updated |
| `FAILED` | Processing failed or was cancelled; review `Error` before retrying |

Legacy `COMPLETED` rows are skipped as already finished. Blank or unrecognized statuses are skipped with a warning. A job failure normally leaves an error on that row and allows the next pending job to run.

## Recovery

Failed jobs are not retried automatically. Fix the cause, inspect any saved upload information, then set `Status` to `NOT_PROCESSED` and start again. Each retry makes a new render and a new upload ID.

A crash may leave a row at `PROCESSING`. The next run warns and skips it. Confirm no process is still working before resetting it.

When rendering succeeds but uploading or updating Baserow fails, the complete local MP4 is retained. Check the job directory under `temp/`:

| Checkpoint | Contents |
| --- | --- |
| `rendered.json` | Completed local output path, audio path and row ID |
| `upload.json` | Intended Cloudinary public ID, written before uploading |
| `uploaded.json` | Confirmed upload URL, public ID, byte count and output path |
| `failure.json` | Error, local paths and any known uploaded URL |

If `uploaded.json` exists, copy the URL into `Output URL`, the output path into `Output File`, and set `Status` to `DONE` manually after checking the file.

If the final upload request timed out, inspect Cloudinary for the public ID in `upload.json` before retrying. The remote video may already exist. Cloudinary uploads and Baserow updates are separate operations, so a lost response does not prove the upload failed.

Checkpoint JSON files are replaced atomically so readers do not observe partially written documents. A disk or permission failure can still prevent a checkpoint from being saved. The runner attempts diagnostics and remote failure updates independently; check the row error and logs if a checkpoint is absent. A complete MP4 is retained after rendering even if a later checkpoint fails. If `uploaded.json` could not be saved, a known uploaded URL may still appear in `failure.json` or the failed row.

After Baserow acknowledges `DONE`, failure to remove the temporary work directory is logged without changing the job back to `FAILED`. Shared-status publication failures are also logged without changing the job outcome.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Baserow reports an error | Numeric table IDs, exact field names, token permissions and server URL |
| No jobs run | Audio rows must have the exact status `NOT_PROCESSED` |
| No usable images | Separate image table ID, `Image URL` field and at least one direct HTTP(S) image link |
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

Install `requirements-dev.txt` for the pinned runtime and development tools, or `requirements.lock` for runtime dependencies alone. Export the required environment variables, then run `python main.py` or `python api.py` from the environment. See [development checks](ARCHITECTURE.md#development-checks) for lint, formatting, type checks and tests. The application does not load `.env` automatically. Keep native processing separate from Docker processing against the same Baserow table.

## Validation scope

Tests use real local HTTP downloads and FFmpeg renders. They cover caption frames, image selection, Baserow pagination and updates, API authentication, cancellation, upload signatures and chunk retries, byte preservation and recovery.

External service responses are mocked. Live Baserow, paid transcription and Cloudinary integration require configured credentials and network access. The current validation does not establish that a real paid request or upload has succeeded.
