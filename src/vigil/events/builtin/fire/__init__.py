"""The `fire` module (ADR 0015). A declaration that fire detection is absent, not a detector."""

from vigil.events.builtin.fire.module import MODULE, MODULE_ID, FireModule, create

__all__ = ["MODULE", "MODULE_ID", "FireModule", "create"]
