"""The repository gates, the layering contracts and the absence of import cycles."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import grimp
import pytest
from scripts import gates

from tests.conftest import REPO_ROOT


def tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return tmp_path


def found(root: Path, gate: str) -> list[gates.Violation]:
    return [v for v in gates.run_gates(root) if v.gate == gate]


# ------------------------------------------------------------------ the real repository


def test_the_repository_passes_every_gate() -> None:
    assert gates.run_gates(REPO_ROOT) == []


def test_gates_script_exits_zero_on_a_clean_repo() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/gates.py"], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0 and "all clean" in result.stdout


# ------------------------------------------------------------------ clock gate


@pytest.mark.parametrize(
    "snippet",
    [
        "import time\nx = time.time()\n",
        "import time\nx = time.monotonic()\n",
        "import time\nx = time.perf_counter()\n",
        "import datetime\nx = datetime.datetime.now()\n",
        "from datetime import datetime\nx = datetime.utcnow()\n",
    ],
)
def test_clock_gate_catches_real_time_reads(tmp_path: Path, snippet: str) -> None:
    root = tree(tmp_path, {"src/vigil/pipeline/x.py": snippet})
    assert len(found(root, "clock")) == 1


def test_clock_gate_exempts_only_the_clock_module(tmp_path: Path) -> None:
    root = tree(tmp_path, {"src/vigil/core/clock.py": "import time\nx = time.monotonic_ns()\n"})
    assert found(root, "clock") == []


def test_clock_gate_ignores_unrelated_now_calls_and_honours_the_opt_out(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "src/vigil/a.py": "x = notatime.now()\nsched.now()\n",
            "src/vigil/b.py": "import time\nx = time.time()  # gate: allow benchmark wall time\n",
        },
    )
    assert found(root, "clock") == []


# ------------------------------------------------------------------ except gate


def test_except_gate_catches_bare_and_swallowed_broad_handlers(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "src/vigil/bare.py": "try:\n    pass\nexcept:\n    raise\n",
            "src/vigil/swallow.py": "try:\n    pass\nexcept Exception:\n    pass\n",
            "src/vigil/swallow2.py": "try:\n    pass\nexcept BaseException:\n    ...\n",
            "src/vigil/loop.py": (
                "for _ in []:\n    try:\n        pass\n    except Exception:\n        continue\n"
            ),
        },
    )
    assert len(found(root, "except")) == 4


def test_except_gate_allows_narrow_and_handled_exceptions(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "src/vigil/ok.py": (
                "try:\n    pass\nexcept ValueError:\n    pass\n"
                "try:\n    pass\nexcept Exception as e:\n    log(e)\n"
            )
        },
    )
    assert found(root, "except") == []


# ------------------------------------------------------------------ paths and secrets


@pytest.mark.parametrize(
    "line",
    ['P = "C:\\\\Users\\\\bob\\\\x"', 'P = "D:/data"', 'P = "/Users/bob/x"', 'P = "/home/bob"'],
)
def test_path_gate_catches_machine_specific_paths(tmp_path: Path, line: str) -> None:
    root = tree(tmp_path, {"src/vigil/p.py": line + "\n"})
    assert len(found(root, "paths")) == 1


def test_path_gate_ignores_urls_and_relative_paths(tmp_path: Path) -> None:
    root = tree(tmp_path, {"src/vigil/p.py": 'U = "http://x/y"\nV = "rtsp://h/s"\nW = "./var"\n'})
    assert found(root, "paths") == []


@pytest.mark.parametrize(
    "line",
    [
        'url = "rtsp://admin:hunter2@10.0.0.5/s"',
        'password = "correct-horse-battery"',
        'API_KEY: "abcdefghijk"',
        'k = "AKIAABCDEFGHIJKLMNOP"',
        'k = "ghp_' + "a" * 36 + '"',
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_secret_gate_catches_credential_shapes(tmp_path: Path, line: str) -> None:
    root = tree(tmp_path, {"src/vigil/s.py": line + "\n"})
    assert len(found(root, "secrets")) == 1


def test_secret_gate_allows_references_and_empty_placeholders(tmp_path: Path) -> None:
    root = tree(
        tmp_path,
        {
            "config/c.yaml": "token: ${env:VIGIL_API_TOKEN:-}\nurl: rtsp://10.0.0.5/s\n",
            "src/vigil/s.py": 'password_env = "CAM_PASS"\nx = {"token": None}\n',
            ".env.example": "VIGIL_API_TOKEN=\n# FOO=bar\n",
        },
    )
    assert found(root, "secrets") == []


def test_secret_gate_rejects_a_filled_in_env_example(tmp_path: Path) -> None:
    root = tree(tmp_path, {".env.example": "VIGIL_API_TOKEN=realvalue\n"})
    assert len(found(root, "secrets")) == 1


def test_an_unparseable_source_file_is_a_violation_not_a_crash(tmp_path: Path) -> None:
    root = tree(tmp_path, {"src/vigil/broken.py": "def (:\n"})
    assert [v.gate for v in gates.run_gates(root)].count("syntax") >= 1


def test_size_gate(tmp_path: Path) -> None:
    root = tree(tmp_path, {"src/vigil/big.py": "x = 1\n" * (gates.MAX_LINES + 1)})
    assert len(found(root, "size")) == 1


# ------------------------------------------------------------------ architecture


def test_import_linter_contracts_hold() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "importlinter.cli", "--config", "pyproject.toml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if "No module named" in result.stderr:
        pytest.skip("import-linter has no module entry point here; checked by scripts/check.ps1")
    assert result.returncode == 0, result.stdout + result.stderr


def _runtime_graph() -> grimp.ImportGraph:
    return grimp.build_graph(
        "vigil", include_external_packages=False, exclude_type_checking_imports=True
    )


def _strongly_connected(graph: grimp.ImportGraph) -> list[list[str]]:
    """Tarjan's algorithm over the module import graph."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = [0]

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.find_modules_directly_imported_by(v):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            result.append(comp)

    sys.setrecursionlimit(10_000)
    for m in sorted(graph.modules):
        if m not in index:
            visit(m)
    return result


