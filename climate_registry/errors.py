class RegistryError(Exception):
    """Base error for registry audit operations."""


class RegistryInputError(RegistryError):
    """Raised when an audit input or destination is unsafe or invalid."""


class RegistryBuildError(RegistryError):
    """Raised when the audit registry cannot be built."""
