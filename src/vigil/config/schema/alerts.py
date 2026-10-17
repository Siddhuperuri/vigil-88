"""Alert channel settings (04 §3.6, 06 §10).

An unconfigured channel is **absent**, not stubbed: `channels` is an explicit allowlist, and a
channel that cannot work reports `is_available()` false and is not offered in the UI.

Severity is named here as a string Literal rather than imported from `domain.enums.Severity`,
because `config/` is L1 and `domain/` is L2 and the layering forbids the import (01 §5). See
`schema/taxonomy.py` for the full reasoning; a test checks the mirror against the enum, so the
two cannot drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from vigil.config.schema.base import ConfigModel
from vigil.core.redaction import contains_inline_credentials

ChannelName = Literal["console", "jsonl", "webhook"]
# Mirrors domain.enums.Severity; `schema/taxonomy.py` explains why, and a test keeps it honest.
SeverityName = Literal["INFO", "LOW", "MODERATE", "HIGH", "CRITICAL"]
_ALLOWED_SCHEMES = ("http://", "https://")


class AlertsConfig(ConfigModel):
    enabled: bool = True
    # Explicit allowlist. Discovering a channel never enables it, exactly as for modules.
    channels: tuple[ChannelName, ...] = ("console",)
    # Alerts below this band are recorded as incidents but not dispatched: an alert storm
    # destroys the operator's ability to see anything, which is worse than a missed alert.
    min_severity: SeverityName = "MODERATE"
    # Default: <logs_dir>/alerts.jsonl
    jsonl_file: Path | None = None
    # The only outbound network this application makes, and only when set (01 §10).
    webhook_url: str | None = None
    webhook_timeout_ms: int = Field(default=3000, ge=100, le=30_000)
    webhook_max_attempts: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def _webhook_is_safe(self) -> Self:
        url = self.webhook_url
        if url is None:
            if "webhook" in self.channels:
                raise ValueError(
                    "the webhook channel is enabled but alerts.webhook_url is not set; "
                    "a channel that cannot deliver must not be listed"
                )
            return self
        if not url.startswith(_ALLOWED_SCHEMES):
            raise ValueError("alerts.webhook_url must start with http:// or https://")
        if contains_inline_credentials(url):
            raise ValueError(
                "alerts.webhook_url must not contain credentials; put the secret in a header "
                "value referenced as ${env:NAME} or use a signed URL from the receiving system"
            )
        return self
