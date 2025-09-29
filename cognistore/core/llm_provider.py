from __future__ import annotations

from typing import Protocol


class LLMProvider(Protocol):  # pragma: no cover - interface
    def complete_json(self, prompt: str, schema_hint: str | None = None) -> dict:
        ...


class MockLLMProvider:
    """Deterministic mock provider for unit tests."""

    def __init__(self, responses: dict[str, dict]):
        self.responses = responses

    def complete_json(self, prompt: str, schema_hint: str | None = None) -> dict:
        # For simplicity, match on schema_hint when provided else prompt
        key = schema_hint or prompt
        return self.responses.get(key, {"action": "stay", "reason": "mock default"})
