"""Static checks that catch mistakes py_compile can't, without importing anything.

Shadowing: a function parameter with the same name as a module the file imports,
used inside that function as if it were still the module (lab.lab_runs(...) when
lab is a bool). That only fails at runtime, on the first request that reaches it.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted(p for d in ("demo", "dev") for p in (ROOT / d).rglob("*.py"))


def _imported_names(tree: ast.Module) -> set:
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {a.asname or a.name for a in node.names}
    return names


def _shadowed_imports(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = _imported_names(tree)
    problems = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = fn.args
        params = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs} | {x.arg for x in (a.vararg, a.kwarg) if x}
        for name in sorted(params & imported):
            used_as_module = any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == name
                                 for n in ast.walk(fn))
            if used_as_module:
                problems.append(f"{path.relative_to(ROOT)}:{fn.lineno} {fn.name}() parameter '{name}' hides the imported '{name}'")
    return problems


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_parameter_hides_an_imported_module(path):
    assert _shadowed_imports(path) == []


def test_the_check_catches_the_lab_bug(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("from . import lab\n\nasync def snapshot(selected, lab=False):\n    return lab.lab_runs({})\n")
    original = ROOT
    try:
        globals()["ROOT"] = tmp_path
        assert _shadowed_imports(bad) == ["bad.py:3 snapshot() parameter 'lab' hides the imported 'lab'"]
    finally:
        globals()["ROOT"] = original
