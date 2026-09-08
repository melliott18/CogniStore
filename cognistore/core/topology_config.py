"""Validated JSON/YAML topology registration through CatalogStore."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .topology import Pool, Tier, _fields

if TYPE_CHECKING:
    from .catalog import CatalogStore


@dataclass(frozen=True)
class TopologyConfig:
    """A self-contained topology whose references are validated before writes."""

    tiers: tuple[Tier, ...]
    pools: tuple[Pool, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tiers, Sequence) or any(
            not isinstance(tier, Tier) for tier in self.tiers
        ):
            raise ValueError("tiers must be a sequence of Tier values")
        if not isinstance(self.pools, Sequence) or any(
            not isinstance(pool, Pool) for pool in self.pools
        ):
            raise ValueError("pools must be a sequence of Pool values")
        by_name = {tier.name: tier for tier in self.tiers}
        if len(by_name) != len(self.tiers):
            raise ValueError("duplicate tier names in topology configuration")
        if len({pool.pool_id for pool in self.pools}) != len(self.pools):
            raise ValueError("duplicate pool IDs in topology configuration")
        for pool in self.pools:
            if pool.tier not in by_name:
                raise ValueError(f"pool {pool.pool_id!r} references an undeclared tier {pool.tier!r}")
            if not by_name[pool.tier].active:
                raise ValueError(f"pool {pool.pool_id!r} requires an active tier for registration")
        object.__setattr__(self, "tiers", deepcopy(tuple(self.tiers)))
        object.__setattr__(self, "pools", deepcopy(tuple(self.pools)))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TopologyConfig:
        values = _fields(value, required={"tiers", "pools"}, optional=set())
        for name in ("tiers", "pools"):
            if isinstance(values[name], (str, bytes)) or not isinstance(values[name], Sequence):
                raise ValueError(f"{name} must be a sequence")
        return cls(
            tiers=tuple(Tier.from_mapping(tier) for tier in values["tiers"]),
            pools=tuple(Pool.from_mapping(pool) for pool in values["pools"]),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "tiers": [tier.to_mapping() for tier in self.tiers],
            "pools": [pool.to_mapping() for pool in self.pools],
        }


def load_topology_config(path: str | Path) -> TopologyConfig:
    """Parse a self-contained .json, .yaml, or .yml file without catalog writes."""

    source = Path(path)
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() == ".json":
        value = json.loads(text)
    elif source.suffix.lower() in (".yaml", ".yml"):
        value = yaml.safe_load(text)
    else:
        raise ValueError("topology configuration must use .json, .yaml, or .yml")
    return TopologyConfig.from_mapping(value)


def register_topology(
    catalog: CatalogStore,
    config: TopologyConfig | Mapping[str, Any],
) -> None:
    """Validate all input, then upsert tiers followed by pools via the contract.

    Registration is additive: omitted catalog entries remain unchanged. Each
    registration is individually atomic; this convenience helper is not a
    transaction spanning the entire configuration and existing catalog state.
    """

    # Revalidation also catches modifications to a detached model's mappings.
    topology = TopologyConfig.from_mapping(
        config.to_mapping() if isinstance(config, TopologyConfig) else config
    )
    for tier in topology.tiers:
        catalog.register_tier(tier.name, tier.metadata, active=tier.active)
    for pool in topology.pools:
        catalog.register_pool(
            pool.pool_id,
            pool.tier,
            pool.metadata,
            region=pool.region,
            members=pool.members,
            localities=pool.localities,
            attributes=pool.attributes,
            active=pool.active,
        )
