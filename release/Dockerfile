# Each assembly resolves once; retain the resulting bundle, never re-resolve to promote.
FROM python:3.12.14-slim-bookworm@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a AS native
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install --yes --no-install-recommends libmagic1 \
    && rm -rf /var/lib/apt/lists/*

FROM native AS assembly
WORKDIR /source
COPY . .
RUN mkdir -p /bundle/build-wheelhouse /bundle/wheelhouse /bundle/tools-wheelhouse \
    && python -m pip download --only-binary=:all: --dest /bundle/build-wheelhouse 'build>=1.2.2' 'setuptools>=77' wheel \
    && python scripts/release_candidate.py lock --wheels /bundle/build-wheelhouse --output /bundle/build.lock \
    && python -m venv /build-tools \
    && /build-tools/bin/pip install --no-index --find-links /bundle/build-wheelhouse --require-hashes -r /bundle/build.lock \
    && /build-tools/bin/python -m build --wheel --no-isolation --outdir /bundle/wheelhouse \
    && python -m pip download --only-binary=:all: --dest /bundle/wheelhouse /bundle/wheelhouse/cognistore-*.whl pip \
    && python scripts/release_candidate.py lock --wheels /bundle/wheelhouse --output /bundle/runtime.lock \
    && python scripts/release_candidate.py lock --wheels /bundle/wheelhouse --output /bundle/bootstrap.lock --only-package pip \
    && python scripts/release_candidate.py constraints --lock /bundle/runtime.lock --output /bundle/runtime.txt \
    && python scripts/release_candidate.py constraints --lock /bundle/runtime.lock --output /bundle/dependencies.txt --exclude-project \
    && python -m pip download --only-binary=:all: --dest /bundle/tools-wheelhouse \
         --constraint /bundle/runtime.txt pytest pytest-cov 'bandit[toml]' pip-audit \
    && python scripts/release_candidate.py lock --wheels /bundle/tools-wheelhouse --output /bundle/tools.lock

FROM native AS runtime
ARG SOURCE_SHA
ARG PACKAGE_VERSION
LABEL org.opencontainers.image.title="CogniStore release candidate" \
      org.opencontainers.image.source="https://github.com/melliott18/CogniStore" \
      org.opencontainers.image.revision="${SOURCE_SHA}" \
      org.opencontainers.image.version="${PACKAGE_VERSION}"
COPY --from=assembly /bundle/wheelhouse /install/wheelhouse
COPY --from=assembly /bundle/runtime.lock /install/runtime.lock
COPY --from=assembly /bundle/bootstrap.lock /install/bootstrap.lock
RUN python -m pip install --no-index --find-links /install/wheelhouse --require-hashes -r /install/bootstrap.lock \
    && python -m venv /opt/cognistore \
    && /opt/cognistore/bin/pip install --no-index --find-links /install/wheelhouse --require-hashes -r /install/runtime.lock \
    && /opt/cognistore/bin/pip check \
    && rm -rf /install \
    && groupadd --gid 10001 cognistore \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin cognistore \
    && install -d -o 10001 -g 10001 /var/lib/cognistore
ENV PATH=/opt/cognistore/bin:$PATH
WORKDIR /var/lib/cognistore
USER 10001:10001
EXPOSE 8081
ENTRYPOINT ["cognistore"]
CMD ["--help"]

# A separate test environment uses the same hashed wheel/runtime closure.
# No source package or root conftest is on the test import path.
FROM native AS qualification
COPY --from=assembly /bundle /bundle
RUN python -m venv /opt/qualification \
    && /opt/qualification/bin/pip install --no-index --find-links /bundle/wheelhouse --require-hashes -r /bundle/runtime.lock \
    && /opt/qualification/bin/pip install --no-index --find-links /bundle/tools-wheelhouse --require-hashes -r /bundle/tools.lock \
    && /opt/qualification/bin/pip check
ENV PATH=/opt/qualification/bin:$PATH COGNISTORE_SECURITY_PROFILE=development
COPY tests /qualification/tests
COPY pyproject.toml /qualification/pyproject.toml
COPY scripts/release_smoke.py /qualification/release_smoke.py
COPY cognistore /scan/cognistore
WORKDIR /qualification
