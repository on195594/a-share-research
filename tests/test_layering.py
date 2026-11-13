"""Web, pure-module, and policy → account → core import boundaries."""

import ast
from importlib.util import resolve_name
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
LAYER_FORBIDDEN = {
    "core": {
        "account", "policy", "tools", "research", "research_tools", "material",
        "evidence", "db", "web", "worker", "screening", "external_operations",
        "external_acquisition", "acquisition_process", "external_research", "external_review",
    },
    "account": {
        "policy", "tools", "research", "research_tools", "screening", "web", "worker",
        "external_operations", "external_acquisition", "acquisition_process",
    },
    "policy": {
        "tools", "web", "worker", "screening", "external_operations",
        "external_acquisition", "acquisition_process", "external_research", "external_review",
    },
}


@pytest.mark.parametrize("path", [
    path for layer in LAYER_FORBIDDEN for path in sorted((PACKAGE / layer).rglob("*.py"))
], ids=lambda path: str(path.relative_to(PACKAGE)))
def test_layer_import_direction(path):
    relative = path.relative_to(PACKAGE)
    forbidden = LAYER_FORBIDDEN[relative.parts[0]]
    package = ".".join((PACKAGE.name, *relative.parts[:-1]))
    violations = []
    # Walk every node, including lazy imports inside functions and classes.
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        else:
            continue
        for name in names:
            if name.startswith("a_share_research.") and name.split(".")[1] in forbidden:
                violations.append(f"{node.lineno}: {name}")
    assert not violations, f"{relative}: forbidden imports {violations}"


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
