"""Read the local Whisper credential for the standalone development launcher."""

import os
import shlex
from pathlib import Path

from app.config import ROOT


def load_whisper_key(config, path=ROOT / ".env"):
    """Load only the configured key, preserving an exported environment value.

    Accept KEY=value and export KEY=value with plain or quoted values and
    comments. Never execute shell code, expand variables, or log the secret.
    Missing files/keys leave the UI available with a configuration message.
    """
    name = config["whisper"]["api_key_env"]
    path = Path(path)
    if name in os.environ or not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != name:
            continue
        try:
            values = shlex.split(value, comments=True)
        except ValueError:
            raise ValueError(f"Check the quotes around {name} in .env.") from None
        if len(values) > 1:
            raise ValueError(f"Put quotes around the {name} value in .env if it contains spaces.")
        os.environ[name] = values[0] if values else ""
        return
