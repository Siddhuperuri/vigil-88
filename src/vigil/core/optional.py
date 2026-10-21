"""Importing an optional dependency without taking the CLI down with it.

`numpy` and `opencv-python-headless` live in the `capture` extra, not in the core dependencies
(ADR 0011): capturing frames should never be a condition of *starting*, because the command an
operator runs when something is missing is `vigil doctor`, and a `doctor` that cannot import is
worthless.

P6's packaging check found that it was. A bare `pip install vigil-88` could not run even
`vigil --version`: `cli/main.py` imports every command, `commands/bench.py` reaches
`pipeline/app.py`, and eight modules down the chain `pipeline/annotate.py` did
`import cv2` at module scope. The failure was a raw `ModuleNotFoundError` traceback — the
opposite of degrading honestly.

So a module that needs one of these imports it through `optional_import`, which hands back
either the module or a recorded reason, and guards its entry points with `require`. The module
still imports; the *functions* refuse, naming the extra to install. Nothing pretends to work.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

from vigil.core.errors import CapabilityError

# Which extra supplies each optional module, so the error can name the fix rather than the gap.
EXTRA_FOR: dict[str, str] = {
    "cv2": "capture",
    "numpy": "capture",
    "onnxruntime": "onnx-gpu",
    "reportlab": "reports",
}


def optional_import(name: str) -> tuple[ModuleType | None, str | None]:
    """Import `name`, or report why it could not be imported. Never raises."""
    try:
        return import_module(name), None
    except ImportError as exc:
        return None, str(exc)


def require(*names: str) -> None:
    """Raise a `CapabilityError` naming the install command if any of `names` is absent.

    Called at the top of every entry point in a module that holds an optional import, so the
    refusal happens where an operator can act on it rather than at interpreter start-up.
    """
    missing = [name for name in names if optional_import(name)[0] is None]
    if not missing:
        return
    extras = sorted({EXTRA_FOR.get(name, name) for name in missing})
    install = " ".join(f"--extra {extra}" for extra in extras)
    raise CapabilityError(
        f"this needs {', '.join(missing)}, which {'is' if len(missing) == 1 else 'are'} not "
        f"installed: uv sync {install}",
        context={"missing": missing, "extras": extras},
    )
