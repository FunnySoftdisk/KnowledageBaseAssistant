"""`memory` Schema 权威 Store 的 SQLAlchemy 适配器（实现 `MemoryRepositoryPort`）。

显式 kwargs 互转（无 `model_validate`），不提交事务、不开启事务（由上层 UoW/服务提交）。
`commit_candidate` 从 `iam.user_account` 解析 `organization_id`（域契约只携带 `owner_id`，物理表
按 ADR-0004§4 存 `organization_id + owner_id`）；查询按 `owner_id + scope` 过滤（`user_account.id`
全局唯一，等价于并集隔离键）。`revise` 追加新 Revision 并重算 `canonical_hash`；`forget` 先落
Tombstone 再把记录置 `RETRACTED`。Embedding/pgvector 与近似去重留待召回切片。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.memory.application.ports import (
    ForgetReceiptV1,
    GovernedMemoryCandidateV1,
    MemoryForgetCommandV1,
    MemoryPinCommandV1,
    MemoryRevisionCommandV1,
)
from knowledge_system.modules.memory.domain.memory_governance import (
    compute_canonical_hash,
    normalize_memory_statement,
)
from knowledge_system.modules.memory.domain.memory_types import (
    MemoryKindV1,
    MemoryRecordV1,
    MemoryScopeV1,
    MemoryStateV1,
    MemoryTrustV1,
    ProvenanceRefV1,
    ProvenanceSourceTypeV1,
)

from .foundation_models import UserAccountRecord
from .memory_models import MemoryForgetTombstoneRecord, MemoryRecordRecord


class MemoryRecordIntegrityError(RuntimeError):
    """已持久化列违反记忆不变式，或引用主体不存在，无法完成适配。"""


def _provenance_to_json(refs: tuple[ProvenanceRefV1, ...]) -> list[dict[str, object]]:
    return [
        {
            "source_type": ref.source_type.value,
            "source_id": ref.source_id,
            "source_version": ref.source_version,
            "content_hash": ref.content_hash,
            "message_sequence": ref.message_sequence,
        }
        for ref in refs
    ]


def _provenance_from_json(value: object) -> tuple[ProvenanceRefV1, ...]:
    if not isinstance(value, list):
        raise MemoryRecordIntegrityError("MEMORY_PROVENANCE_NOT_ARRAY")
    refs: list[ProvenanceRefV1] = []
    for item in value:
        if not isinstance(item, dict):
            raise MemoryRecordIntegrityError("MEMORY_PROVENANCE_ITEM_NOT_OBJECT")
        source_version = item.get("source_version")
        message_sequence = item.get("message_sequence")
        refs.append(
            ProvenanceRefV1(
                source_type=ProvenanceSourceTypeV1(str(item["source_type"])),
                source_id=str(item["source_id"]),
                source_version=None if source_version is None else str(source_version),
                content_hash=str(item["content_hash"]),
                message_sequence=None if message_sequence is None else int(message_sequence),
            )
        )
    return tuple(refs)


def record_from_candidate(
    candidate: GovernedMemoryCandidateV1, *, organization_id: UUID, record_id: UUID
) -> MemoryRecordRecord:
    """写入 Gate 裁决后的候选 → ORM 行；`id`/`schema_version`/`revision` 由 Repository 生成。"""

    return MemoryRecordRecord(
        id=record_id,
        organization_id=organization_id,
        owner_id=candidate.owner_id,
        conversation_id=candidate.conversation_id,
        task_id=candidate.task_id,
        scope=candidate.scope.value,
        kind=candidate.kind.value,
        state=candidate.state.value,
        trust=candidate.trust.value,
        canonical_key=candidate.canonical_key,
        content=candidate.content,
        canonical_hash=candidate.canonical_hash,
        importance=candidate.importance,
        pinned=False,
        valid_from=candidate.valid_from,
        valid_until=candidate.valid_until,
        supersedes_id=candidate.supersedes_id,
        conflict_group_id=candidate.conflict_group_id,
        provenance_json=_provenance_to_json(candidate.provenance),
        schema_version=1,
        revision=1,
    )


def memory_from_record(record: MemoryRecordRecord) -> MemoryRecordV1:
    """ORM 行 → 领域记录（显式 kwargs，无 `model_validate`）。"""

    return MemoryRecordV1(
        id=record.id,
        owner_id=record.owner_id,
        conversation_id=record.conversation_id,
        task_id=record.task_id,
        scope=MemoryScopeV1(record.scope),
        kind=MemoryKindV1(record.kind),
        state=MemoryStateV1(record.state),
        trust=MemoryTrustV1(record.trust),
        canonical_key=record.canonical_key,
        content=record.content,
        canonical_hash=record.canonical_hash,
        importance=record.importance,
        pinned=record.pinned,
        valid_from=record.valid_from,
        valid_until=record.valid_until,
        supersedes_id=record.supersedes_id,
        conflict_group_id=record.conflict_group_id,
        provenance=_provenance_from_json(record.provenance_json),
        schema_version=record.schema_version,
        revision=record.revision,
    )


class SqlAlchemyMemoryRepository:
    """`memory` Schema 权威 Store 写侧实现；持有同 Session，不提交、不开启事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _organization_id_for(self, owner_id: UUID) -> UUID:
        account = await self._session.get(UserAccountRecord, owner_id)
        if account is None:
            raise MemoryRecordIntegrityError("OWNER_ACCOUNT_NOT_FOUND")
        return account.organization_id

    async def _get_record(self, record_id: UUID) -> MemoryRecordRecord:
        record = await self._session.get(MemoryRecordRecord, record_id)
        if record is None:
            raise MemoryRecordIntegrityError("MEMORY_RECORD_NOT_FOUND")
        return record

    async def find_active_by_hash(
        self, *, owner_id: UUID, scope: MemoryScopeV1, canonical_hash: str
    ) -> MemoryRecordV1 | None:
        statement: Select[tuple[MemoryRecordRecord]] = select(MemoryRecordRecord).where(
            MemoryRecordRecord.owner_id == owner_id,
            MemoryRecordRecord.scope == scope.value,
            MemoryRecordRecord.state == MemoryStateV1.ACTIVE.value,
            MemoryRecordRecord.canonical_hash == canonical_hash,
        )
        record = (await self._session.scalars(statement)).first()
        return None if record is None else memory_from_record(record)

    async def find_active_by_key(
        self, *, owner_id: UUID, scope: MemoryScopeV1, canonical_key: str
    ) -> MemoryRecordV1 | None:
        statement: Select[tuple[MemoryRecordRecord]] = select(MemoryRecordRecord).where(
            MemoryRecordRecord.owner_id == owner_id,
            MemoryRecordRecord.scope == scope.value,
            MemoryRecordRecord.state == MemoryStateV1.ACTIVE.value,
            MemoryRecordRecord.canonical_key == canonical_key,
        )
        record = (await self._session.scalars(statement)).first()
        return None if record is None else memory_from_record(record)

    async def find_recent_active(
        self, *, owner_id: UUID, scope: MemoryScopeV1, limit: int
    ) -> tuple[MemoryRecordV1, ...]:
        """最近 Active Memory，按 importance/valid_from/id 确定性降序取前 `limit` 条（§9.2 通道5）。

        排序与 `MemoryRecallService` 的确定性排序一致，保证 `limit` 截断选择与最终召回排序自洽。
        """

        statement: Select[tuple[MemoryRecordRecord]] = (
            select(MemoryRecordRecord)
            .where(
                MemoryRecordRecord.owner_id == owner_id,
                MemoryRecordRecord.scope == scope.value,
                MemoryRecordRecord.state == MemoryStateV1.ACTIVE.value,
            )
            .order_by(
                MemoryRecordRecord.importance.desc(),
                MemoryRecordRecord.valid_from.desc(),
                MemoryRecordRecord.id.asc(),
            )
            .limit(limit)
        )
        return tuple(
            memory_from_record(record)
            for record in (await self._session.scalars(statement)).all()
        )

    async def find_pinned(
        self, *, owner_id: UUID, scope: MemoryScopeV1, limit: int
    ) -> tuple[MemoryRecordV1, ...]:
        """被用户 Pin 的 Active Memory（§9.2 通道2），确定性降序取前 `limit` 条。"""

        statement: Select[tuple[MemoryRecordRecord]] = (
            select(MemoryRecordRecord)
            .where(
                MemoryRecordRecord.owner_id == owner_id,
                MemoryRecordRecord.scope == scope.value,
                MemoryRecordRecord.state == MemoryStateV1.ACTIVE.value,
                MemoryRecordRecord.pinned.is_(True),
            )
            .order_by(
                MemoryRecordRecord.importance.desc(),
                MemoryRecordRecord.valid_from.desc(),
                MemoryRecordRecord.id.asc(),
            )
            .limit(limit)
        )
        return tuple(
            memory_from_record(record)
            for record in (await self._session.scalars(statement)).all()
        )

    async def find_active_by_conflict_group(
        self, *, owner_id: UUID, conflict_group_id: UUID
    ) -> tuple[MemoryRecordV1, ...]:
        """冲突组内全部 ACTIVE 记录（§9.2 冲突隔离），按 id 升序确定性返回。"""

        statement: Select[tuple[MemoryRecordRecord]] = (
            select(MemoryRecordRecord)
            .where(
                MemoryRecordRecord.owner_id == owner_id,
                MemoryRecordRecord.conflict_group_id == conflict_group_id,
                MemoryRecordRecord.state == MemoryStateV1.ACTIVE.value,
            )
            .order_by(MemoryRecordRecord.id.asc())
        )
        return tuple(
            memory_from_record(record)
            for record in (await self._session.scalars(statement)).all()
        )

    async def commit_candidate(self, candidate: GovernedMemoryCandidateV1) -> MemoryRecordV1:
        organization_id = await self._organization_id_for(candidate.owner_id)
        record = record_from_candidate(
            candidate, organization_id=organization_id, record_id=uuid4()
        )
        self._session.add(record)
        await self._session.flush()
        return memory_from_record(record)

    async def append_provenance(
        self, record_id: UUID, provenance: tuple[ProvenanceRefV1, ...]
    ) -> MemoryRecordV1:
        record = await self._get_record(record_id)
        record.provenance_json = record.provenance_json + _provenance_to_json(provenance)
        return memory_from_record(record)

    async def revise(self, command: MemoryRevisionCommandV1) -> MemoryRecordV1:
        existing = await self._get_record(command.record_id)
        existing.state = MemoryStateV1.SUPERSEDED.value
        revised = MemoryRecordRecord(
            id=uuid4(),
            organization_id=existing.organization_id,
            owner_id=existing.owner_id,
            conversation_id=existing.conversation_id,
            task_id=existing.task_id,
            scope=existing.scope,
            kind=existing.kind,
            state=MemoryStateV1.ACTIVE.value,
            trust=command.trust.value,
            canonical_key=existing.canonical_key,
            content=command.corrected_content,
            canonical_hash=compute_canonical_hash(
                MemoryScopeV1(existing.scope),
                MemoryKindV1(existing.kind),
                existing.canonical_key,
                normalize_memory_statement(command.corrected_content),
            ),
            importance=existing.importance,
            pinned=existing.pinned,
            valid_from=datetime.now(UTC),
            valid_until=existing.valid_until,
            supersedes_id=existing.id,
            conflict_group_id=existing.conflict_group_id,
            provenance_json=_provenance_to_json(command.provenance),
            schema_version=existing.schema_version,
            revision=existing.revision + 1,
        )
        self._session.add(revised)
        return memory_from_record(revised)

    async def forget(self, command: MemoryForgetCommandV1) -> ForgetReceiptV1:
        record = await self._get_record(command.record_id)
        now = datetime.now(UTC)
        tombstone_id = uuid4()
        record.state = MemoryStateV1.RETRACTED.value
        self._session.add(
            MemoryForgetTombstoneRecord(
                tombstone_id=tombstone_id,
                record_id=record.id,
                organization_id=record.organization_id,
                owner_id=record.owner_id,
                reason=command.reason,
                forgotten_at=now,
            )
        )
        return ForgetReceiptV1(
            record_id=record.id, tombstone_id=tombstone_id, forgotten_at=now
        )

    async def pin(self, command: MemoryPinCommandV1) -> MemoryRecordV1:
        """切换 `pinned` 标志（§6 Pin/Unpin）；不校验 state，召回查询按 ACTIVE 过滤。"""

        record = await self._get_record(command.record_id)
        record.pinned = command.pinned
        return memory_from_record(record)


__all__ = [
    "MemoryRecordIntegrityError",
    "SqlAlchemyMemoryRepository",
    "memory_from_record",
    "record_from_candidate",
]
