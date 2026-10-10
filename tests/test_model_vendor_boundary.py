import ast
from pathlib import Path

import tangible

VENDORS = {"boto3", "botocore", "anthropic", "openai"}
ALLOWED = Path("agent") / "llm.py"


def _imported(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_only_llm_imports_a_model_sdk() -> None:
    root = Path(tangible.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel == ALLOWED:
            continue
        if _imported(ast.parse(path.read_text())) & VENDORS:
            offenders.append(str(rel))
    assert not offenders, f"model SDK imported outside agent/llm.py: {offenders}"
