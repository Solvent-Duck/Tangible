import ast
from pathlib import Path

import tangible

MODEL_SDKS = {"anthropic", "openai"}
AWS_SDKS = {"boto3", "botocore"}
LLM = Path("agent") / "llm.py"
# boto3 is allowed only where an AWS service is the point of the module. Bedrock clients are
# created only in agent/llm.py (checked below by looking for the service name).
AWS_ALLOWED = {LLM, Path("evidence") / "s3.py", Path("secrets") / "aws.py"}
# The agent framework is confined to one package; its model calls still go through llm.py.
STRANDS_PKG = Path("agent") / "strands_engine"


def _imported(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _modules() -> list[tuple[Path, str]]:
    root = Path(tangible.__file__).parent
    return [(p.relative_to(root), p.read_text()) for p in root.rglob("*.py")]


def test_only_llm_imports_a_model_sdk() -> None:
    offenders = [
        str(rel) for rel, src in _modules() if rel != LLM and _imported(ast.parse(src)) & MODEL_SDKS
    ]
    assert not offenders, f"model SDK imported outside agent/llm.py: {offenders}"


def test_aws_sdk_only_in_allowed_modules() -> None:
    offenders = [
        str(rel)
        for rel, src in _modules()
        if rel not in AWS_ALLOWED and _imported(ast.parse(src)) & AWS_SDKS
    ]
    assert not offenders, f"boto3/botocore imported outside {sorted(map(str, AWS_ALLOWED))}"


def test_bedrock_only_in_llm() -> None:
    offenders = [str(rel) for rel, src in _modules() if rel != LLM and "bedrock" in src.lower()]
    assert not offenders, f"Bedrock referenced outside agent/llm.py: {offenders}"


def test_strands_only_in_the_engine_package() -> None:
    offenders = [
        str(rel)
        for rel, src in _modules()
        if STRANDS_PKG not in rel.parents and "strands" in _imported(ast.parse(src))
    ]
    assert not offenders, f"strands imported outside {STRANDS_PKG}: {offenders}"


def test_strands_engine_uses_no_vendor_model_provider() -> None:
    # Strands ships Bedrock/Anthropic/OpenAI providers; we use only our ModelClient adapter.
    for rel, src in _modules():
        if STRANDS_PKG in rel.parents:
            mods = {
                n.module
                for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.ImportFrom) and n.module
            }
            assert not {m for m in mods if m.startswith("strands.models.")}, rel
