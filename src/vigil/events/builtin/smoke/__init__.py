"""The `smoke` module (ADR 0015). A declaration that smoke detection is absent, not a detector."""

from vigil.events.builtin.smoke.module import MODULE, MODULE_ID, SmokeModule, create

__all__ = ["MODULE", "MODULE_ID", "SmokeModule", "create"]
