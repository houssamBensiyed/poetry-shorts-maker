"""Build captioned vertical videos from remotely tracked audio jobs.

The package separates queue ownership (processor), per-job completion and recovery
(execution), media preparation (pipeline), and provider selection (bootstrap).
Adapters perform network and FFmpeg work behind the contracts module so those
services can be replaced independently. Importing this package starts no jobs;
main.py and api.py provide explicit user-controlled entry points.
"""
