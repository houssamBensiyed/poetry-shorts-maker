"""One workflow router for application clients; dependencies belong to each style."""

import uuid
from pathlib import Path
from types import SimpleNamespace

from app.contracts import ProcessingServices
from app.models import Job
from app.state import RunLock


def poetry_services(config, section=None, request=None, transcriber=None):
    from app.bootstrap import build_services

    return build_services(config)


def story_services(config, section=None, request=None, transcriber=None):
    if not section and request is None:
        raise ValueError("Story style requires a section directory")
    from app.story import run_story, section_request, validate_inputs

    directory = Path(section).resolve() if section else request.work.resolve()

    class Executor:
        def __init__(self, stop, report):
            self.stop, self.report = stop, report

        def run(self, job, repository, sources):
            token = "story-" + uuid.uuid4().hex
            selected = request or section_request(
                directory,
                Path(config["paths"]["output"]) / (token + ".mp4"),
                Path(config["paths"]["temp"]) / token,
            )
            lock = RunLock(f"story:{directory}")
            if not lock.acquire():
                raise RuntimeError("This story section is already being rendered")
            try:
                self.report(output=str(selected.output), work=str(selected.work))
                run_story(selected, config, self.stop, self.report, transcriber=transcriber)
                return True
            finally:
                lock.release()

    def executor(event, reporter):
        return Executor(event, reporter)

    def validate():
        validate_inputs(request or section_request(directory, Path("unused"), Path("unused")))

    return ProcessingServices(
        identity=f"story:{directory}",
        repository=lambda event: SimpleNamespace(
            jobs=lambda: [Job(1, directory.name, "", "NOT_PROCESSED")]
        ),
        catalog=lambda event: SimpleNamespace(sources=lambda: []),
        executor=executor,
        validate=validate,
    )


WORKFLOWS = {"poetry": poetry_services, "story": story_services}


def build_workflow(config, style="poetry", section=None, *, request=None, transcriber=None):
    if style not in WORKFLOWS:
        raise ValueError("Style must be poetry or story")
    return WORKFLOWS[style](config, section, request, transcriber)
