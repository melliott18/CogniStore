#!/bin/sh
set -eu
env -u COGNISTORE_GCS_LIVE_BUCKET COGNISTORE_SECURITY_PROFILE=development COGNISTORE_GCS_EMULATOR_ENDPOINT=http://127.0.0.1:55077 /private/tmp/cognistore-165-automation-venv/bin/python -m pytest tests/integration/test_gcs.py -q -o addopts= --junitxml=/tmp/cognistore-155-validation/gcs-pinned-tests.xml
