"""Load selected application settings from a local .env without executing shell code."""

import os
import shlex
from pathlib import Path


def load_environment(names, path):
    """Fill missing environment variables, preserving explicitly exported values.

    Accept plain or quoted KEY=value assignments, optional export prefixes and
    comments. Ignore unrelated variables; never expand variables or execute values.
    Parse before applying changes so malformed credentials do not leave a partial
    environment. Error messages identify the variable without exposing its value.
    """
    path = Path(path)
    pending = set(names) - os.environ.keys()
    if not pending or not path.is_file():
        return
    loaded = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or name not in pending:
            continue
        try:
            values = shlex.split(value, comments=True)
        except ValueError:
            raise ValueError(f"Check the quotes around {name} in .env.") from None
        if len(values) > 1:
            raise ValueError(f"Put quotes around the {name} value in .env if it contains spaces.")
        loaded[name] = values[0] if values else ""
        pending.remove(name)
    os.environ.update(loaded)