def test_there_are_no_runtime_import_cycles_between_modules() -> None:
    cycles = [sorted(c) for c in _strongly_connected(_runtime_graph()) if len(c) > 1]
    assert cycles == [], f"circular imports: {cycles}"


def test_cycle_detector_actually_detects_a_cycle() -> None:
    g = grimp.ImportGraph()
    for m in ("p", "p.a", "p.b"):
        g.add_module(m)
    g.add_import(importer="p.a", imported="p.b")
    g.add_import(importer="p.b", imported="p.a")
    assert [sorted(c) for c in _strongly_connected(g) if len(c) > 1] == [["p.a", "p.b"]]


def test_core_and_domain_import_nothing_from_higher_layers() -> None:
    g = _runtime_graph()
    higher = (
        "vigil.config",
        "vigil.observability",
        "vigil.ingest",
        "vigil.vision",
        "vigil.pipeline",
        "vigil.cli",
    )
    for pkg, banned in (("vigil.core", (*higher, "vigil.domain")), ("vigil.domain", higher)):
        for module in g.find_descendants(pkg):
            imported = g.find_modules_directly_imported_by(module)
            bad = [i for i in imported if any(i == b or i.startswith(b + ".") for b in banned)]
            assert not bad, f"{module} imports upward: {bad}"


INFERENCE_LIBRARIES = ("onnxruntime", "onnx", "torch", "tensorflow", "ultralytics")


def test_the_api_never_performs_inference() -> None:
    """The UI and API read results; they never run a model. No API module may import the vision
    engine or any inference runtime, directly or as an external package (08 §5 rule 2)."""
    g = grimp.build_graph(
        "vigil", include_external_packages=True, exclude_type_checking_imports=True
    )
    api_modules = list(g.find_descendants("vigil.api"))
    assert len(api_modules) >= 5  # the check must actually look at something
    forbidden = ("vigil.vision", *INFERENCE_LIBRARIES)
    for module in api_modules:
        for imported in g.find_modules_directly_imported_by(module):
            assert not any(imported == f or imported.startswith(f + ".") for f in forbidden), (
                f"{module} imports {imported}"
            )


def test_the_core_stays_headless() -> None:
    """Nothing outside the api package may import the web framework."""
    g = grimp.build_graph(
        "vigil", include_external_packages=True, exclude_type_checking_imports=True
    )
    web = ("fastapi", "uvicorn", "starlette")
    for module in g.modules:
        if module.startswith("vigil.api") or module in {"fastapi", "uvicorn", "starlette"}:
            continue
        for imported in g.find_modules_directly_imported_by(module):
            assert not any(imported == w or imported.startswith(w + ".") for w in web), (
                f"{module} imports the web framework: {imported}"
            )
