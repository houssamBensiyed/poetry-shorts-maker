"""Persist explicitly applied project settings with atomic replacement."""

import os
import tempfile
from pathlib import Path

import yaml

from app.config import load_config, validate_shake
from app.state import RunLock


def save_shake_settings(path, shake):
    """Update only the shake section of a server-selected YAML configuration.

    Keep unrelated settings, relative paths and surrounding text intact. Validate
    before writing and use an adjacent temporary file for atomic replacement.
    """
    validate_shake(shake)
    path = Path(path).resolve()
    lock = RunLock(f"config:{path}")
    if not lock.acquire():
        raise ValueError("Project settings are being updated; try again")
    temporary = None
    try:
        load_config(path)
        original = path.read_text(encoding="utf-8")
        document = yaml.compose(original)
        replacement = yaml.safe_dump({"shake": shake}, sort_keys=False)
        span = (
            next(
                (
                    (key.start_mark.index, value.end_mark.index)
                    for key, value in document.value
                    if key.value == "shake"
                ),
                None,
            )
            if document
            else None
        )
        if span:
            # YAML's end mark includes comments preceding the next section.
            # Those comments belong to the surrounding project text.
            lines = original[span[0] : span[1]].splitlines(keepends=True)
            while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")):
                lines.pop()
            span = (span[0], span[0] + sum(map(len, lines)))
        updated = (
            original[: span[0]] + replacement + original[span[1] :]
            if span
            else original.rstrip() + "\n" + replacement
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".shake-",
            suffix=".yaml",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        load_config(temporary)
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.release()
