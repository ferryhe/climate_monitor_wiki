class ClimateDeliveryError(Exception):
    """Base class for expected, user-facing failures."""


class InputError(ClimateDeliveryError):
    """Invalid report, path, or server-only configuration."""


class GenerationError(ClimateDeliveryError):
    """Summary or PDF generation failed."""


class DeliveryError(ClimateDeliveryError):
    """An SMTP delivery attempt failed with a known outcome."""


class LockStateError(ClimateDeliveryError):
    """A concurrent run or ambiguous prior delivery blocks progress."""
