"""계층 의존 방향 검사 (PRD 7.2, NFR-6.1, CLAUDE.md R-5).

각 모듈의 import를 읽어 PRD 7.2의 참조 가능 표를 벗어나는 참조가 없는지 확인한다.
"""

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "coindata"

ALLOWED: dict[str, set[str]] = {
    "entry": {"cli"},
    "cli": {"cli", "report", "compute", "store", "ingest", "config", "models"},
    "report": {"report", "compute", "store", "config", "models"},
    "compute": {"compute", "store", "config", "models"},
    "store": {"store", "config", "models"},
    "ingest": {"ingest", "config", "models"},
    "config": set(),
    "models": set(),
}


def layer_of(module_path: Path) -> str:
    parts = module_path.relative_to(PACKAGE).with_suffix("").parts
    if parts[0] in ("__init__", "__main__"):
        return "entry"
    return parts[0]


def imported_layers(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return {name.split(".")[1] for name in names if name.startswith("coindata.")}


class LayerTest(unittest.TestCase):
    def test_no_forbidden_references(self) -> None:
        violations = []
        for path in sorted(PACKAGE.rglob("*.py")):
            layer = layer_of(path)
            for target in imported_layers(path) - ALLOWED[layer]:
                violations.append(f"{path.relative_to(PACKAGE)} ({layer}) → {target}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
