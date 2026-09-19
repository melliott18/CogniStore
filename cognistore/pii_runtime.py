"""Trusted, tenant-specific configuration for optional scan-time PII detection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from cognistore.auth.tenancy import require_tenant, validate_tenant_id
from cognistore.core.pii import PIIDetectionPipeline, PIIDetector, PIILimits, RegexPIIDetector
from cognistore.policy_feature_runtime import _runtime_config

DetectorFactory = Callable[[], PIIDetector]


@dataclass(frozen=True)
class _Selection:
    detectors: tuple[str, ...] = ()
    limits: PIILimits = field(default_factory=PIILimits)


class PIIConfig:
    """Select fresh detector instances from trusted application factories.

    Configuration never imports arbitrary code. Deployments can explicitly
    supply replacement factories in Python without changing persisted schemas.
    Tenant overrides inherit global settings; an empty detector list disables
    detection for that tenant. Factories must create independent instances.
    """

    def __init__(
        self,
        value: object = None,
        *,
        detector_factories: Mapping[str, DetectorFactory] | None = None,
    ) -> None:
        self._factories = MappingProxyType(
            dict({"regex": RegexPIIDetector} if detector_factories is None else detector_factories)
        )
        if any(
            not isinstance(name, str) or not name or not callable(factory)
            for name, factory in self._factories.items()
        ):
            raise ValueError("invalid PII detector registry")
        block = {} if value is None else value
        if not isinstance(block, Mapping) or set(block) - {"detectors", "limits", "tenants"}:
            raise ValueError("invalid PII configuration")
        self._default = self._selection(
            {key: item for key, item in block.items() if key != "tenants"}, _Selection()
        )
        tenants = block.get("tenants", {})
        if not isinstance(tenants, Mapping):
            raise ValueError("PII tenants must be a mapping")
        self._tenants = MappingProxyType({
            validate_tenant_id(owner): self._selection(settings, self._default)
            for owner, settings in tenants.items()
        })

    def _selection(self, value: object, base: _Selection) -> _Selection:
        if not isinstance(value, Mapping) or set(value) - {"detectors", "limits"}:
            raise ValueError("invalid PII tenant configuration")
        names = value.get("detectors", list(base.detectors))
        if (
            not isinstance(names, list)
            or len(names) > 16
            or any(not isinstance(name, str) or name not in self._factories for name in names)
            or len(set(names)) != len(names)
        ):
            raise ValueError("PII detectors must name unique registered detectors (at most 16)")
        limits = value.get("limits", {})
        if not isinstance(limits, Mapping) or set(limits) - {
            "max_text_bytes", "timeout_seconds", "max_findings"
        }:
            raise ValueError("invalid PII limits")
        return _Selection(
            tuple(names),
            PIILimits(**{
                "max_text_bytes": base.limits.max_text_bytes,
                "timeout_seconds": base.limits.timeout_seconds,
                "max_findings": base.limits.max_findings,
                **limits,
            }),
        )

    def pipeline_for_tenant(self, tenant_id: str) -> PIIDetectionPipeline:
        owner = validate_tenant_id(tenant_id)
        require_tenant(owner)
        selection = self._tenants.get(owner, self._default)
        try:
            detectors = tuple(self._factories[name]() for name in selection.detectors)
            return PIIDetectionPipeline(detectors, limits=selection.limits)
        except Exception:
            # Factory failures are configuration errors, not successful scans;
            # do not relay plugin exception messages containing sensitive data.
            raise ValueError("PII detector initialization failed") from None


def load_pii_config(
    config_path: str | Path | None,
    *,
    detector_factories: Mapping[str, DetectorFactory] | None = None,
) -> PIIConfig:
    """Read the optional ``pii`` block beside driver configuration."""
    block = {} if config_path is None else _runtime_config(config_path).get("pii", {})
    if not isinstance(block, Mapping):
        raise ValueError("PII configuration must be a mapping")
    return PIIConfig(block, detector_factories=detector_factories)
