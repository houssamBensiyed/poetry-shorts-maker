"""Launch the development-only interface: python -m devtools.video_test."""

import argparse

import uvicorn

from .server import create_app


def main():
    parser = argparse.ArgumentParser(description="Run the local Video Builder Test UI.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
