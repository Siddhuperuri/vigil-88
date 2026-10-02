"""Secret and environment resolution.

Real environment variables win over `.env` values. `.env` is parsed into a dict and never
pushed into `os.environ`, so loading configuration has no process-global side effects.
Windows Credential Manager (`keyring`) is not implemented; see docs/LIMITATIONS.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_dotenv(text: str) -> dict[str, str]:
    """Minimal dotenv: KEY=VALUE, optional quotes, `#` comments. No interpolation."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse_dotenv(path.read_text(encoding="utf-8"))


class EnvResolver:
    """Looks a name up in the real environment first, then in `.env`."""

    def __init__(self, env: Mapping[str, str], dotenv: Mapping[str, str] | None = None) -> None:
        self._env = env
        self._dotenv = dotenv or {}

    def get(self, name: str) -> str | None:
        if name in self._env:
            return self._env[name]
        return self._dotenv.get(name)

    def merged(self) -> dict[str, str]:
        """`.env` values underneath the real environment."""
        return {**self._dotenv, **self._env}
