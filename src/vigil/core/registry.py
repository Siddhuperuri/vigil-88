"""Generic plugin registry with capability gating (D-008).

Discovery of concrete modules (entry points, `events/builtin/`) arrives with the event
engine in P3. This registry is the mechanism: it stores plugins with their declared
requirements and answers, for a given set of available capabilities, which may run.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Generic, TypeVar

from vigil.core.capabilities import (
    Availability,
    AvailabilityReport,
    Capability,
    resolve_availability,
)
from vigil.core.errors import CapabilityError

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _Entry(Generic[T]):
    plugin: T
    requires: frozenset[Capability]
    optional: frozenset[Capability]


class PluginRegistry(Generic[T]):
    def __init__(self) -> None:
        self._entries: dict[str, _Entry[T]] = {}

    def register(
        self,
        plugin_id: str,
        plugin: T,
        *,
        requires: Iterable[Capability],
        optional: Iterable[Capability] = (),
    ) -> None:
        if plugin_id in self._entries:
            raise CapabilityError(f"plugin {plugin_id!r} is already registered")
        self._entries[plugin_id] = _Entry(plugin, frozenset(requires), frozenset(optional))

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def get(self, plugin_id: str) -> T:
        try:
            return self._entries[plugin_id].plugin
        except KeyError:
            raise CapabilityError(f"unknown plugin {plugin_id!r}") from None

    def availability(self, plugin_id: str, available: frozenset[Capability]) -> AvailabilityReport:
        if plugin_id not in self._entries:
            raise CapabilityError(f"unknown plugin {plugin_id!r}")
        e = self._entries[plugin_id]
        return resolve_availability(e.requires, e.optional, available)

    def table(self, available: frozenset[Capability]) -> dict[str, AvailabilityReport]:
        return {pid: self.availability(pid, available) for pid in self.ids()}

    def runnable(self, available: frozenset[Capability]) -> tuple[str, ...]:
        """Ids whose required capabilities are all present (ACTIVE or DEGRADED)."""
        return tuple(
            pid
            for pid, report in self.table(available).items()
            if report.status is not Availability.UNAVAILABLE
        )
