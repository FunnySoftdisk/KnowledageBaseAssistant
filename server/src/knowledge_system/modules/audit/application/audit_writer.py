"""审计写入端口：独立短事务追加（供需要自带事务边界的用例使用）。"""

from __future__ import annotations

from typing import Protocol

from ..domain.audit_event import AuditEventDraft


class AuditWriter(Protocol):
    async def append_committed(self, draft: AuditEventDraft) -> None: ...
