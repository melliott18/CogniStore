"""Pytest configuration local to opt-in integration tests."""


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires a separately managed external service",
    )
