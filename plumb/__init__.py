__version__ = "0.1.0"


class PlumbError(Exception):
    """Base exception for all Plumb errors."""


class PlumbInferenceError(PlumbError):
    """Raised when an LLM inference call fails after retries."""


class PlumbAuthError(PlumbError):
    """Raised when the API key is missing or invalid."""
