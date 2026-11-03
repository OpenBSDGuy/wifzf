#!/bin/sh
# Build and upload wifzf to PyPI.
# Requires: pip install build twine, and a PyPI token (~/.pypirc or TWINE_PASSWORD).
set -e

rm -rf dist build ./*.egg-info
python -m build
twine check dist/*
twine upload dist/*
