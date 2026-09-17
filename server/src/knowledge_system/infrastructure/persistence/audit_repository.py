"""审计追加的SQLAlchemy适配器：只INSERT，分配前序Hash并推进链头；不提交事务。"""

from __future__ import annotations

import hashlib
from typing import Final, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_system.modules.audit.domain.audit_event import AuditEventDraft

from .audit_models import AuditEventRecord

AUDIT_CHAIN_KEY: Final = "default"


class AuditChainError(RuntimeError):
    """审计链头缺失，无法分配前序Hash。"""


class AuditRepository:
    """在既有Session事务内追加一条审计事件；链头使用`FOR UPDATE`串行化并发写。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, draft: AuditEventDraft) -> AuditEventRecord:
        canonical = draft.canonical_bytes()
        previous_hash = await self._lock_head()
        event_hash = hashlib.sha256(
            ("" if previous_hash is None else previous_hash).encode("ascii") + canonical
        ).hexdigest()
        record = AuditEventRecord(
            event_id=draft.event_id,
            occurred_at=draft.occurred_at,
            actor_id=draft.actor_id,
            actor_role_snapshot=list(draft.actor_role_snapshot),
            session_id=draft.session_id,
            source_ip=draft.source_ip,
            device_id=draft.device_id,
            action=draft.action,
            resource_type=draft.resource_type,
            resource_id=draft.resource_id,
            result=draft.result,
            reason_code=draft.reason_code,
            before_digest=draft.before_digest,
            after_digest=draft.after_digest,
            details_json=None if draft.details_json is None else dict(draft.details_json),
            trace_id=draft.trace_id,
            previous_hash=previous_hash,
            event_hash=event_hash,
        )
        self._session.add(record)
        await self._session.flush((record,))
        await self._update_head(event_hash)
        return record

    async def _lock_head(self) -> str | None:
        head = (
            await self._session.execute(
                text(
                    "SELECT head_hash FROM audit.audit_chain_head "
                    "WHERE chain_key = :key FOR UPDATE"
                ),
                {"key": AUDIT_CHAIN_KEY},
            )
        ).first()
        if head is None:
            raise AuditChainError("AUDIT_CHAIN_HEAD_MISSING")
        return cast(str | None, head.head_hash)

    async def _update_head(self, event_hash: str) -> None:
        await self._session.execute(
            text("UPDATE audit.audit_chain_head SET head_hash = :hash WHERE chain_key = :key"),
            {"hash": event_hash, "key": AUDIT_CHAIN_KEY},
        )
