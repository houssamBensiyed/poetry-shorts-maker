import logging

from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, ProgressBar, RichLog, Static

log = logging.getLogger("video_builder")


class ConfirmExit(ModalScreen):
    def compose(self):
        yield Static("Processing is active. Stop safely and exit?")
        with Horizontal():
            yield Button("Stop and exit", id="confirm", variant="error")
            yield Button("Keep processing", id="keep")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "confirm")


class Dashboard(App):
    TITLE = "AI VIDEO BUILDER"
    CSS = """
    Screen { layout: vertical; }
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
        super().__init__()
        self.processor = processor
        self.live_logs = live_logs
        self.log_sequence = 0
        self.exiting = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("IDLE", id="status")
        yield Static("", id="stats")
        yield Static("", id="job", markup=False)
        yield ProgressBar(total=100, show_eta=False, id="progress")
        yield RichLog(id="logs", wrap=True, markup=False, max_lines=500)
        with Grid(id="actions"):
            yield Button("Start Processing", id="start", variant="success")
            yield Button("Stop Processing", id="stop", variant="warning")
            yield Button("Refresh Jobs", id="refresh")
            yield Button("View Logs", id="view")
            yield Button("Exit", id="exit")
        yield Footer()

    def on_mount(self):
        self.set_interval(0.2, self.refresh_display)
        log.info(
            "Dashboard ready. Configure Baserow settings and add footage, then Start Processing."
        )
        self.refresh_display()

    def refresh_display(self):
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
        action = event.button.id
        if action == "start":
            if not self.processor.start():
                log.warning("Processing is already active in this or another process")
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

    def refresh_jobs(self):
        try:
            self.processor.refresh()
            log.info("Job counts refreshed")
        except Exception:
            log.exception("Cannot refresh Baserow")

    def action_request_exit(self):
        if self.processor.active:
            self.push_screen(ConfirmExit(), self.confirm_exit)
        else:
            self.exit()

    def action_quit(self):
        self.action_request_exit()

    def confirm_exit(self, confirmed):
        if confirmed:
            self.exiting = True
            self.processor.stop()
