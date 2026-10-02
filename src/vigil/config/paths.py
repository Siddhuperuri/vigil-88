"""Every filesystem location the application uses, derived in one place (01 §10).

Relative paths resolve against `base_dir` (the directory that contains `config/`), never
against the process working directory, so behaviour does not depend on where it was launched.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from vigil.config.settings import Settings


@dataclass(frozen=True, slots=True)
class ResolvedPaths:
    base_dir: Path
    config_dir: Path
    data_dir: Path
    models_dir: Path
    logs_dir: Path
    evidence_dir: Path
    db_dir: Path
    bench_dir: Path
    log_file: Path

    def directories(self) -> tuple[Path, ...]:
        return (
            self.data_dir,
            self.models_dir,
            self.logs_dir,
            self.evidence_dir,
            self.db_dir,
            self.bench_dir,
        )

    def ensure_directories(self) -> None:
        for d in self.directories():
            d.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def check_writable(self) -> tuple[bool, str | None]:
        """Create and remove a probe file in the data dir. Returns (ok, error)."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.data_dir, prefix=".vigil-probe-"):
                pass
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, None


def resolve_paths(settings: Settings, *, config_dir: Path) -> ResolvedPaths:
    base_dir = config_dir.resolve().parent

    def absolute(p: Path) -> Path:
        return (p if p.is_absolute() else base_dir / p).resolve()

    cfg = settings.paths
    data_dir = absolute(cfg.data_dir)
    logs_dir = absolute(cfg.logs_dir) if cfg.logs_dir else data_dir / "logs"
    json_file = settings.logging.json_file
    return ResolvedPaths(
        base_dir=base_dir,
        config_dir=config_dir.resolve(),
        data_dir=data_dir,
        models_dir=absolute(cfg.models_dir) if cfg.models_dir else data_dir / "models",
        logs_dir=logs_dir,
        evidence_dir=absolute(cfg.evidence_dir) if cfg.evidence_dir else data_dir / "evidence",
        db_dir=absolute(cfg.db_dir) if cfg.db_dir else data_dir / "db",
        bench_dir=absolute(cfg.bench_dir) if cfg.bench_dir else data_dir / "bench",
        log_file=absolute(json_file) if json_file else logs_dir / "vigil.jsonl",
    )
