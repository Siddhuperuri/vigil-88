"""The intrusion module (06 §4, §11). Exports the descriptor and factory the registry needs."""

from vigil.events.builtin.intrusion.module import MODULE, MODULE_ID, IntrusionModule, create
from vigil.events.builtin.intrusion.params import IntrusionParams

__all__ = ["MODULE", "MODULE_ID", "IntrusionModule", "IntrusionParams", "create"]
