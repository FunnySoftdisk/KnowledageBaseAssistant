"""`workflow.task_observation` / `workflow.observation_disposition` 不可变观察投影适配器。

Observation 按 `observation_id` 幂等插入（`ON CONFLICT DO NOTHING`），Disposition 按
`disposition_event_id` 幂等追加；两者创建后均不更新。本模块不提交事务，由上层 UoW/服务在合适
边界提交。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.tasking.domain.observation import (
    OBSERVATION_PAYLOAD_ADAPTER,
    ObservationClassV1,
    ObservationDispositionEventV1,
    ObservationDispositionV1,
    ObservationSourceKindV1,
    TaskObservationV1,
)

from .observation_models import ObservationDispositionRecord, TaskObservationRecord


class ObservationRecordIntegrityError(RuntimeError):
    """已持久化列违反观察不变式，无法还原为读模型。"""


@dataclass(frozen=True, slots=True)
class ObservationInsertResult:
    created: bool
    observation_id: UUID


@dataclass(frozen=True, slots=True)
class DispositionAppendResult:
    created: bool
    disposition_event_id: UUID


def record_from_observation(observation: TaskObservationV1) -> TaskObservationRecord:
    return TaskObservationRecord(
        observation_id=observation.observation_id,
        task_id=observation.task_id,
        source_event_id=observation.source_event_id,
        source_event_sequence=observation.source_event_sequence,
        source_ordinal=observation.source_ordinal,
        source_kind=observation.source_kind.value,
        observation_class=observation.observation_class.value,
        source_plan_version=observation.source_plan_version,
        contract_version=observation.contract_version,
        created_at=observation.created_at,
        payload_json=observation.payload.model_dump(mode="json"),
        integrity_digest=observation.integrity_digest,
    )


def observation_from_record(record: TaskObservationRecord) -> TaskObservationV1:
    """ORM 行 → 不可变观察（顶层显式 kwargs，payload 由领域判别联合 TypeAdapter 重建）。"""

    payload = OBSERVATION_PAYLOAD_ADAPTER.validate_json(json.dumps(record.payload_json))
    return TaskObservationV1(
        contract_version=cast(Literal["task_observation_v1"], record.contract_version),
        observation_id=record.observation_id,
        task_id=record.task_id,
        source_event_id=record.source_event_id,
        source_event_sequence=record.source_event_sequence,
        source_ordinal=record.source_ordinal,
        source_kind=ObservationSourceKindV1(record.source_kind),
        observation_class=ObservationClassV1(record.observation_class),
        source_plan_version=record.source_plan_version,
        created_at=record.created_at,
        payload=payload,
        integrity_digest=record.integrity_digest,
    )


def disposition_record_from_event(
    event: ObservationDispositionEventV1,
) -> ObservationDispositionRecord:
    return ObservationDispositionRecord(
        disposition_event_id=event.disposition_event_id,
        observation_id=event.observation_id,
        task_id=event.task_id,
        disposition=event.disposition.value,
        reason_code=event.reason_code,
        caused_by_event_id=event.caused_by_event_id,
        contract_version=event.contract_version,
        created_at=event.created_at,
    )


def disposition_event_from_record(
    record: ObservationDispositionRecord,
) -> ObservationDispositionEventV1:
    return ObservationDispositionEventV1(
        contract_version=cast(
            Literal["observation_disposition_event_v1"], record.contract_version
        ),
        disposition_event_id=record.disposition_event_id,
        observation_id=record.observation_id,
        task_id=record.task_id,
        disposition=ObservationDispositionV1(record.disposition),
        reason_code=record.reason_code,
        caused_by_event_id=record.caused_by_event_id,
        created_at=record.created_at,
    )


class TaskObservationRepository:
    """观察不可变投影适配器；持有同 Session，不提交、不开启事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, observation: TaskObservationV1) -> ObservationInsertResult:
        record = record_from_observation(observation)
        values = {
            column.name: getattr(record, column.name)
            for column in TaskObservationRecord.__table__.columns
        }
        statement = (
            pg_insert(TaskObservationRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[TaskObservationRecord.observation_id])
            .returning(TaskObservationRecord.observation_id)
        )
        inserted_id = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted_id is not None:
            return ObservationInsertResult(created=True, observation_id=inserted_id)
        return ObservationInsertResult(created=False, observation_id=observation.observation_id)

    async def append_disposition(
        self, event: ObservationDispositionEventV1
    ) -> DispositionAppendResult:
        record = disposition_record_from_event(event)
        values = {
            column.name: getattr(record, column.name)
            for column in ObservationDispositionRecord.__table__.columns
        }
        statement = (
            pg_insert(ObservationDispositionRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ObservationDispositionRecord.disposition_event_id])
            .returning(ObservationDispositionRecord.disposition_event_id)
        )
        inserted_id = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted_id is not None:
            return DispositionAppendResult(created=True, disposition_event_id=inserted_id)
        return DispositionAppendResult(
            created=False, disposition_event_id=event.disposition_event_id
        )

    async def get(self, observation_id: UUID) -> TaskObservationV1 | None:
        record = await self._get_record(observation_id)
        return None if record is None else observation_from_record(record)

    async def list_dispositions(
        self, observation_id: UUID
    ) -> tuple[ObservationDispositionEventV1, ...]:
        statement: Select[tuple[ObservationDispositionRecord]] = (
            select(ObservationDispositionRecord)
            .where(ObservationDispositionRecord.observation_id == observation_id)
            .order_by(
                ObservationDispositionRecord.created_at,
                ObservationDispositionRecord.disposition_event_id,
            )
        )
        records = (await self._session.scalars(statement)).all()
        return tuple(disposition_event_from_record(record) for record in records)

    async def _get_record(self, observation_id: UUID) -> TaskObservationRecord | None:
        statement: Select[tuple[TaskObservationRecord]] = select(TaskObservationRecord).where(
            TaskObservationRecord.observation_id == observation_id
        )
        return (await self._session.scalars(statement)).first()


__all__ = [
    "DispositionAppendResult",
    "ObservationInsertResult",
    "ObservationRecordIntegrityError",
    "TaskObservationRepository",
    "disposition_event_from_record",
    "disposition_record_from_event",
    "observation_from_record",
    "record_from_observation",
]
