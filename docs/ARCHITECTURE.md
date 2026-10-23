# Architecture and extension guide

The default video worker owns a local numbered voice queue. Remote ingestion and
publication are independent optional adapters.

## Responsibility boundaries

| Component | Responsibility |
| --- | --- |
| `VoiceBuffer` in `app/buffer.py` | Strict numbering, atomic enqueue, source metadata and completion outbox |
| `LocalImages` | Reusable local image candidates |
| `Processor` | Sequential scheduling, cancellation, shared lock and observable counts |
| `BufferRunner` | Render one buffered voice; save event and consume source on success |
| `MediaPipeline.run_local()` | Probe audio, transcribe, plan, resolve local media and render |
| `video_builder.render()` | Move still images, compose film overlay/captions and mix gained voice/music |
| `bootstrap.build_services()` | Compose the local default without Baserow or Cloudinary |
| `BufferAutomations` | Optional empty-queue refill and completion-event delivery |
| `JobTable` / `Uploader` | Provider-specific HTTP requests, authentication and upload confirmation |

The dashboard, HTTP API and buffer CLI all use the same default Processor.
Transcription remains injectable; the default adapter uses the configured hosted
speech API. The queue and renderer do not require a Baserow table or Cloudinary
account. Remote provider modules are not imported by the default composition.

## Local sequence

1. Enqueue copies an audio file to the next `voice-NNNNNN.ext` name. A metadata
   lock serializes allocation; a persistent high-water mark prevents ID reuse.
   Incomplete copies are hidden and never become queued inputs.
2. Processor snapshots numbered voices in numeric order, under its run lock.
3. Local image candidates are snapshotted once. The timeline planner selects
   random images at eligible sentence boundaries; it never queries a sheet.
4. `run_local` operates on the existing voice path and creates a validated MP4
   with the same voice ID. It never downloads the voice or publishes the result.
5. BufferRunner saves a `rendered` receipt, removes that voice, and marks the
   receipt `consumed`. Failed rendering retains the input and emits no event.
6. Successful temporary work is cleaned. Images, output, sequence state, source
   metadata and events remain. An empty queue needs no provider credentials.

A saved rendered receipt can finish interrupted removal without re-rendering.
Receipt replacement is atomic for readers, not a transaction against power loss.
The queue's metadata lock and the scheduling lock serve different purposes;
enqueue can run while an existing snapshot renders.

## Render composition

`video_builder.render()` is shared by the buffer worker and the dev tool. It picks
one music track per reel from `composition.music_directory`, using the footage
seed when supplied. The configured film overlay is a separate local video input.
Asset presence is checked before transcription; FFmpeg validates media content.
The dev tool stores all test uploads, videos and diagnostics in per-session
directories under `paths.temp/ui-test/`. It does not use production voice/image
buffers or the production output directory.

Still-image segments use 3% overscan and shallow sinusoidal offsets with 2.4-second
horizontal and 3.2-second vertical cycles. These shorter cycles increase shake
speed while keeping the existing travel distance and overscan. Processing RGB
at twice the output size allows finer crop steps without chroma-grid jumps.
Motion phase follows the global timeline across cuts;
existing video sources retain their own motion.

Source-image RGB values are multiplied by 0.8 after motion processing. The final
pass loops/scales the overlay, blends RGB channels using difference at 70% opacity,
then draws captions at their original brightness. It mixes the original voice at
+3.4 dB with the chosen track
at −12.0 dB, without amix normalization. Mono voice is duplicated into stereo
without implicit pan attenuation; overlay audio is never mapped. Output duration
is bounded by the voice timeline, including when either asset is shorter.
`composition.json` records asset choices and fixed effects for diagnosis. The dev
tool includes that record in its run report and checks asset readiness before a
run. Rendering remains independent of provider ingestion/publication.

