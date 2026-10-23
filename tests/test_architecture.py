"""Keep application policy independent of provider and rendering implementations."""

import ast
from pathlib import Path


def test_core_modules_do_not_import_concrete_providers():
    root = Path(__file__).resolve().parents[1] / "app"
    forbidden = {
        "app.jobs",
        "app.downloads",
        "app.images",
        "app.uploads",
        "app.transcription",
        "app.video_builder",
        "httpx",
        "cloudinary",
    }
    for name in ("processor", "execution", "pipeline", "contracts", "models", "buffer"):
        tree = ast.parse((root / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden, f"{name} imports {node.module}"
            elif isinstance(node, ast.Import):
                assert not forbidden.intersection(alias.name for alias in node.names)


def test_default_composition_has_no_baserow_or_cloudinary_imports():
    path = Path(__file__).resolve().parents[1] / "app" / "bootstrap.py"
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in {"app.jobs", "app.uploads", "app.automations", "cloudinary"}
