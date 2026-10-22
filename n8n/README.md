# Visual n8n workflows

[Project README](../README.md) · [Architecture](../docs/ARCHITECTURE.md) · [Operations](../docs/operations.md)

Import **[poetry-shorts-visual-demo.json](poetry-shorts-visual-demo.json)** first. It shows the whole project as connected, executable n8n nodes with sample data. It runs without service credentials or a running Python API.

| Workflow | What executes | What you can inspect |
| --- | --- | --- |
| `poetry-shorts-visual-demo.json` | Offline simulation of every stage | Job selection, sequential processing, timestamped words, footage cuts, progressive captions, checkpoint data, DONE/FAILED and continuation |
| `poetry-shorts-api-runner.json` | Real authenticated calls to the project's `/run` and `/status` endpoints | Accepted/already-running response, current stage, render progress, counts, completion, failure and monitoring timeout |

The demo does not download media, write files, render an MP4, call paid services or update Baserow. Its paths and URLs are sample values; every result is marked `simulated`. The live workflow delegates real media work to the Python/FFmpeg engine. The current API exposes no individual stage endpoints, so the live workflow cannot execute each internal stage as a separate HTTP node.

## 1. Open n8n locally

If you already have n8n, skip to importing. Otherwise, install Docker with the Compose v2 plugin, then run this from the **project root**:

```sh
docker compose -f compose.yaml -f n8n/compose.yaml --profile n8n up -d n8n
```

Open **http://localhost:5678** and complete the initial n8n account setup. This starts only n8n; it does not require provider credentials or start Python processing. The overlay pins n8n to `2.44.5`, stores its workflows/credentials in the `n8n-data` volume, and publishes the editor on localhost. The JSON folder is also available read-only inside the container at `/workflows`.

## 2. Import and run the visual demo

1. Create a workflow in the editor.
2. Open the workflow menu and choose **Import from File**.
3. Select `n8n/poetry-shorts-visual-demo.json` from your checkout.
4. Click **Execute workflow**. Both supplied workflows use manual triggers and import inactive.
5. Open individual nodes to inspect their **Output → JSON** data. Open **Demo Results - Inspect Here** for the complete result.

The default fixture contains four jobs:

| Row | Initial status | Expected outcome |
| --- | --- | --- |
| 101 | `NOT_PROCESSED` | Completes with a simulated output URL |
| 102 | `NOT_PROCESSED` | Fails during the simulated upload and retains its complete-output metadata |
| 103 | `DONE` | Skipped |
| 104 | `NOT_PROCESSED` | Completes after row 102's failure, demonstrating queue continuation |

The summary reports **2 completed, 1 failed, 1 skipped**. The third row was already DONE and is not included in `completed_this_run`.

In **Demo Settings**, edit the JavaScript values:

```js
injectFailure: false, // All three pending jobs will complete.
failureRow: 102,
failureStage: 'upload',
```

Supported failure stages are `download`, `transcribe`, `plan`, `images`, `captions`, `render` and `upload`. Execute the whole workflow again after changing the settings. Expected simulated errors are carried to the FAILED branch as data, so they do not abort the n8n execution. Subsequent media nodes pass an errored item through unchanged until that branch; inspect `error` and `history` to find the failing stage even when the handled Code node is green. The loop's `done` output leads to the summary; its `loop` output processes one job at a time.

### Useful visual checks

- **Plan Sentences and Image Cuts:** the fixture creates cuts at 0, 0.4 and 5 seconds. The first qualifying sentence begins at 0.4, so it selects an image after the initial background at zero. The one-word sentence at 3 seconds keeps the current image.
- **Buffer Selected Images:** three selected links out of the four-link pool are represented; no unselected image is buffered.
- **Build Progressive Caption Events:** words reveal cumulatively. The first complete sentence stays until 3 seconds, the short sentence until 5 seconds, and the final caption until the audio ends at 10 seconds.
- **Save Failure and Mark FAILED:** an upload failure includes the rendered output reference and intended public ID. A failure before rendering has no complete output.
- **Mark DONE and Clean Temp:** a successful job records its URL and models removing temporary work while retaining source buffers and output. Checkpoint objects remain in n8n execution data so you can inspect them.

