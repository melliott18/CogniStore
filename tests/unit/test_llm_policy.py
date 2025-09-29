from cognistore.core.policy import LLMPolicy, PolicyLLMProvider


class FakePolicyProvider:
    def __init__(self, mapping: dict):
        self.mapping = mapping

    def decide(self, inputs: dict) -> dict:  # matches PolicyLLMProvider
        key = (inputs.get("current_tier"), inputs.get("size"))
        return self.mapping.get(key, {"action": "stay", "reason": "no-op"})


def test_llm_policy_move_and_stay():
    provider = FakePolicyProvider({
        ("hot", 10): {"action": "move", "dst_tier": "warm", "reason": "test move"},
        ("warm", 1): {"action": "stay", "reason": "already optimal"},
    })
    policy = LLMPolicy(provider, allowed_tiers=("hot", "warm"))

    dec1 = policy.evaluate("hot", 10)
    assert dec1.action == "move" and dec1.dst_tier == "warm"

    dec2 = policy.evaluate("warm", 1)
    assert dec2.action == "stay" and dec2.dst_tier is None
