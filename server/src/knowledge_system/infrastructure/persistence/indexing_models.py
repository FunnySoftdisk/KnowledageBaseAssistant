"""`content.index_build` / `content.parent_chunk` / `content.search_chunk` 权威 Store 映射。

DEV-09 收尾：把模块06 §2/§8 的索引构建聚合与父子块确定性持久化到 `content` Schema（模块07 §4
明确以正式 `content.search_chunk` 为检索读表）。索引是派生数据、可重建；构建中 `is_active=false`
不污染线上检索，发布事务把旧版本置 false、新版本置 true 并更新活动指针（模块06 §9）。

本切片只冻结确定性列（chunk 身份、text/normalized_text、结构路径/BlockRefs、页面区间、token、
text_hash、is_active、构建状态）。`embedding vector(1024)`（pgvector HNSW，BGE-M3 权重未评测）与
`lexical_vector`（tsvector GIN，中文分词器未冻结）是「召回/embedding 切片」的投影列，本切片不建，
与 DEV-08 memory 的「Embedding Projection（pgvector）留待召回切片」同口径；`knowledge_version_id`
在知识版本表（模块04）落地前保持裸 UUID 延迟外键，不伪造关系闭包。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_STATUS_VALUES = (
    "'CREATED','CHUNKING','EMBEDDING','WRITING','VALIDATING',"
    "'READY','ACTIVE','RETIRED','FAILED','CANCELLED'"
)


class IndexBuildRecord(Base):
    """模块06 §2 `IndexBuild`：一次索引构建/版本聚合根。"""

    __tablename__ = "index_build"
    __table_args__ = (
        CheckConstraint("dimension >= 1", name="dimension_positive"),
        CheckConstraint("canonical_hash ~ '^[0-9a-f]{64}$'", name="canonical_hash_lower_hex"),
        CheckConstraint("config_hash ~ '^[0-9a-f]{64}$'", name="config_hash_lower_hex"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_allowed"),
        {"schema": "content"},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # 知识版本表（模块04）落地前保持裸 UUID，见 deferred_foreign_keys。
    knowledge_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    canonical_hash: Mapped[str] = mapped_column(CHAR(64))
    chunker_version: Mapped[str] = mapped_column(String(64))
    embedding_revision: Mapped[str] = mapped_column(String(64))
    dimension: Mapped[int] = mapped_column(Integer)
    config_hash: Mapped[str] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(16))
    stats_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ParentChunkRecord(Base):
    """模块06 §2 `ParentChunk`：父块（结构路径 + 原文 + token），内容寻址稳定 ID。"""

    __tablename__ = "parent_chunk"
    __table_args__ = (
        CheckConstraint("id ~ '^[0-9a-f]{64}$'", name="id_lower_hex"),
        CheckConstraint("page_from >= 1", name="page_from_positive"),
        CheckConstraint("page_to >= 1", name="page_to_positive"),
        CheckConstraint("page_to >= page_from", name="page_range_valid"),
        CheckConstraint("token_count >= 0", name="token_count_nonnegative"),
        {"schema": "content"},
    )

    id: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    index_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.index_build.id", ondelete="RESTRICT")
    )
    structure_path_json: Mapped[list[object]] = mapped_column(JSONB)
    page_from: Mapped[int] = mapped_column(Integer)
    page_to: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SearchChunkRecord(Base):
    """模块06 §2/§8 `SearchChunk`：子块（检索读侧权威表，模块07 §4 `content.search_chunk`）。"""

    __tablename__ = "search_chunk"
    __table_args__ = (
        CheckConstraint("id ~ '^[0-9a-f]{64}$'", name="id_lower_hex"),
        CheckConstraint("text_hash ~ '^[0-9a-f]{64}$'", name="text_hash_lower_hex"),
        CheckConstraint("page_from >= 1", name="page_from_positive"),
        CheckConstraint("page_to >= 1", name="page_to_positive"),
        CheckConstraint("page_to >= page_from", name="page_range_valid"),
        CheckConstraint("token_count >= 0", name="token_count_nonnegative"),
        Index("ix_search_chunk_knowledge_version_active", "knowledge_version_id", "is_active"),
        Index("ix_search_chunk_index_version_parent", "index_version_id", "parent_id"),
        {"schema": "content"},
    )

    id: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    index_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content.index_build.id", ondelete="RESTRICT")
    )
    # 知识版本表（模块04）落地前保持裸 UUID，见 deferred_foreign_keys（§8 检索谓词反规范化列）。
    knowledge_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    parent_id: Mapped[str] = mapped_column(
        CHAR(64), ForeignKey("content.parent_chunk.id", ondelete="RESTRICT")
    )
    text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    # LanguageProfile（模块06 §3.3）尚未冻结，由调用方供给语言标签，本切片不设 CHECK。
    language: Mapped[str] = mapped_column(String(16))
    page_from: Mapped[int] = mapped_column(Integer)
    page_to: Mapped[int] = mapped_column(Integer)
    block_refs_json: Mapped[list[object]] = mapped_column(JSONB)
    token_count: Mapped[int] = mapped_column(Integer)
    text_hash: Mapped[str] = mapped_column(CHAR(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
