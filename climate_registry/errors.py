class RegistryError(Exception):
    """Base error for registry operations."""


class RegistryInputError(RegistryError):
    """Raised when a registry input or destination is unsafe or invalid."""


class RegistryBuildError(RegistryError):
    """Raised when a registry candidate cannot be built or validated."""


class RegistryLockError(RegistryError):
    """Raised when a persistent registry update is already in progress."""
