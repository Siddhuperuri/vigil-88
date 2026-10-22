"""The person_down module (06 §11). Exports the descriptor and factory the registry needs."""

from vigil.events.builtin.person_down.module import (
    MODULE,
    MODULE_ID,
    PersonDownModule,
    create,
)
from vigil.events.builtin.person_down.params import PersonDownParams

__all__ = ["MODULE", "MODULE_ID", "PersonDownModule", "PersonDownParams", "create"]
