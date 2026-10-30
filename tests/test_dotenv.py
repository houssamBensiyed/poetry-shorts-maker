"""Verify shared .env loading and native console/API startup with no exported keys."""

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import create_app
from app.config import check_transcription_config, load_config
from app.environment import load_environment


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.ROOT", tmp_path)
    for name in (
        "WHISPER_API_KEY",
        "BASEROW_API_TOKEN",
        "CLOUDINARY_API_KEY",
        "CLOUDINARY_API_SECRET",
        "VIDEO_BUILDER_API_KEY",
        "VIDEO_BUILDER_HOST",
        "VIDEO_BUILDER_PORT",
        "CUSTOM_WHISPER_KEY",
        "UNRELATED_ENV_SETTING",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(
        "whisper:\n  api_url: https://transcription.example/transcriptions\n  model: hosted\n"
    )
    return tmp_path


def test_configuration_loads_credentials_from_project_even_outside_working_directory(
    project, tmp_path, monkeypatch
):
    (project / ".env").write_text(
        'export WHISPER_API_KEY="test whisper" # comment\n'
        "BASEROW_API_TOKEN=test-baserow\n"
        "CLOUDINARY_API_KEY=test-cloud-key\n"
        "CLOUDINARY_API_SECRET='$(do-not-execute)'\n"
        "VIDEO_BUILDER_API_KEY=test-api\n"
        "UNRELATED_ENV_SETTING=ignored\n"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text("WHISPER_API_KEY=wrong-file\n")
    monkeypatch.chdir(outside)

    config = load_config()

    check_transcription_config(config)
    assert os.environ["WHISPER_API_KEY"] == "test whisper"
    assert os.environ["BASEROW_API_TOKEN"] == "test-baserow"
    assert os.environ["CLOUDINARY_API_KEY"] == "test-cloud-key"
    assert os.environ["CLOUDINARY_API_SECRET"] == "$(do-not-execute)"
    assert os.environ["VIDEO_BUILDER_API_KEY"] == "test-api"
    assert "UNRELATED_ENV_SETTING" not in os.environ


@pytest.mark.parametrize("exported", ["shell-value", ""])
def test_exported_values_take_priority_including_explicit_blanks(project, monkeypatch, exported):
    monkeypatch.setenv("WHISPER_API_KEY", exported)
    (project / ".env").write_text('WHISPER_API_KEY="malformed-file-value\n')
    load_config()
    assert os.environ["WHISPER_API_KEY"] == exported


def test_custom_configuration_uses_configured_key_name(project):
    custom = project / "custom.yaml"
    custom.write_text("whisper:\n  api_key_env: CUSTOM_WHISPER_KEY\n")
    (project / ".env").write_text("CUSTOM_WHISPER_KEY=custom-value\nWHISPER_API_KEY=ignored\n")
    load_config(custom)
    assert os.environ["CUSTOM_WHISPER_KEY"] == "custom-value"
    assert "WHISPER_API_KEY" not in os.environ


def test_missing_env_leaves_credentials_for_deferred_preflight(project):
    config = load_config()
    with pytest.raises(ValueError, match=r"WHISPER_API_KEY.*\.env"):
        check_transcription_config(config)


@pytest.mark.parametrize("value", ['"secret-with-unclosed-quote', "private unquoted value"])
def test_malformed_credentials_do_not_disclose_secrets_or_partially_load(project, value):
    (project / ".env").write_text(
        f"VIDEO_BUILDER_API_KEY=valid-private-key\nWHISPER_API_KEY={value}\n"
    )
    with pytest.raises(ValueError, match=r"WHISPER_API_KEY.*\.env") as error:
        load_config()
    assert "private" not in str(error.value)
    assert "secret" not in str(error.value)
    assert "VIDEO_BUILDER_API_KEY" not in os.environ


def test_missing_key_and_unrelated_malformed_lines_are_ignored(project):
    (project / ".env").write_text('UNRELATED_ENV_SETTING="unterminated\n# comment\n')
    load_environment({"WHISPER_API_KEY"}, project / ".env")
    assert "WHISPER_API_KEY" not in os.environ
    assert "UNRELATED_ENV_SETTING" not in os.environ


def test_console_loads_dotenv_before_processing_preflight(project, monkeypatch):
    import main

    (project / ".env").write_text("WHISPER_API_KEY=test-whisper\n")
    started = []
    closed = []
    monkeypatch.setattr(main, "configure", lambda path: [])
    monkeypatch.setattr(
        main,
        "Processor",
        lambda config: SimpleNamespace(config=config, close=lambda: closed.append(True)),
    )

    def dashboard(processor, logs):
        def run():
            check_transcription_config(processor.config)
            started.append(True)

        return SimpleNamespace(run=run)

    monkeypatch.setattr(main, "Dashboard", dashboard)
    main.main()
    assert started == closed == [True]


def test_api_loads_authentication_and_transcription_keys_from_dotenv(project):
    (project / ".env").write_text("VIDEO_BUILDER_API_KEY=test-api\nWHISPER_API_KEY=test-whisper\n")
    processor = SimpleNamespace(status=lambda: {"status": "idle"}, close=lambda: None)
    with TestClient(create_app(processor=processor)) as client:
        assert client.get("/status").status_code == 401
        assert client.get("/status", headers={"X-API-Key": "test-api"}).status_code == 200
        assert os.environ["WHISPER_API_KEY"] == "test-whisper"
