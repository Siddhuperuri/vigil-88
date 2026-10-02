"""Structural guarantees of the domain layer (02 §5): pure, frozen, slotted."""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import subprocess
import sys

import pytest

import vigil.domain as domain

BANNED_IMPORTS = ["numpy", "cv2", "pydantic", "structlog", "yaml", "psutil", "typer", "rich"]


def _domain_dataclasses() -> list[type]:
    found: list[type] = []
    for mod in pkgutil.iter_modules(domain.__path__):
        module = importlib.import_module(f"vigil.domain.{mod.name}")
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and dataclasses.is_dataclass(obj)
                and obj.__module__ == module.__name__
            ):
                found.append(obj)
    return found


def test_domain_import_pulls_in_no_infrastructure_library() -> None:
    code = (
        "import sys, vigil.domain, vigil.core\n"
        f"banned = {BANNED_IMPORTS!r}\n"
        "loaded = sorted(m for m in banned if m in sys.modules)\n"
        "print('LOADED:' + ','.join(loaded))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "LOADED:", out.stdout


def test_there_are_domain_dataclasses_to_check() -> None:
    assert len(_domain_dataclasses()) >= 30


@pytest.mark.parametrize("cls", _domain_dataclasses(), ids=lambda c: c.__name__)
def test_every_domain_dataclass_is_frozen_and_slotted(cls: type) -> None:
    params = cls.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen, f"{cls.__name__} must be frozen"
    assert hasattr(cls, "__slots__"), f"{cls.__name__} must use slots=True"


def test_public_exports_exist() -> None:
    for name in domain.__all__:
        assert hasattr(domain, name), name
