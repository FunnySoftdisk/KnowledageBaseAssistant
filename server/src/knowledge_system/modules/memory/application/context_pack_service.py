"""DEV-08 子批次 10：Context Pack 复用编排（reuse-or-assemble，零 LLM/零 I/O）。

把子批次 7 的确定性装配与子批次 8/9 的权威 Store 写/读串成「先查复用、未命中再装配落库」的
`obtain`：按 `input_digest` 内容寻址——同一任务内 `input_digest` 命中 READY/CONSUMED 头行即复用
既有不可变 Pack（§4.3/§12.1），未命中则 `assemble_context_pack` 装配新 Pack 并 `save_pack` 落库。

本切片不做并发线性化：`input_digest` 复用判断与 Pack 落库不是原子唯一接受点（§12.1/§13 的
「Gateway 接受 ModelCall 是并发语义的线性化点」属模型网关接线消费切片），两个并发同 digest 请求
各自 ASSEMBLED 的可能由网关消费事务裁决。`ContextPackRepositoryPort` 由调用方注入，本服务不提交、
不开启事务。
"""

from __future__ import annotations

from enum import StrEnum

from knowledge_system.modules.tasking.domain.core_types import Sha256Hex, StrictContractV1

from ..domain.context_assembly import (
    ContextAssemblyInputV1,
    ContextPackReuseHitV1,
    ContextPackV1,
    assemble_context_pack,
    compute_input_digest,
)
from .ports import ContextPackRepositoryPort, ContextPackSaveOutcomeV1


class ContextPackOutcomeV1(StrEnum):
    """`obtain` 的终局：命中 `input_digest` 复用既有 Pack，或未命中装配新 Pack。"""

    REUSED = "REUSED"
    ASSEMBLED = "ASSEMBLED"


class ContextPackObtainResultV1(StrictContractV1):
    """`obtain` 的结果：REUSED 只回读侧复用命中，ASSEMBLED 回完整新 Pack + 落库结果。

    两分支互斥：REUSED 时仅 `reused` 非空，ASSEMBLED 时仅 `pack`/`save_outcome` 非空。
    """

    outcome: ContextPackOutcomeV1
    input_digest: Sha256Hex
    reused: ContextPackReuseHitV1 | None = None
    pack: ContextPackV1 | None = None
    save_outcome: ContextPackSaveOutcomeV1 | None = None


class ContextPackService:
    """确定性复用编排：`input_digest` 查复用 → 未命中装配并落库。"""

    def __init__(self, repository: ContextPackRepositoryPort) -> None:
        self._repository = repository

    async def obtain(self, request: ContextAssemblyInputV1) -> ContextPackObtainResultV1:
        """按 `input_digest` 内容寻址复用：命中则回读复用命中，未命中则装配新 Pack 并落库。"""

        input_digest = compute_input_digest(request)
        hit = await self._repository.find_reusable_by_input_digest(
            task_id=request.task_id, input_digest=input_digest
        )
        if hit is not None:
            return ContextPackObtainResultV1(
                outcome=ContextPackOutcomeV1.REUSED,
                input_digest=input_digest,
                reused=hit,
            )
        pack = assemble_context_pack(request)
        save_outcome = await self._repository.save_pack(pack=pack)
        return ContextPackObtainResultV1(
            outcome=ContextPackOutcomeV1.ASSEMBLED,
            input_digest=input_digest,
            pack=pack,
            save_outcome=save_outcome,
        )


__all__ = [
    "ContextPackObtainResultV1",
    "ContextPackOutcomeV1",
    "ContextPackService",
]