The image choices rotate deterministically for repeatable inspection; Python uses random choices with an optional seed. The demo produces caption-event JSON rather than ASS or MP4 bytes. Actual decoding, multipart requests, upload signatures/chunks, filesystem failures, frame verification, locking and cancellation remain the Python engine's responsibility.

## 3. Run real processing from n8n

Configure `.env`, the Baserow tables and `config.yaml` using the [project setup](../README.md#quick-start), including `VIDEO_BUILDER_API_KEY`. Then start both services from the project root:

```sh
docker compose -f compose.yaml -f n8n/compose.yaml --profile api --profile n8n up -d --build api n8n
```

Import `n8n/poetry-shorts-api-runner.json` as a separate workflow. In n8n:

1. Create a **Header Auth** credential with header name **`X-API-Key`** and value equal to the Python API's **`VIDEO_BUILDER_API_KEY`**.
2. Select that credential on **both** HTTP Request nodes: **Start Processing - GET run** and **Read Processing Status**. Use Generic Credential Type → Header Auth. No credentials are embedded in the exported JSON.
3. Open **Connection Settings**. Keep `http://api:8000` when using the supplied Compose overlay. If n8n runs directly on the host, use `http://127.0.0.1:8000`. Another container or n8n Cloud needs its own reachable API address; its `localhost` is not your Python worker.
4. Click **Execute workflow**. This starts the actual pending jobs and may incur transcription/provider usage.

The start request is sent once, with no automatic retry. An `already_running` response attaches monitoring to the current run. The workflow waits 5 seconds between status reads and allows 120 polls by default, plus HTTP time/retries. Edit those settings for longer videos; the workflow also has a 30-minute execution timeout.

**Live Run Summary** reports an idle worker with no reported error, no pending jobs and a `Run finished` stage. **Needs Attention** ends the n8n execution with an error for a run failure, reported job errors, a stopped/incomplete run, an unconfirmed idle state, or the monitoring limit. HTTP/authentication failures appear directly on the HTTP node. Inspect the previous **Evaluate Processing Status** output for details.

Counts are the worker's current snapshot and include existing DONE/FAILED rows. The API has no run ID or per-job results endpoint; this monitor cannot reliably attribute a snapshot across worker restarts or another operator starting a run. Look up real video links in Baserow's `Output URL` and local files under `output/`.

Stopping the n8n execution or reaching a monitoring timeout does **not** cancel the Python worker. Stop its owning dashboard/process as described in [running safely](../docs/operations.md#running-safely). Do not remove its lock volume during a run.

## Stop the optional services

```sh
docker compose -f compose.yaml -f n8n/compose.yaml --profile api --profile n8n stop api n8n
```

The n8n data volume remains available for the next start.

## Validation

From the project root, run either:

```sh
node n8n/check-workflows.js
# Or, if the SpiderMonkey shell is installed:
js140 n8n/check-workflows.js
```

The harness validates the graphs, executes the exported Code nodes, tests all seven injected failure stages and continuation, checks timeline/caption behavior, and checks bounded live-monitor decisions with fake responses. It makes no network calls. This is not an n8n engine or browser test; importing and executing in your instance is the final compatibility check.

Check the Docker configuration without starting containers:

```sh
docker compose -f compose.yaml -f n8n/compose.yaml --profile api --profile n8n config --quiet
```

The workflow formats use built-in Manual Trigger, Code v2, IF v2.2, Loop Over Items v3, HTTP Request v4.2, Wait v1.1, Stop And Error and Sticky Note nodes. No community nodes or external JavaScript modules are required.

References: [n8n JSON import](https://docs.n8n.io/workflows/export-import/), [Code node](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.code/), [HTTP Request](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/), [Wait](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/), [Docker installation](https://docs.n8n.io/hosting/installation/docker/).
