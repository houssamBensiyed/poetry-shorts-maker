"use strict";
const $ = (id) => document.getElementById(id);
let session = null;
let info = null;
let busy = false;
let connected = false;
let imageKey = "";
let audioKey = "";
let eventKey = "";
let videoKey = "";
let pollTimer;
const storage = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* Optional persistence. */
    }
  },
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function showError(error) {
  $("message").textContent = error.message || String(error);
  $("message").hidden = false;
}
function clearError() {
  $("message").hidden = true;
}
function duration(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}
function bytes(size) {
  return size < 1048576
    ? `${Math.round(size / 1024)} KB`
    : `${(size / 1048576).toFixed(1)} MB`;
}
function base() {
  return `/api/sessions/${session.id}`;
}
function active() {
  return session?.run && ["running", "stopping"].includes(session.run.status);
}
function connection(ok) {
  connected = ok;
  $("connection").textContent = ok ? "Server connected" : "Server unavailable";
  $("connection-dot").classList.toggle("offline", !ok);
}
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { "X-Video-Test": "1", ...options.headers },
      cache: "no-store",
    });
  } catch {
    connection(false);
    throw new Error(
      "Cannot reach the test server. Check that it is still running.",
    );
  }
  connection(true);
  const result = await response.json();
  if (!response.ok) {
    const detail =
      typeof result.detail === "string"
        ? result.detail
        : "Check your test settings and try again.";
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return result;
}
function updateChecks() {
  const imagesReady = session.images.length >= 3 && session.images.length <= 10;
  const audioReady = Boolean(session.audio);
  const toolsReady = info?.ffmpeg && info?.font && info?.assets;
  for (const [id, ready] of [
    ["check-images", imagesReady],
    ["step-images", imagesReady],
    ["check-audio", audioReady],
    ["step-audio", audioReady],
    ["check-tools", toolsReady],
    ["check-speech", info?.speech],
    ["step-run", session.run?.status === "passed"],
  ]) {
    $(id).classList.toggle("complete", Boolean(ready));
  }
  const locked = Boolean(busy || active() || session.uploading);
  document.body.classList.toggle("busy", locked);
  for (const id of ["image-drop", "audio-drop", "remove-audio", "size", "seed"])
    $(id).disabled = locked;
  $("image-drop").disabled = locked || session.images.length >= 10;
  document
    .querySelectorAll(".remove-image")
    .forEach((button) => (button.disabled = locked));
  const validSeed = $("seed").validity.valid && $("seed").value !== "";
  $("run").disabled =
    locked ||
    !connected ||
    !imagesReady ||
    !audioReady ||
    !toolsReady ||
    !info?.speech ||
    !validSeed;
  $("run").hidden = Boolean(active());
  $("stop").hidden = !active();
  $("stop").disabled = session.run?.status === "stopping";
  $("stop").textContent =
    session.run?.status === "stopping" ? "Stopping…" : "Stop test";
  $("run").querySelector("span").textContent = session.run
    ? "Run again"
    : "Run test";
  $("run-hint").textContent = active()
    ? "Building from the files in your test session."
    : busy
      ? "Saving your test files to temporary storage…"
      : !connected
        ? "Reconnect to the test server to continue."
        : !toolsReady
          ? info?.assets_error || "FFmpeg and the caption font are required."
          : !imagesReady || !audioReady
            ? "Add 3–10 images and a voice recording."
            : !info?.speech
              ? "Set up the Whisper API before running a test."
              : !validSeed
                ? "Enter a valid image selection seed."
                : "Everything is ready. Start your test.";
}
function renderMedia() {
  $("image-count").textContent = `${session.images.length} / 10`;
  $("image-status").textContent = session.images.length
    ? `${session.images.length} saved to temporary storage`
    : "Waiting for images";
  $("image-status").classList.toggle("saved", session.images.length > 0);
  const nextImageKey = session.images.map((item) => item.id).join(",");
  if (nextImageKey !== imageKey) {
    imageKey = nextImageKey;
    $("image-grid").replaceChildren(
      ...session.images.map((item, index) => {
        const wrapper = node("div", "image-item");
        const thumb = node("div", "image-thumb");
        const img = node("img");
        img.src = `${base()}/media/${item.id}`;
        img.alt = item.name;
        img.loading = "lazy";
        const remove = node("button", "icon-button remove-image");
        remove.type = "button";
        remove.setAttribute("aria-label", `Remove ${item.name}`);
        remove.title = `Remove ${item.name}`;
        remove.append(icon("close"));
        remove.addEventListener("click", () => removeMedia(item.id));
        thumb.append(
          img,
          node("span", "image-index", String(index + 1).padStart(2, "0")),
          remove,
        );
        const caption = node("div", "image-name", item.name);
        caption.title = `${item.name} · ${item.width} × ${item.height}`;
        wrapper.append(thumb, caption);
        return wrapper;
      }),
    );
  }
  const nextAudioKey = session.audio?.id || "";
  if (audioKey !== nextAudioKey) {
    audioKey = nextAudioKey;
    if (session.audio) {
      $("audio-name").textContent = session.audio.name;
      $("audio-details").textContent =
        `${duration(session.audio.duration)} · ${bytes(session.audio.bytes)} · Saved to temporary storage`;
      $("audio-player").src = `${base()}/media/${session.audio.id}`;
    } else {
      $("audio-player").pause();
      $("audio-player").removeAttribute("src");
      $("audio-player").load();
    }
  }
  $("audio-file").hidden = !session.audio;
  $("audio-drop").hidden = Boolean(session.audio);
  $("audio-status").textContent = session.audio
    ? "Saved to temporary storage"
    : "Waiting for voice";
  $("audio-status").classList.toggle("saved", Boolean(session.audio));
}
function renderRun() {
  const run = session.run;
  const labels = {
    running: "Running",
    stopping: "Stopping",
    passed: "Passed",
    failed: "Failed",
    stopped: "Stopped",
  };
  $("run-status").textContent = run ? labels[run.status] : "Not started";
  $("run-status").className = `status-badge ${run?.status || ""}`;
  $("stage").textContent = run?.stage || "Ready when your files are.";
  const percent = Math.floor(run?.progress || 0);
  $("percent").textContent = `${percent}%`;
  $("progress-fill").style.width = `${percent}%`;
  $("progress-bar").setAttribute("aria-valuenow", percent);
  $("elapsed").textContent = run
    ? duration((run.finished || Date.now() / 1000) - run.started)
    : "00:00";
  $("report-link").hidden = !run;
  if (run) $("report-link").href = `${base()}/report`;
  const nextEventKey = `${run?.id}:${run?.events.length}:${run?.error}`;
  if (nextEventKey !== eventKey) {
    eventKey = nextEventKey;
    const events = (run?.events || []).map((event) => {
      const row = node("div", "event");
      row.append(
        node("time", "", duration(event.time - run.started)),
        node("span", "", event.message),
      );
      return row;
    });
    if (run?.error) events.push(node("div", "event error", run.error));
    $("events").replaceChildren(
      ...(events.length
        ? events
        : [
            node(
              "div",
              "empty-log",
              "Test steps and results will appear here.",
            ),
          ]),
    );
    $("events").scrollTop = $("events").scrollHeight;
  }
  const nextVideoKey = run?.video ? run.id : "";
  if (nextVideoKey !== videoKey) {
    videoKey = nextVideoKey;
    if (videoKey) {
      $("video-player").src = `${base()}/video/${run.id}`;
      $("download").href = `${base()}/video/${run.id}?download=true`;
    } else {
      $("video-player").pause();
      $("video-player").removeAttribute("src");
      $("video-player").load();
    }
  }
  $("video-player").hidden = !videoKey;
  $("video-sound").hidden = !videoKey;
  updateSoundStatus();
  $("preview-empty").hidden = Boolean(videoKey);
  $("preview-label").hidden = Boolean(videoKey);
  $("download").hidden = !videoKey;
  $("video-frame").classList.toggle("building", Boolean(active()));
  $("preview-title").textContent = active()
    ? "Building your video"
    : run?.status === "failed"
      ? "The test failed"
      : run?.status === "stopped"
        ? "Test stopped"
        : "Your video goes here";
  $("preview-detail").textContent = active()
    ? `${percent}% complete · See progress below`
    : run?.status === "failed"
      ? "See the error in Test progress."
      : "Run a test to see the result.";
  $("resolution").textContent = run
    ? `${run.width} × ${run.height}`
    : $("size").value === "full"
      ? info?.full_size
      : info?.preview_size || "360 × 640";
  $("fps").textContent = `${run?.fps || info?.fps || 30} fps`;
  $("duration").textContent = duration(
    run?.duration ?? session.audio?.duration,
  );
  if (run) $("video-frame").style.aspectRatio = `${run.width}/${run.height}`;
}
function updateSoundStatus() {
  const player = $("video-player");
  $("sound-status").textContent = player.muted
    ? "Video is muted"
    : player.volume === 0
      ? "Video volume is 0%"
      : `Sound on · ${Math.round(player.volume * 100)}% volume`;
}

