"""Shared base for every config model.

`extra="forbid"`: an unknown key is an ERROR, never a warning. A silently-ignored typo in a
threshold is indistinguishable from the feature not working (06 §5).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        str_strip_whitespace=True,
    )
