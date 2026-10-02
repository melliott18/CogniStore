FROM cognistore:qualification-160-base-20261002
USER 0:0
COPY tools-wheelhouse /tool-wheels
COPY tools.lock /tools.lock
RUN /usr/local/bin/python -m venv /opt/test-tools && /opt/test-tools/bin/pip install --no-index --find-links=/tool-wheels --require-hashes --only-binary=:all: -r /tools.lock && rm -rf /tool-wheels /tools.lock
COPY extra-test-wheels /extra-test-wheels
COPY extra-test-tools.lock /extra-test-tools.lock
RUN /opt/test-tools/bin/pip install --no-index --find-links=/extra-test-wheels --require-hashes -r /extra-test-tools.lock && rm -rf /extra-test-wheels /extra-test-tools.lock
COPY tests /qualification/tests
COPY scripts /qualification/scripts
COPY pyproject.toml /qualification/pyproject.toml
ENV PATH=/opt/test-tools/bin:$PATH PYTHONPATH=/opt/cognistore/lib/python3.12/site-packages COGNISTORE_SECURITY_PROFILE=development
WORKDIR /qualification
ENTRYPOINT ["python"]
