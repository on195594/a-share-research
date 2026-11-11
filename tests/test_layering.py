"""Only the explicit web dependency and pure-module import boundaries."""

import ast
from pathlib import Path
import subprocess
import sys

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "a_share_research"
PURE_MODULES = (
    "valuation", "stance", "forecasts", "agenda", "screening_factors",
    "batches", "stock_codes", "limits",
) + tuple(name for name in ("report", "contract") if (PACKAGE / f"{name}.py").exists())
FORBIDDEN = {"db", "sources", "market", "model", "service", "research", "screening", "web", "worker"}


def test_web_does_not_load_market_dependencies(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
import a_share_research.web
for name in ("pandas", "tushare", "akshare", "a_share_research.core.providers", "a_share_research.policy", "numpy"):
    assert name not in sys.modules, f"web loaded {name}"
"""],
        cwd=PACKAGE.parent,
        env={
            "DATA_DIR": str(tmp_path),
            "PUBLIC_BASE_URL": "http://127.0.0.1",
            "SESSION_SECRET": "layering-test-secret-32-characters",
            "DEV_LOGIN": "1",
        },
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_policy_page_read_loads_metadata_without_policy_runtime_or_market(tmp_path):
    result=subprocess.run([sys.executable,'-c','''
import sys
import a_share_research.web as web
from a_share_research.policy.presentation import owner_view
assert owner_view(web.conn)['status']=='inactive'
for name in ('pandas','numpy','tushare','akshare','a_share_research.policy.v0',
             'a_share_research.policy.evaluation','a_share_research.policy.capture',
             'a_share_research.core.providers'):
    assert name not in sys.modules, f'policy page loaded {name}'
'''],cwd=PACKAGE.parent,env={'DATA_DIR':str(tmp_path),'PUBLIC_BASE_URL':'http://127.0.0.1',
        'SESSION_SECRET':'layering-test-secret-32-characters','DEV_LOGIN':'1'},
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


def _module_imports(node):
    # Include conditional module imports, but not imports inside functions or classes.
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    if isinstance(node, ast.Import):
        yield from (alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if node.level:
            module = f"a_share_research.{module}".rstrip(".")
        yield module
        yield from (f"{module}.{alias.name}" for alias in node.names)
    for child in ast.iter_child_nodes(node):
        yield from _module_imports(child)


@pytest.mark.parametrize("module", PURE_MODULES)
def test_pure_module_imports(module):
    tree = ast.parse((PACKAGE / f"{module}.py").read_text())
    violations = {
        name for name in _module_imports(tree)
        if name.startswith("a_share_research.") and name.split(".")[1] in FORBIDDEN
    }
    assert not violations, f"{module}: forbidden imports {sorted(violations)}"
