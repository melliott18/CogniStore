"""Verified client identity and JWT/OIDC authentication."""

from .principal import Principal, current_principal, principal_context

__all__ = ["Principal", "current_principal", "principal_context"]
