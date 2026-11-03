"""The line-crossing module. Exports the descriptor and factory the registry needs."""

from vigil.events.builtin.line_crossing.module import (
    MODULE,
    MODULE_ID,
    LineCrossingModule,
    create,
)
from vigil.events.builtin.line_crossing.params import LineCrossingParams

__all__ = ["MODULE", "MODULE_ID", "LineCrossingModule", "LineCrossingParams", "create"]