$("video-player").addEventListener("volumechange", updateSoundStatus);
$("video-player").addEventListener("error", () => {
  if (videoKey)
    showError(
      new Error(
        "Your browser could not play this video. Download the MP4 and try another player.",
      ),
    );
});
$("play-with-sound").addEventListener("click", async () => {
  const player = $("video-player");
  $("audio-player").pause();
  player.muted = false;
  player.volume = 1;
  updateSoundStatus();
  try {
    await player.play();
  } catch (error) {
    showError(new Error(`Could not start video playback: ${error.message}`));
  }
});

function render() {
  if (!session) return;
  renderMedia();
  renderRun();
  updateChecks();
}
async function removeMedia(id) {
  if (busy || active()) return;
  busy = true;
  clearError();
  render();
  try {
    session = await api(`${base()}/media/${id}`, { method: "DELETE" });
  } catch (error) {
    showError(error);
  } finally {
    busy = false;
    render();
  }
}
async function upload(files, kind) {
  if (!session || busy || active()) return;
  files = [...files];
  if (!files.length) return;
  clearError();
  if (kind === "image" && session.images.length + files.length > 10)
    return showError(
      new Error(
        "Choose no more than 10 images in total. Remove an image to make room.",
      ),
    );
  if (kind === "audio" && files.length !== 1)
    return showError(new Error("Choose one voice recording."));
  const limit = kind === "image" ? 20 : 100;
  if (files.some((file) => file.size > limit * 1024 * 1024))
    return showError(new Error(`Each file must be smaller than ${limit} MB.`));
  busy = true;
  render();
  $("upload-notice").hidden = false;
  const failures = [];
  try {
    for (const [index, file] of files.entries()) {
      $("upload-message").textContent =
        `Saving ${index + 1} of ${files.length}: ${file.name}`;
      try {
        session = await api(
          `${base()}/media/${kind}?name=${encodeURIComponent(file.name)}`,
          {
            method: "POST",
            body: file,
            headers: { "Content-Type": "application/octet-stream" },
          },
        );
        render();
      } catch (error) {
        failures.push(`${file.name}: ${error.message}`);
        if (!connected) break;
      }
    }
    if (failures.length) showError(new Error(failures.join(" ")));
  } finally {
    busy = false;
    $("upload-notice").hidden = true;
    // Fetch after the upload handler has released its write lock.
    try {
      session = await api(base());
    } catch (error) {
      showError(error);
    }
    render();
  }
}
for (const kind of ["image", "audio"]) {
  const input = $(`${kind}-input`);
  const drop = $(`${kind}-drop`);
  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    upload(input.files, kind);
    input.value = "";
  });
  for (const name of ["dragenter", "dragover"])
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      if (!drop.disabled) drop.classList.add("dragover");
    });
  for (const name of ["dragleave", "drop"])
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.remove("dragover");
    });
  drop.addEventListener("drop", (event) => {
    if (!drop.disabled) upload(event.dataTransfer.files, kind);
  });
}
// Avoid navigating away when a file misses the upload target.
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());
$("remove-audio").addEventListener(
  "click",
  () => session.audio && removeMedia(session.audio.id),
);
for (const id of ["size", "seed"]) $(id).addEventListener("input", render);
$("run").addEventListener("click", async () => {
  if ($("run").disabled) return;
  busy = true;
  clearError();
  render();
  try {
    session = await api(`${base()}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        size: $("size").value,
        seed: Number($("seed").value),
      }),
    });
  } catch (error) {
    showError(error);
  } finally {
    busy = false;
    render();
  }
});
$("stop").addEventListener("click", async () => {
  $("stop").disabled = true;
  try {
    session = await api(`${base()}/stop`, { method: "POST" });
  } catch (error) {
    showError(error);
  } finally {
    render();
  }
});
async function connect() {
  info = await api("/api/info");
  $("full-size").textContent = `${info.full_size} · Full size`;
  $("size").options[0].textContent = `${info.preview_size} · Fast preview`;
  $("speech-status").value = info.speech_model
    ? `Whisper API · ${info.speech_model}`
    : "Whisper API";
  $("speech-setup").hidden = info.speech;
  $("speech-setup").textContent = info.speech_error
    ? `${info.speech_error}. You can put the key in the project's .env file, then restart the test server.`
    : "";
  const saved = storage.get("video-test-session");
  if (saved && /^[a-f0-9]{32}$/.test(saved)) {
    try {
      session = await api(`/api/sessions/${saved}`);
    } catch (error) {
      if (error.status !== 404) throw error;
    }
  }
  if (!session) {
    session = await api("/api/sessions", { method: "POST" });
    storage.set("video-test-session", session.id);
  }
}
async function poll() {
  try {
    if (!session) await connect();
    else if (!busy) session = await api(base());
  } catch (error) {
    if (error.status === 404) {
      session = null;
      imageKey = audioKey = eventKey = videoKey = "";
      $("image-grid").replaceChildren();
      for (const id of ["audio-player", "video-player"]) {
        $(id).pause();
        $(id).removeAttribute("src");
        $(id).load();
      }
      showError(
        new Error(
          "The server restarted. A new test session will open. Previous files remain on disk.",
        ),
      );
    } else if (!session) showError(error);
  } finally {
    render();
    pollTimer = window.setTimeout(poll, connected ? 900 : 2500);
  }
}
window.addEventListener("pagehide", () => clearTimeout(pollTimer));
window.addEventListener("pageshow", (event) => {
  if (event.persisted) poll();
});
poll();
