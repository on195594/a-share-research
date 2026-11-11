"""Keep TuShare credentials and transport behind the market gateway."""

import ast
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1] / "a_share_research"


def test_market_gateway_boundary():
    forbidden = {"a_share_research.screening._pro", "a_share_research.screening._trade_days"}
    for path in sorted(PACKAGE.glob("*.py")):
        source = path.read_text()
        if path.name != "market.py":
            assert "pro_api(" not in source, path.name
            assert "TUSHARE_TOKEN" not in source, path.name

        tree = ast.parse(source)
        imports = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
                    _check_dependency(path, alias.name)
            elif isinstance(node, ast.ImportFrom):
                # The repository requires absolute package imports.
                if path.name in {"market.py", "sources.py"}:
                    assert node.level == 0, (path.name, node.lineno)
                module = node.module or ""
                for alias in node.names:
                    target = f"{module}.{alias.name}"
                    imports[alias.asname or alias.name] = target
                    _check_dependency(path, target)
                    if path.name != "screening.py":
                        assert target not in forbidden, (path.name, target)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            parts = []
            root = node
            while isinstance(root, ast.Attribute):
                parts.append(root.attr)
                root = root.value
            if isinstance(root, ast.Name):
                target = ".".join([imports.get(root.id, root.id), *reversed(parts)])
                if path.name != "screening.py":
                    assert target not in forbidden, (path.name, target)


def _check_dependency(path: Path, target: str) -> None:
    if not target.startswith("a_share_research."):
        return
    dependency = target.split(".")[1]
    if path.name == "market.py":
        assert dependency in {"db", "companies", "core"}, target  # core is the merged provider library
    if path.name == "sources.py":
        assert dependency != "screening", target
