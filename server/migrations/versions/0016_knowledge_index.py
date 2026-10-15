"""content schema: index build, parent chunk, search chunk

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-16

DEV-09 收尾迁移包：把模块06 §2/§8 的索引构建聚合与父子块确定性持久化到 `content` Schema
（模块07 §4 明确以正式 `content.search_chunk` 为检索读表）。索引是派生数据、可重建；构建中
`is_active=false` 不污染线上检索（模块06 §9）。

本迁移只落地确定性列；`embedding vector(1024)`（pgvector HNSW，BGE-M3 权重未评测）与
`lexical_vector`（tsvector GIN，中文分词器未冻结）是「召回/embedding 切片」的投影列，本迁移不建，
与 DEV-08 memory 的「Embedding Projection（pgvector）留待召回切片」同口径；`knowledge_version_id`
在知识版本表（模块04）落地前保持裸 UUID 延迟外键，不伪造关系闭包。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_VALUES = (
    "'CREATED','CHUNKING','EMBEDDING','WRITING','VALIDATING',"
    "'READY','ACTIVE','RETIRED','FAILED','CANCELLED'"
)


def upgrade() -> None:
    op.create_table(
        "index_build",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("knowledge_version_id", sa.UUID(), nullable=False),
        sa.Column("canonical_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("chunker_version", sa.String(length=64), nullable=False),
        sa.Column("embedding_revision", sa.String(length=64), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stats_json", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("dimension >= 1", name=op.f("ck_index_build_dimension_positive")),
        sa.CheckConstraint(
            "canonical_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_index_build_canonical_hash_lower_hex"),
        ),
        sa.CheckConstraint(
            "config_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_index_build_config_hash_lower_hex"),
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUS_VALUES})", name=op.f("ck_index_build_status_allowed")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_build")),
        schema="content",
    )

    op.create_table(
        "parent_chunk",
        sa.Column("id", sa.CHAR(length=64), nullable=False),
        sa.Column("index_version_id", sa.UUID(), nullable=False),
        sa.Column("structure_path_json", JSONB(), nullable=False),
        sa.Column("page_from", sa.Integer(), nullable=False),
        sa.Column("page_to", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id ~ '^[0-9a-f]{64}$'", name=op.f("ck_parent_chunk_id_lower_hex")),
        sa.CheckConstraint("page_from >= 1", name=op.f("ck_parent_chunk_page_from_positive")),
        sa.CheckConstraint("page_to >= 1", name=op.f("ck_parent_chunk_page_to_positive")),
        sa.CheckConstraint("page_to >= page_from", name=op.f("ck_parent_chunk_page_range_valid")),
        sa.CheckConstraint("token_count >= 0", name=op.f("ck_parent_chunk_token_count_nonnegative")),
        sa.ForeignKeyConstraint(
            ["index_version_id"],
            ["content.index_build.id"],
            name=op.f("fk_parent_chunk_index_version_id_index_build"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parent_chunk")),
        schema="content",
    )

    op.create_table(
        "search_chunk",
        sa.Column("id", sa.CHAR(length=64), nullable=False),
        sa.Column("index_version_id", sa.UUID(), nullable=False),
        sa.Column("knowledge_version_id", sa.UUID(), nullable=False),
        sa.Column("parent_id", sa.CHAR(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("page_from", sa.Integer(), nullable=False),
        sa.Column("page_to", sa.Integer(), nullable=False),
        sa.Column("block_refs_json", JSONB(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("text_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id ~ '^[0-9a-f]{64}$'", name=op.f("ck_search_chunk_id_lower_hex")),
        sa.CheckConstraint(
            "text_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_search_chunk_text_hash_lower_hex")
        ),
        sa.CheckConstraint("page_from >= 1", name=op.f("ck_search_chunk_page_from_positive")),
        sa.CheckConstraint("page_to >= 1", name=op.f("ck_search_chunk_page_to_positive")),
        sa.CheckConstraint("page_to >= page_from", name=op.f("ck_search_chunk_page_range_valid")),
        sa.CheckConstraint("token_count >= 0", name=op.f("ck_search_chunk_token_count_nonnegative")),
        sa.ForeignKeyConstraint(
            ["index_version_id"],
            ["content.index_build.id"],
            name=op.f("fk_search_chunk_index_version_id_index_build"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["content.parent_chunk.id"],
            name=op.f("fk_search_chunk_parent_id_parent_chunk"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_chunk")),
        schema="content",
    )
    op.create_index(
        "ix_search_chunk_knowledge_version_active",
        "search_chunk",
        ["knowledge_version_id", "is_active"],
        unique=False,
        schema="content",
    )
    op.create_index(
        "ix_search_chunk_index_version_parent",
        "search_chunk",
        ["index_version_id", "parent_id"],
        unique=False,
        schema="content",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM content.search_chunk) THEN "
            "RAISE EXCEPTION 'SEARCH_CHUNK_DOWNGRADE_REQUIRES_RECONCILIATION'; "
            "END IF; END $$"
        )
    )
    op.drop_index("ix_search_chunk_knowledge_version_active", table_name="search_chunk", schema="content")
    op.drop_index("ix_search_chunk_index_version_parent", table_name="search_chunk", schema="content")
    op.drop_table("search_chunk", schema="content")
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM content.parent_chunk) THEN "
            "RAISE EXCEPTION 'PARENT_CHUNK_DOWNGRADE_REQUIRES_RECONCILIATION'; "
            "END IF; END $$"
        )
    )
    op.drop_table("parent_chunk", schema="content")
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM content.index_build) THEN "
            "RAISE EXCEPTION 'INDEX_BUILD_DOWNGRADE_REQUIRES_RECONCILIATION'; "
            "END IF; END $$"
        )
    )
    op.drop_table("index_build", schema="content")
