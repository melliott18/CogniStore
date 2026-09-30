#!/bin/sh
set -eu
docker build --progress=plain --label org.opencontainers.image.source=https://github.com/fsouza/fake-gcs-server --label org.opencontainers.image.revision=3c29d20789f6475f65d2554ad6898c4afd7124bb --tag cognistore/gcs-emulator:155-3c29d20789f6 https://github.com/fsouza/fake-gcs-server.git#3c29d20789f6475f65d2554ad6898c4afd7124bb
