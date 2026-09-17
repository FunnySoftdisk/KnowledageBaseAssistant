"""追加审计事件：字段与确定性规范序列化（JCS/RFC8785）。

`event_hash`不在领域草稿中，由审计链在落库时依据前序Hash计算；`canonical_bytes()`
只覆盖事件内容字段（不含`previous_hash`/`event_hash`），保证Hash可离线复验。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import rfc8785


class AuditResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"


@dataclass(frozen=True, slots=True)
class AuditEventDraft:
    """待追加的审计事件草稿；字段与数据库`audit.audit_event`逐列对应。"""

    event_id: UUID
    occurred_at: datetime
    actor_id: UUID | None
    actor_role_snapshot: tuple[str, ...]
    session_id: UUID | None
    source_ip: str | None
    device_id: UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    result: str
    reason_code: str | None
    before_digest: str | None
    after_digest: str | None
    details_json: Mapping[str, object] | None
    trace_id: str

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "actor_id": None if self.actor_id is None else str(self.actor_id),
            "actor_role_snapshot": list(self.actor_role_snapshot),
            "session_id": None if self.session_id is None else str(self.session_id),
            "source_ip": self.source_ip,
            "device_id": None if self.device_id is None else str(self.device_id),
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "result": self.result,
            "reason_code": self.reason_code,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "details_json": None if self.details_json is None else dict(self.details_json),
            "trace_id": self.trace_id,
        }

    def canonical_bytes(self) -> bytes:
        return rfc8785.dumps(self.canonical_payload())
