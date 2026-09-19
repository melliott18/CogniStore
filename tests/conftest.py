"""Tests-level conftest.

The repository root is now added by the root-level conftest.py.
This file remains for future per-tests fixtures and settings.
"""

import os

import pytest

# Test fixtures intentionally use local emulators and unencrypted temporary
# volumes. Production-policy tests override this explicitly.
os.environ.setdefault("COGNISTORE_SECURITY_PROFILE", "development")


@pytest.fixture(autouse=True)
def development_security_profile(monkeypatch):
    monkeypatch.setenv("COGNISTORE_SECURITY_PROFILE", "development")


@pytest.fixture(autouse=True)
def isolate_secret_redaction_registry():
    """A synthetic credential from one test must not redact another's fixtures."""
    from cognistore.utils import redaction

    with redaction._known_values_lock:
        previous = redaction._known_values.copy()
    yield
    with redaction._known_values_lock:
        redaction._known_values.clear()
        redaction._known_values.update(previous)
