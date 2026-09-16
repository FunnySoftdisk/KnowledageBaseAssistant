"""DEV-01A基础ORM与已批准Psycopg引擎探针。"""

import unittest

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from knowledge_system.infrastructure.persistence import (
    Base,
    DatabaseEngineSettings,
    build_async_engine,
    build_session_factory,
    foundation_models,  # noqa: F401
)


class PersistenceFoundationTests(unittest.TestCase):
    def test_only_approved_psycopg_url_is_accepted(self) -> None:
        settings = DatabaseEngineSettings(
            "postgresql+psycopg://knowledge:secret@127.0.0.1:5432/knowledge"
        )
        engine = build_async_engine(settings)
        try:
            self.assertEqual(engine.url.drivername, "postgresql+psycopg")
            self.assertEqual(engine.pool.size(), 5)
            factory = build_session_factory(engine)
            self.assertFalse(factory.kw["expire_on_commit"])
            self.assertFalse(factory.kw["autoflush"])
        finally:
            engine.sync_engine.dispose()

        for url in (
            "postgresql+asyncpg://knowledge:secret@localhost/knowledge",
            "sqlite+aiosqlite:///knowledge.db",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(
                ValueError, "DATABASE_DRIVER_NOT_APPROVED"
            ):
                DatabaseEngineSettings(url)

    def test_foundation_metadata_and_postgresql_ddl_compile(self) -> None:
        expected = {
            "iam.organization",
            "iam.user_account",
            "workflow.conversation",
            "content.artifact",
            "policy.policy_snapshot",
        }
        self.assertTrue(expected.issubset(Base.metadata.tables))
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
            self.assertIn("CREATE TABLE", ddl)
            self.assertIn(table.name, ddl)

        artifact = Base.metadata.tables["content.artifact"]
        self.assertNotIn("original_name", artifact.c)
        self.assertIn("schema_digest", artifact.c)
        self.assertTrue(artifact.c.owner_id.nullable)
        self.assertTrue(artifact.c.origin_upload_id.nullable)

    def test_pool_settings_fail_closed(self) -> None:
        url = "postgresql+psycopg://knowledge:secret@localhost/knowledge"
        for values in (
            {"pool_size": 0},
            {"max_overflow": -1},
            {"pool_timeout_seconds": 0},
            {"pool_recycle_seconds": 0},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                DatabaseEngineSettings(url, **values)


if __name__ == "__main__":
    unittest.main()
