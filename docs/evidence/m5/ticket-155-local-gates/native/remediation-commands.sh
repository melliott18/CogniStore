#!/bin/sh
# Local-only native-stage validation; no candidate freeze, promotion, or deployment.
set -eu
cd <CHECKOUT>
docker build --no-cache --pull=false --platform linux/amd64 --target native --file release/Dockerfile --tag cognistore:155-native-remediation . > /tmp/cognistore-155-native-inspection/native-build.log 2>&1
docker image inspect cognistore:155-native-remediation > /tmp/cognistore-155-native-inspection/native-image-inspect.json
docker run --rm -i --network none --platform linux/amd64 --entrypoint python cognistore:155-native-remediation - < /tmp/cognistore-155-native-inspection/native-smoke.py > /tmp/cognistore-155-native-inspection/native-smoke.json 2> /tmp/cognistore-155-native-inspection/native-smoke.stderr.log
git diff --check
