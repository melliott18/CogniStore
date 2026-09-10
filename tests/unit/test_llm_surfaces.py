"""LLM evidence and safe defaults through the public policy surfaces."""

from __future__ import annotations

import json

import pytest

from cognistore.api.gateway import CogniStoreGateway
from cognistore.api.models import PolicyConfig, PolicyEvaluationRequest
from cognistore.cli import cognistore_cli
from cognistore.core import policy_factory
from cognistore.core.catalog import Catalog
from cognistore.core.placement_llm import CallablePlacementProvider, PlacementInference
from cognistore.drivers.posix_driver import PosixDriver
from cognistore.sdk.models import PolicyEvaluationResponse as SDKPolicyEvaluationResponse


def configured_run(tmp_path, monkeypatch, response):
    catalog = Catalog()
    catalog.upsert("bucket", "private-object.txt", 5, tier="warm")
    drivers = {tier: PosixDriver(tmp_path / tier) for tier in ("hot", "warm")}
    drivers["warm"].put_object("bucket", "private-object.txt", b"12345")
    prompts = []

    def complete(prompt, schema, timeout_seconds):
        prompts.append(prompt)
        return response

    provider = CallablePlacementProvider(
        complete, provider_id="test", model="placement-test", version="1",
    )
    monkeypatch.setattr(
        policy_factory, "placement_inference_from_env", lambda: PlacementInference(provider),
    )
    monkeypatch.setattr(cognistore_cli, "Catalog", lambda **kwargs: catalog)
    monkeypatch.setattr(cognistore_cli, "load_drivers", lambda path: drivers)
    return catalog, drivers, prompts


def forbid_write(*args, **kwargs):
    pytest.fail("dry-run attempted a write")


@pytest.mark.parametrize("response,action", [
    ('{"action":"move","dst_tier":"hot","reason":"api_key=private-value"}', "move"),
    ('{"action":"move","dst_tier":"hot"}', "stay"),
])
def test_cli_llm_dry_run_exposes_redacted_evidence_without_writes(
    tmp_path, monkeypatch, capsys, response, action,
):
    catalog, drivers, prompts = configured_run(tmp_path, monkeypatch, response)
    for method in ("append_audit_event", "upsert", "update_placement", "delete"):
        monkeypatch.setattr(catalog, method, forbid_write)
    for driver in drivers.values():
        monkeypatch.setattr(driver, "put_object", forbid_write)
        monkeypatch.setattr(driver, "delete_object", forbid_write)
    assert cognistore_cli.main([
        "--drivers", "unused.yaml", "policy-run", "bucket", "--policy", "llm",
        "--dry-run", "--json",
    ]) == 0
    text = capsys.readouterr().out
    result = json.loads(text)
    evaluation = result["evaluations"][0]
    assert evaluation["action"] == action
    assert evaluation["llm_audit"]["final_decision"]["action"] == action
    assert evaluation["llm_audit"]["prompt"] == prompts[0]
    assert "private-object.txt" not in prompts[0]
    assert "private-value" not in text
    assert catalog.list_audit_events() == []
    assert catalog.get("bucket", "private-object.txt").tier == "warm"
    assert drivers["warm"].get_object("bucket", "private-object.txt") == b"12345"
    assert not (tmp_path / "hot").exists()


def test_api_and_sdk_preserve_llm_evidence(tmp_path, monkeypatch):
    catalog, drivers, prompts = configured_run(
        tmp_path, monkeypatch,
        '{"action":"move","dst_tier":"hot","reason":"small object"}',
    )
    response = CogniStoreGateway(catalog, drivers).evaluate_policy(PolicyEvaluationRequest(
        bucket="bucket", key="private-object.txt", config=PolicyConfig(policy="llm"),
    ))
    public = response.model_dump(mode="json")
    assert public["action"] == "move"
    assert public["llm_audit"]["prompt"] == prompts[0]
    sdk = SDKPolicyEvaluationResponse.model_validate(public)
    assert sdk.llm_audit == public["llm_audit"]
    assert catalog.list_audit_events() == []
    assert not (tmp_path / "hot").exists()


def test_factory_no_provider_never_uses_old_threshold(monkeypatch):
    monkeypatch.delenv("COGNISTORE_LLM_ENDPOINT", raising=False)
    for threshold in (0, 1000000):
        policy = policy_factory.build_policy(
            "llm", threshold=threshold, llm_threshold=threshold,
            allowed_tiers=["hot", "warm"],
        )
        decision = policy.evaluate("warm", 1)
        assert decision.action == "stay"
        assert decision.reason == "llm_provider_unavailable"
        assert decision.llm_audit["fallback_reason"] == "llm_provider_unavailable"
