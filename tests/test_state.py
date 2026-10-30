import json
import uuid

import pytest

from app.artifacts import Workspace
from app.models import Job
from app.state import RunLock


def test_second_acquire_does_not_lose_owned_handle():
    identity = str(uuid.uuid4())
    owner, contender = RunLock(identity), RunLock(identity)
    assert owner.acquire()
    try:
        assert not owner.acquire()
        assert not contender.acquire()
    finally:
        owner.release()
    assert contender.acquire()
    contender.release()


@pytest.mark.parametrize("content", ["null", "[]", "false", '"text"', "{}", "{broken"])
def test_bad_external_state_has_safe_fallback(content):
    lock = RunLock(str(uuid.uuid4()))
    lock.state_path.write_text(content)
    try:
        assert lock.external_state()["status"] == "processing"
    finally:
        lock.state_path.unlink()


def test_checkpoint_replace_failure_keeps_previous_valid_json(monkeypatch, tmp_path):
    from pathlib import Path

    workspace = Workspace.for_job(
        {key: str(tmp_path / key) for key in ("temp", "output", "buffer")},
        Job(1, "test", "unused", "PROCESSING"),
    )
    workspace.prepare()
    workspace.write("rendered", {"output": "original.mp4"})

    def fail(self, target):
        raise OSError("Cannot replace checkpoint")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        workspace.write("rendered", {"output": "new.mp4"})
    assert json.loads((workspace.work / "rendered.json").read_text()) == {"output": "original.mp4"}
    assert not list(workspace.work.glob("*.tmp"))
