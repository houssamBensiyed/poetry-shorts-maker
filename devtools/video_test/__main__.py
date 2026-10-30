"""Launch the development-only interface: python -m devtools.video_test."""

import argparse

import uvicorn

from app.config import ROOT, load_config

from .server import create_app


def main():
    parser = argparse.ArgumentParser(description="Run the local Video Builder Test UI.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    config = load_config()
    uvicorn.run(
        create_app(config, config_path=ROOT / "config.yaml"), host="127.0.0.1", port=args.port
    )


if __name__ == "__main__":
    main()
