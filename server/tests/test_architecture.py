"""首批静态导入边界；不冒充跨表事务/完整依赖图检查。"""

import ast
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src/knowledge_system"
FORBIDDEN_PROJECT_FRAMEWORKS = {"langgraph", "langchain", "pydantic_graph", "pydantic_ai_harness"}
FORBIDDEN_DOMAIN_DEPENDENCIES = {
    "fastapi",
    "sqlalchemy",
    "temporalio",
    "pydantic_ai",
    "httpx",
    "celery",
    "kombu",
}


def imported_roots(source: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


class ArchitectureTests(unittest.TestCase):
    def test_project_does_not_adopt_disallowed_or_sdk_internal_orchestration(self) -> None:
        sources = list(SOURCE_ROOT.rglob("*.py"))
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(path=source.relative_to(SOURCE_ROOT)):
                self.assertFalse(imported_roots(source) & FORBIDDEN_PROJECT_FRAMEWORKS)

    def test_domain_has_no_web_orm_model_or_workflow_sdk(self) -> None:
        sources = list(SOURCE_ROOT.glob("modules/*/domain/*.py"))
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(path=source.relative_to(SOURCE_ROOT)):
                self.assertFalse(imported_roots(source) & FORBIDDEN_DOMAIN_DEPENDENCIES)


if __name__ == "__main__":
    unittest.main()
