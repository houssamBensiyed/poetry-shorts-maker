"""Expose manual processing controls and live status through Textual.

The UI polls snapshots rather than touching worker state directly. Queue refresh
runs in a background worker so network reads do not freeze the terminal, while
start/stop delegate lifecycle ownership to Processor. Opening the dashboard does
not automatically contact providers or start jobs.
"""

import logging
from threading import Event

from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, ProgressBar, RichLog, Select, Static

log = logging.getLogger("video_builder")
EMPTY_BUFFER_MESSAGE = (
    "The local voice buffer is empty. Add voice files to the buffer or choose "
    "Baserow to import them."
)


class ConfirmExit(ModalScreen):
    """Ask whether active local processing should stop before the UI exits."""

    def compose(self):
        """Yield the confirmation message and explicit stop/keep-processing buttons."""
        yield Static("Processing is active. Stop safely and exit?")
        with Horizontal():
            yield Button("Stop and exit", id="confirm", variant="error")
            yield Button("Keep processing", id="keep")

    def on_button_pressed(self, event):
        """Dismiss the modal with True only for the stop-and-exit button."""
        self.dismiss(event.button.id == "confirm")


class Dashboard(App):
    """Display processing state, incremental logs and explicit manual actions.

    The refresh timer keeps the screen responsive during worker activity. Remote
    lock holders are shown through Processor.status, while stop applies only to
    this instance's worker. Provider logic remains outside the interface.
    """

    TITLE = "AI VIDEO BUILDER"
    CSS = """
    Screen { layout: vertical; }
    #source, #mode, #channel-name { height: 3; }
    #status { height: 3; padding: 1; }
    #stats { height: 2; padding: 0 1; }
    #job { height: 5; padding: 0 1; }
    #progress { height: 2; padding: 0 1; }
    #logs { height: 1fr; border: solid $primary; }
    #actions { height: 6; grid-size: 3; grid-columns: 1fr 1fr 1fr; }
    Button { margin: 0 1; min-width: 12; }
    ConfirmExit { align: center middle; background: $background 85%; }
    ConfirmExit Static { width: 60; height: 3; background: $panel; padding: 1; }
    ConfirmExit Horizontal { width: 60; height: 4; background: $panel; }
    """
    BINDINGS = [("q", "request_exit", "Exit"), ("ctrl+c", "request_exit", "Exit")]

    def __init__(self, processor, live_logs):
        """Attach the shared processor and live-log handler without starting work.

        Track the last displayed log sequence and whether a confirmed graceful exit
        is pending; the periodic refresh completes exit after the worker stops.
        """
        super().__init__()
        self.processor = processor
        self.live_logs = live_logs
        self.log_sequence = 0
        self.exiting = False

    def compose(self) -> ComposeResult:
        """Yield status, counters, job details, progress, logs and action buttons.

        Disable markup for job text and logs because source/provider strings should
        appear literally instead of being interpreted as terminal formatting.
        """
        yield Header()
        yield Select(
            [
                ("Local buffer — process buffered voices", "local"),
                ("Baserow — import voices, then process", "baserow"),
            ],
            value="local",
            allow_blank=False,
            id="source",
        )
        yield Select(
            [("Images", "image"), ("Videos", "video"), ("Hybrid — video first", "hybrid")],
            value=self.processor.config["footage"]["mode"],
            allow_blank=False,
            id="mode",
        )
        yield Input(
            value=self.processor.config["captions"]["channel_name"],
            placeholder="Channel name (optional, 8 pt below captions)",
            max_length=80,
            id="channel-name",
        )
        yield Static("IDLE", id="status")
        yield Static("", id="stats")
        yield Static("", id="job", markup=False)
        yield ProgressBar(total=100, show_eta=False, id="progress")
        yield RichLog(id="logs", wrap=True, markup=False, max_lines=500)
        with Grid(id="actions"):
            yield Button("Start Processing", id="start", variant="success")
            yield Button("Stop Processing", id="stop", variant="warning")
            yield Button("Refresh Buffer", id="refresh")
            yield Button("View Logs", id="view")
            yield Button("Exit", id="exit")
        yield Footer()

    def on_mount(self):
        """Start 200 ms display polling and show the initial idle snapshot."""
        self.set_interval(0.2, self.refresh_display)
        log.info(
            "Choose Local buffer to process existing voices, or Baserow to import "
            "voices into an empty buffer before processing."
        )
        self.refresh_display()

    def refresh_display(self):
        """Refresh widgets and append unseen log messages from detached snapshots.

        Use indeterminate progress when an active stage has no render percentage.
        Disable actions inappropriate for active/exiting state, including stopping an
        external process. Finish a confirmed exit once the local worker has ended.
        """
        state = self.processor.status()
        active = state["status"] == "processing"
        color = {"idle": "green", "processing": "yellow", "error": "red"}.get(
            state["status"], "white"
        )
        self.query_one("#status", Static).update(f"[{color}]{state['status'].upper()}[/{color}]")
        self.query_one("#stats", Static).update(
            f"Pending: {state.get('pending', 0)}   Completed: {state.get('completed', 0)}   Failed: {state.get('failed', 0)}"
        )
        self.query_one("#job", Static).update(
            f"Job: {state.get('current_job') or '—'}\nVoiceover: {state.get('voiceover', '')}\nStage: {state.get('stage', '')}\n{state.get('error', '')}"
        )
        bar = self.query_one("#progress", ProgressBar)
        value = state.get("render_progress")
        bar.update(total=None if active and value is None else 100, progress=value or 0)
        self.query_one("#start", Button).disabled = active or self.exiting
        self.query_one("#source", Select).disabled = active or self.exiting
        self.query_one("#mode", Select).disabled = active or self.exiting
        self.query_one("#channel-name", Input).disabled = active or self.exiting
        self.query_one("#stop", Button).disabled = not active or state.get(
            "external_process", False
        )
        self.query_one("#refresh", Button).disabled = active or self.exiting
        for sequence, line in self.live_logs.since(self.log_sequence):
            self.query_one("#logs", RichLog).write(line)
            self.log_sequence = sequence
        if self.exiting and not self.processor.active:
            self.exit()

    def on_button_pressed(self, event):
        """Dispatch user controls to the processor or UI log/exit actions.

        Repository refresh uses a Textual thread worker to keep network waits away
        from the UI event loop. Start delegates contention checks to Processor.
        """
        action = event.button.id
        if action == "start":
            source = self.query_one("#source", Select).value
            try:
                started = self.processor.start(
                    prepare=self.prepare_baserow if source == "baserow" else None,
                    empty_message=EMPTY_BUFFER_MESSAGE,
                    mode=self.query_one("#mode", Select).value,
                    channel_name=self.query_one("#channel-name", Input).value,
                )
                if not started:
                    log.warning("Processing is already active in this or another process")
            except Exception:
                log.exception("Could not start processing")
            self.refresh_display()
        elif action == "stop":
            self.processor.stop()
        elif action == "refresh":
            self.run_worker(self.refresh_jobs, thread=True, exclusive=True)
        elif action == "view":
            log.info(
                "Full rotating log file: %s/video_builder.log",
                self.processor.config["paths"]["logs"],
            )
            self.query_one("#logs", RichLog).focus()
        elif action == "exit":
            self.action_request_exit()

    def prepare_baserow(self, stop: Event) -> None:
        """Load the optional importer only after the user selects Baserow."""
        from app.automations import BufferAutomations

        self.processor.update(stage="Importing voices from Baserow")
        count = BufferAutomations(self.processor.config, stop).import_for_processing()
        log.info("Imported %d voices from Baserow; processing the local buffer", count)

    def refresh_jobs(self):
        """Read job counts in the background and log any repository failure.

        Processor.refresh records visible error state; catching here prevents an
        ordinary configuration/network problem from becoming an unhandled UI error.
        """
        try:
            self.processor.refresh()
            log.info("Job counts refreshed")
        except Exception:
            log.exception("Cannot refresh the voice buffer")

    def action_request_exit(self):
        """Open confirmation for active local work, or exit immediately when idle."""
        if self.processor.active:
            self.push_screen(ConfirmExit(), self.confirm_exit)
        else:
            self.exit()

    def action_quit(self):
        """Route Textual's quit action through the same active-work exit policy."""
        self.action_request_exit()

    def confirm_exit(self, confirmed):
        """Request cooperative stop after confirmation and let polling finish exit.

        Returning from the modal without confirmation leaves processing active.
        Waiting through the timer keeps the UI alive while worker cleanup completes.
        """
        if confirmed:
            self.exiting = True
            self.processor.stop()
