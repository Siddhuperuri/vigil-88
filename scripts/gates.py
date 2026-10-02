"""Repository gates that no linter expresses (01 §12 step 7).

    uv run python scripts/gates.py

  * clock       - no reads of real time outside core/clock.py (decision logic uses frame times)
  * except      - no bare `except:` and no swallowed broad exceptions
  * paths       - no machine-specific absolute paths in source, config or scripts
  * secrets     - no credential-shaped literals, no credentialed URLs in non-test code
  * size        - no source file beyond MAX_LINES (a giant file is a modularity failure)

A line may opt out with a trailing `# gate: allow` plus a reason; use sparingly.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MAX_LINES = 500
ALLOW_MARKER = "gate: allow"
CLOCK_MODULE = Path("src/vigil/core/clock.py")

_TIME_ATTRS = {
    "time",
    "monotonic",
    "monotonic_ns",
    "perf_counter",
    "perf_counter_ns",
    "now",
    "utcnow",
    "today",
}
_TIME_OWNERS = {"time", "datetime", "date"}
_BROAD = {"Exception", "BaseException"}

_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|/(?:Users|home)/[A-Za-z0-9_.-]+")
_SECRET_RES = {
    "credentialed URL": re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@'\"${}]+:[^@\s'\"${}]+@"),
    "credential literal": re.compile(
        r"""(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*["'][^"'${\s][^"']{5,}["']"""
    ),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "API secret key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
_TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".ps1", ".json", ".example"}


@dataclass(frozen=True, slots=True)
class Violation:
    gate: str
    file: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"[{self.gate}] {self.file}:{self.line}: {self.message}"


def _files(root: Path, *dirs: str) -> Iterator[Path]:
    for d in dirs:
        base = root / d
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                wanted = p.suffix in _TEXT_SUFFIXES or p.name == ".env.example"
                skipped = any(part in {"__pycache__", ".venv"} for part in p.parts)
                if p.is_file() and wanted and not skipped:
                    yield p


def _allowed(line: str) -> bool:
    return ALLOW_MARKER in line


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def _parse(root: Path, p: Path, out: list[Violation]) -> ast.Module | None:
    try:
        return ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        out.append(Violation("syntax", _rel(root, p), exc.lineno or 0, f"cannot parse: {exc.msg}"))
        return None


def _attr_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def check_clock(root: Path) -> list[Violation]:
    out: list[Violation] = []
    for p in _files(root, "src"):
        if p.suffix != ".py" or p.relative_to(root) == CLOCK_MODULE:
            continue
        lines = p.read_text(encoding="utf-8").splitlines()
        tree = _parse(root, p, out)
        for node in ast.walk(tree) if tree else ():
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _TIME_ATTRS
                and _attr_name(node.func.value) in _TIME_OWNERS
                and not _allowed(lines[node.lineno - 1])
            ):
                call = f"{_attr_name(node.func.value)}.{node.func.attr}()"
                out.append(
                    Violation(
                        "clock",
                        _rel(root, p),
                        node.lineno,
                        f"{call} reads real time; use the injected Clock",
                    )
                )
    return out


def _swallows(handler: ast.ExceptHandler) -> bool:
    body = [
        s
        for s in handler.body
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    return all(isinstance(s, ast.Pass | ast.Continue) for s in body)


def check_except(root: Path) -> list[Violation]:
    out: list[Violation] = []
    for p in _files(root, "src", "scripts"):
        if p.suffix != ".py":
            continue
        lines = p.read_text(encoding="utf-8").splitlines()
        tree = _parse(root, p, out)
        for node in ast.walk(tree) if tree else ():
            if not isinstance(node, ast.ExceptHandler) or _allowed(lines[node.lineno - 1]):
                continue
            if node.type is None:
                out.append(Violation("except", _rel(root, p), node.lineno, "bare `except:`"))
            elif _attr_name(node.type) in _BROAD and _swallows(node):
                out.append(
                    Violation(
                        "except",
                        _rel(root, p),
                        node.lineno,
                        "broad exception swallowed without logging or counting",
                    )
                )
    return out


def check_paths(root: Path) -> list[Violation]:
    out: list[Violation] = []
    for p in _files(root, "src", "config", "scripts"):
        if p.name == "gates.py":
            continue  # this file contains the patterns it searches for
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _PATH_RE.search(line) and not _allowed(line):
                out.append(Violation("paths", _rel(root, p), i, "machine-specific absolute path"))
    return out


def check_secrets(root: Path) -> list[Violation]:
    out: list[Violation] = []
    for p in _files(root, "src", "config", "scripts"):
        if p.name == "gates.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _allowed(line):
                continue
            for name, rx in _SECRET_RES.items():
                if rx.search(line):
                    out.append(Violation("secrets", _rel(root, p), i, f"possible {name}"))
    env_example = root / ".env.example"
    if env_example.is_file():
        for i, line in enumerate(env_example.read_text(encoding="utf-8").splitlines(), 1):
            if "=" in line and not line.lstrip().startswith("#") and line.split("=", 1)[1].strip():
                out.append(Violation("secrets", ".env.example", i, "placeholder must be empty"))
    return out


def check_size(root: Path) -> list[Violation]:
    out: list[Violation] = []
    for p in _files(root, "src"):
        if p.suffix == ".py":
            n = len(p.read_text(encoding="utf-8").splitlines())
            if n > MAX_LINES:
                out.append(Violation("size", _rel(root, p), n, f"{n} lines exceeds {MAX_LINES}"))
    return out


GATES = (check_clock, check_except, check_paths, check_secrets, check_size)


def run_gates(root: Path) -> list[Violation]:
    return list(dict.fromkeys(v for gate in GATES for v in gate(root)))  # de-duplicated, ordered


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations = run_gates(root)
    for v in violations:
        sys.stdout.write(f"{v}\n")
    sys.stdout.write(
        f"gates: {len(violations)} violation(s)\n" if violations else "gates: all clean\n"
    )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
