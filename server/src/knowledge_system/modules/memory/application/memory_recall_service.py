"""DEV-08 子批次 5：记忆召回确定性编排（零 LLM/零 embedding/零 I/O）。

按模块16 §9.2 编排 `recall()`：确定性通道（canonical_key 精确匹配 + 最近 Active Memory）→ 按
record.id 精确去重 → `revalidate_record` 防御性重验 → kind 过滤 → 冲突组兄弟展开（不静默丢弃冲突
双方）→ 确定性排序（importance 降序、valid_from 降序、id 升序）→ `max_candidates` 截断。

全文检索与向量召回（通道4，需 FTS/pgvector/embedding）、RRF/MMR
排序（§9.3，权重由评测集冻结）、`token_budget` 门禁（由 Context 装配层按 Manifest Token 账执行）均
留后续切片。仓库查询已按 owner+scope+ACTIVE 过滤；本切片再做一次闭包重验，不改写任何记录。
"""

from __future__ import annotations

from uuid import UUID

from ..domain.memory_recall import (
    MemoryRecallChannelV1,
    MemoryRecallRequestV1,
    MemoryRecallResultV1,
    MemorySearchHitV1,
    revalidate_record,
)
from ..domain.memory_types import MemoryRecordV1
from .ports import MemoryRepositoryPort


class MemoryRecallService:
    """确定性召回编排：候选 → 重验/过滤 → 冲突展开 → 排序 → 截断。"""

    def __init__(self, repository: MemoryRepositoryPort) -> None:
        self._repository = repository

    async def recall(self, request: MemoryRecallRequestV1) -> MemoryRecallResultV1:
        scopes = frozenset(request.scopes)
        required_kinds = frozenset(request.required_kinds)

        # §9.2 通道收集，按 record.id 精确去重（首见通道优先，遍历顺序确定）。
        by_id: dict[UUID, MemorySearchHitV1] = {}
        for scope in request.scopes:
            # 通道2：被用户 Pin 的 Active Memory（取 max_candidates+1，多取一条判定 truncated）。
            for pinned_record in await self._repository.find_pinned(
                owner_id=request.owner_id, scope=scope, limit=request.max_candidates + 1
            ):
                if pinned_record.id not in by_id:
                    by_id[pinned_record.id] = MemorySearchHitV1(
                        record=pinned_record, channel=MemoryRecallChannelV1.PINNED
                    )
        for key in request.canonical_keys:
            for scope in request.scopes:
                record = await self._repository.find_active_by_key(
                    owner_id=request.owner_id, scope=scope, canonical_key=key
                )
                if record is not None and record.id not in by_id:
                    by_id[record.id] = MemorySearchHitV1(
                        record=record, channel=MemoryRecallChannelV1.EXACT_KEY
                    )
        for scope in request.scopes:
            # 取 max_candidates+1：多取一条以在服务层判定 `truncated`，否则溢出会被仓库截断吞掉。
            for record in await self._repository.find_recent_active(
                owner_id=request.owner_id, scope=scope, limit=request.max_candidates + 1
            ):
                if record.id not in by_id:
                    by_id[record.id] = MemorySearchHitV1(
                        record=record, channel=MemoryRecallChannelV1.RECENT_ACTIVE
                    )

        # 防御性重验：owner/scope/state/有效期闭包（仓库已按 ACTIVE 过滤，这里再核对一次）。
        rejected: dict[UUID, MemoryRecordV1] = {}
        validated: dict[UUID, MemorySearchHitV1] = {}
        for hit in by_id.values():
            if revalidate_record(
                hit.record, owner_id=request.owner_id, scopes=scopes, now=request.now
            ):
                validated[hit.record.id] = hit
            else:
                rejected[hit.record.id] = hit.record

        # kind 过滤（required_kinds 为空即不限）。
        if required_kinds:
            validated = {
                rid: hit for rid, hit in validated.items() if hit.record.kind in required_kinds
            }

        # 冲突组兄弟展开：EXACT_KEY 只命中单条，故把同组 ACTIVE 双方都带入，供 Context 呈现分歧。
        expanded: dict[UUID, MemorySearchHitV1] = dict(validated)
        for hit in list(validated.values()):
            group = hit.record.conflict_group_id
            if group is None:
                continue
            for sibling in await self._repository.find_active_by_conflict_group(
                owner_id=request.owner_id, conflict_group_id=group
            ):
                if sibling.id in expanded:
                    continue
                if not revalidate_record(
                    sibling, owner_id=request.owner_id, scopes=scopes, now=request.now
                ):
                    rejected.setdefault(sibling.id, sibling)
                    continue
                if required_kinds and sibling.kind not in required_kinds:
                    continue
                expanded[sibling.id] = MemorySearchHitV1(record=sibling, channel=hit.channel)

        # 确定性排序：importance 降序 → valid_from 降序 → id 升序（UUID 字符串稳定）。
        ordered = sorted(
            expanded.values(),
            key=lambda hit: (
                -hit.record.importance,
                -hit.record.valid_from.timestamp(),
                str(hit.record.id),
            ),
        )

        truncated = len(ordered) > request.max_candidates
        return MemoryRecallResultV1(
            hits=tuple(ordered[: request.max_candidates]),
            rejected=tuple(rejected.values()),
            truncated=truncated,
        )


__all__ = ["MemoryRecallService"]