```mermaid
sequenceDiagram
    participant Queue as Voice buffer
    participant Worker as Local worker
    participant Events as Completion outbox
    participant Listener as Optional automation
    participant Cloud as Cloudinary
    participant Sheet as Baserow
    Worker->>Queue: Read next numbered voice
    Worker->>Worker: Transcribe, select local images, render validated MP4
    Worker->>Events: Save rendered receipt with voice ID and output
    Worker->>Queue: Remove successfully rendered voice
    Worker->>Events: Mark receipt consumed
    opt Automation enabled and origin mapping present
        Listener->>Events: Read consumed event and original row mapping
        Listener->>Cloud: Upload completed MP4
        Cloud-->>Listener: Confirm public URL, ID and bytes
        Listener->>Listener: Persist confirmed upload
        Listener->>Sheet: Write URL and DONE to original row
        Sheet-->>Listener: Acknowledge update
        Listener->>Listener: Mark delivery done
    end
```

## Optional automation sequence

`python -m app.automations --watch` polls every five seconds. It does not render.
Refill returns immediately for a nonempty voice queue without constructing a
Baserow adapter. When empty, it imports pending rows, validates downloaded audio,
seeds images if necessary, and enqueues numbered voices with opaque origin
metadata. Origins preserve server, table, internal row ID and display label.
Retained mappings prevent reimport while the sheet awaits its final update.

Voice identity and provider identity remain separate:

| Value | Example | Used for |
| --- | --- | --- |
| Local sequence / stem | `1` / `voice-000001` | Queue ordering, input, output and event names |
| Original server and table | `https://api.baserow.io`, table `51` | Preventing delivery to a different source |
| Original internal row ID | `92` | Addressing the sheet update after upload |
| Editable sheet `ID` label | `poem-001` | Display metadata; never used to address writes |

The completion event copies the origin mapping before removing the voice, so
publication does not need to reconstruct identity from a deleted input filename.
Persisted state lives under `buffer/voiceovers/.queue/`: `sequence.json` reserves
numbers, `voice-*.json` retains ingestion mappings, `completed/` holds render and
consumption receipts, and `delivery/` holds optional upload/acknowledgement receipts.

The completion listener reads the outbox rather than listening to arbitrary
filesystem unlink events. A manual deletion cannot represent a validated video.
For mapped consumed events it checkpoints the intended upload ID, uploads,
checkpoints the confirmed URL, then acknowledges the URL and DONE together on the
original row. Local-only events have no remote effect. Changing the configured
server/table cannot redirect an older completion to another sheet.

A failed sheet write reuses the confirmed upload. An ambiguous upload requires
reconciliation rather than a blind retry. Per-event failures do not stop other
events, and delivery failures do not prevent the independent refill attempt.
See [recovery](operations.md#recovery).

## Extension contracts

`ProcessingServices` still accepts repository, catalog and executor factories for
custom scheduling compositions. `Job` is the existing scheduling DTO: locally,
`row` holds the sequence, `id` the voice stem, and `voice_link` the local path.
The opaque origin metadata carries the separate remote row identity. Local
completion does not call the remote repository update contract.

`MediaPipeline.run()` and `JobRunner` retain the explicit remote-first injected
workflow for existing callers/tests; they are not selected by main.py, api.py or
the buffer CLI. The default path calls `run_local()` and `BufferRunner`.
`VideoPublisher` remains the upload boundary. Add providers in the optional
adapter layer rather than teaching the local worker about remote table schemas.

Cancellation checks run before expensive stages. Once the renderer successfully
finalizes, the local runner attempts to record and consume the voice even if stop
arrives at that boundary. Status reporting failures remain outside media policy.

## Development checks

```sh
.venv/bin/ruff check app tests devtools/video_test api.py main.py
.venv/bin/ruff format --check app tests devtools/video_test api.py main.py
.venv/bin/mypy
node --check devtools/video_test/static/app.js
.venv/bin/python -m pytest -q tests devtools/video_test/tests
```

Core interface type checks cover the modules listed in pyproject.toml. Tests use
real local media and FFmpeg, with external provider responses mocked. Architecture
tests guard the default composition and core against Baserow/Cloudinary imports.
Core and dev-tool suites verify real media composition, queue recovery and provider
isolation. Container startup and live provider processing need separate validation.
