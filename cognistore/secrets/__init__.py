"""Runtime credentials and key unwrapping without persisted plaintext secrets."""

from .core import (
    KeyProvider,
    KeyReference,
    SecretAccessError,
    SecretConfigurationError,
    SecretError,
    SecretProvider,
    SecretReference,
    SecretResolver,
    SecretUnavailableError,
    SecretValue,
)
from .providers import (
    AWSKMSKeyProvider,
    AWSSecretsManagerProvider,
    VaultKVProvider,
    VaultTransitKeyProvider,
)

__all__ = [
    "KeyProvider", "KeyReference", "SecretAccessError", "SecretConfigurationError", "SecretError",
    "SecretProvider", "SecretReference", "SecretResolver", "SecretUnavailableError", "SecretValue",
    "AWSKMSKeyProvider", "AWSSecretsManagerProvider", "VaultKVProvider", "VaultTransitKeyProvider",
]
