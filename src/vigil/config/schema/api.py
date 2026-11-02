"""HTTP API settings. The API arrives in P1; the security rules are enforced from day one."""

from __future__ import annotations

import ipaddress
from typing import Self

from pydantic import Field, SecretStr, model_validator

from vigil.config.schema.base import ConfigModel

MIN_TOKEN_LENGTH = 32
_LOOPBACK_NAMES = frozenset({"localhost"})


def is_loopback_host(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class AuthConfig(ConfigModel):
    enabled: bool = False
    token: SecretStr | None = None  # reference via ${env:VIGIL_API_TOKEN}


class AccountsConfig(ConfigModel):
    """Local accounts and cookie sessions (ADR 0016). Off for `vigil run --serve`, which keeps its
    optional shared token; `vigil serve`, the desktop engine, always turns it on."""

    enabled: bool = False
    idle_timeout_minutes: int = Field(default=15, ge=5, le=480)
    absolute_lifetime_hours: int = Field(default=12, ge=1, le=72)


class ApiConfig(ConfigModel):
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8088, ge=0, le=65535)  # 0: the OS picks (`vigil serve`)
    push_interval_ms: int = Field(default=100, ge=10)
    stream_fps: float = Field(default=10.0, gt=0, le=60)
    stream_jpeg_quality: int = Field(default=70, ge=1, le=100)
    auth: AuthConfig = AuthConfig()
    accounts: AccountsConfig = AccountsConfig()

    @model_validator(mode="after")
    def _non_loopback_requires_auth(self) -> Self:
        if is_loopback_host(self.bind_host):
            return self
        if not self.auth.enabled:
            raise ValueError(
                f"api.bind_host {self.bind_host!r} is not loopback: set api.auth.enabled=true "
                "or bind to 127.0.0.1"
            )
        token = self.auth.token.get_secret_value() if self.auth.token else ""
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"api.auth.token must be at least {MIN_TOKEN_LENGTH} characters when binding "
                "a non-loopback interface (set VIGIL_API_TOKEN)"
            )
        return self
