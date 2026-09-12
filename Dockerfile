# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ARG NATS_IMAGE=nats:2.10.26-alpine@sha256:d69eb29526c1d98afdfb2e2434763bef77b5f3c83e2e24769c13a4d104be475e
ARG MINIO_IMAGE=quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z@sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e

# The upstream service images default to root. These thin targets prepare the
# persistent data paths during the build, then run the services as an
# unprivileged numeric identity. Runtime credentials are supplied by Compose.
FROM ${NATS_IMAGE} AS nats
USER 0:0
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001

FROM ${MINIO_IMAGE} AS minio
USER 0:0
RUN mkdir -p /data && chown 10001:10001 /data
ENV HOME=/tmp
USER 10001:10001

FROM ${PYTHON_IMAGE} AS python-base

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG COGNISTORE_UID=10001
ARG COGNISTORE_GID=10001
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libmagic1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${COGNISTORE_GID}" cognistore \
    && useradd \
        --uid "${COGNISTORE_UID}" \
        --gid "${COGNISTORE_GID}" \
        --create-home \
        --home-dir /home/cognistore \
        --shell /usr/sbin/nologin \
        cognistore

FROM python-base AS runtime-builder

ENV PATH=/opt/cognistore/bin:${PATH}
WORKDIR /build

RUN python -m venv /opt/cognistore
COPY pyproject.toml README.md LICENSE ./
COPY cognistore ./cognistore
RUN python -m pip install --no-compile .

FROM python-base AS runtime

LABEL org.opencontainers.image.title="CogniStore" \
      org.opencontainers.image.description="CogniStore runtime and background worker" \
      org.opencontainers.image.source="https://github.com/melliott18/CogniStore"

ENV PATH=/opt/cognistore/bin:${PATH}
COPY --from=runtime-builder --chown=cognistore:cognistore /opt/cognistore /opt/cognistore
RUN install -d -o cognistore -g cognistore /var/lib/cognistore

WORKDIR /var/lib/cognistore
USER cognistore:cognistore
EXPOSE 8081

ENTRYPOINT ["cognistore"]
CMD ["--help"]

FROM python-base AS development-builder

ENV PATH=/opt/cognistore/bin:${PATH}
WORKDIR /workspace

RUN python -m venv /opt/cognistore
COPY pyproject.toml README.md LICENSE ./
COPY cognistore ./cognistore
COPY tests ./tests
RUN python -m pip install --no-compile --editable ".[dev,azure]"

FROM python-base AS development

LABEL org.opencontainers.image.title="CogniStore development" \
      org.opencontainers.image.description="CogniStore developer and test environment" \
      org.opencontainers.image.source="https://github.com/melliott18/CogniStore"

ENV PATH=/opt/cognistore/bin:${PATH}
WORKDIR /workspace

COPY --from=development-builder --chown=cognistore:cognistore /opt/cognistore /opt/cognistore
COPY --chown=cognistore:cognistore pyproject.toml README.md LICENSE CONTRIBUTING.md conftest.py requirements.txt ./
COPY --chown=cognistore:cognistore cognistore ./cognistore
COPY --chown=cognistore:cognistore tests ./tests
COPY --chown=cognistore:cognistore docker ./docker
COPY --chown=cognistore:cognistore drivers.yaml drivers.rpi.yaml ./
RUN install -d -o cognistore -g cognistore \
        /test-results \
        /var/lib/cognistore

USER cognistore:cognistore

CMD ["python", "-m", "pytest", "tests/unit", "tests/conformance"]
