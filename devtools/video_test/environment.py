"""Read the local Whisper credential for the standalone development launcher."""

from app.config import ROOT
from app.environment import load_environment


def load_whisper_key(config, path=ROOT / ".env"):
    """Load only the configured key, preserving an exported environment value.

    Accept KEY=value and export KEY=value with plain or quoted values and
    comments. Never execute shell code, expand variables, or log the secret.
    Missing files/keys leave the UI available with a configuration message.
    """
    load_environment({config["whisper"]["api_key_env"]}, path)
