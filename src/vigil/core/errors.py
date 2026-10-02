"""Exception hierarchy rooted at VigilError (01 §7.4).

Rules: no bare `except:`; catch narrowly at a boundary that can decide something; a
swallowed error MUST increment a named metric and log at WARNING or above.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType


class VigilError(Exception):
    """Base class. Carries structured context instead of formatting it into the message."""

    def __init__(self, message: str, *, context: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: Mapping[str, object] = MappingProxyType(dict(context or {}))


class ConfigError(VigilError):
    """Configuration failed to load or validate. `issues` lists every problem found."""

    def __init__(
        self,
        message: str,
        *,
        issues: Iterable[str] = (),
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.issues: tuple[str, ...] = tuple(issues)

    def __str__(self) -> str:
        if not self.issues:
            return self.message
        bullets = "\n".join(f"  - {issue}" for issue in self.issues)
        return f"{self.message}\n{bullets}"


class SourceError(VigilError):
    """A frame source failed. `retryable=False` means retrying cannot help (e.g. bad auth)."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.retryable = retryable


class TransientSourceError(SourceError):
    """A single bad read. The capture worker tolerates a bounded run of these."""

    def __init__(self, message: str, *, context: Mapping[str, object] | None = None) -> None:
        super().__init__(message, retryable=True, context=context)


class InferenceError(VigilError):
    """A detector failed to produce a result for a batch."""


class ModuleError(VigilError):
    """A detection module raised or violated its contract."""


class PersistenceError(VigilError):
    """A repository operation failed."""


class CapabilityError(VigilError):
    """Something was requested that this build or configuration cannot provide."""


class LifecycleError(VigilError):
    """An operation was invalid for the object's current lifecycle state."""
