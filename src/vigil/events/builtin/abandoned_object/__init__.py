"""The abandoned_object module (06 §11). Exports the descriptor and factory the registry needs."""

from vigil.events.builtin.abandoned_object.module import (
    MODULE,
    MODULE_ID,
    AbandonedObjectModule,
    create,
)
from vigil.events.builtin.abandoned_object.params import AbandonedObjectParams

__all__ = [
    "MODULE",
    "MODULE_ID",
    "AbandonedObjectModule",
    "AbandonedObjectParams",
    "create",
]
