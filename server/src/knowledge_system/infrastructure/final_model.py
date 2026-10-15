"""DEV-06：`FinalSynthesisModelPort` 的 PydanticAI 适配器。

把冻结的 Final 快照与支持目录序列化为确定性 Prompt，以 `NativeOutput(strict=True)` 跑
PydanticAI Agent，`retries=STRUCTURAL_REPAIR_LIMIT`（=2）即「最多两次结构修复」——输出解析
失败时 PydanticAI 把校验错误回喂模型重试，超出预算以 `UnexpectedModelBehavior` 失败关闭，不
静默放宽 Schema。校验给出的 `repair_hints`（有界修复路径）在重试请求中追加到 Prompt，让模型
定向修复而非重写。

模型只产出 `FinalSynthesisModelOutputV1`（候选内容子集，不含快照身份/摘要）；适配器按快照
内容寻址派生 `snapshot_id` 并注入 `snapshot_digest`，装配为完整 `FinalSynthesisCandidateV1`。
模型通过构造注入：离线验收注入 `FunctionModel` 测试替身，真实运行注入经 Secret 交接的
Provider 模型；适配器不持有密钥、不选择 Provider、不做授权。OTel/Phoenix 观测接线属 DEV-04
退出项，故 `instrument=False`。
"""

from __future__ import annotations

import json

from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from knowledge_system.modules.tasking.domain.final_admission import FinalSupportCatalogEntryV1
from knowledge_system.modules.tasking.domain.final_candidate import (
    FinalSynthesisCandidateV1,
    FinalSynthesisModelOutputV1,
    assemble_final_candidate,
)
from knowledge_system.modules.tasking.domain.final_synthesis import FinalSynthesisSnapshotV1
from knowledge_system.modules.tasking.domain.supervisor_profiles import STRUCTURAL_REPAIR_LIMIT


def _serialize_materials(
    snapshot: FinalSynthesisSnapshotV1,
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    repair_hints: tuple[str, ...],
) -> str:
    payload: dict[str, object] = {
        "snapshot": snapshot.model_dump(mode="json"),
        "support_catalog": [entry.model_dump(mode="json") for entry in catalog],
    }
    if repair_hints:
        payload["repair_hints"] = list(repair_hints)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class PydanticFinalModel:
    """PydanticAI 实现的 Final 候选生成模型端口。

    `system_prompt` 为可选静态系统提示，仅在真实模型实调（Canary）时注入，用于把候选生成的
    确定性契约（可引用目录白名单、local_id 约定、citation/artifact 完备要求）告知模型；离线
    验收注入 `FunctionModel` 测试替身时不传（保持裸上下文语义），故默认 `None`。
    """

    def __init__(self, model: Model, system_prompt: str | None = None) -> None:
        self._model = model
        self._system_prompt = system_prompt

    async def generate_candidate(
        self,
        snapshot: FinalSynthesisSnapshotV1,
        catalog: tuple[FinalSupportCatalogEntryV1, ...],
        repair_hints: tuple[str, ...] = (),
    ) -> FinalSynthesisCandidateV1:
        agent = Agent(
            self._model,
            name="final-synthesis-candidate",
            output_type=NativeOutput(FinalSynthesisModelOutputV1, strict=True),
            system_prompt=self._system_prompt if self._system_prompt is not None else (),
            tools=[],
            retries=STRUCTURAL_REPAIR_LIMIT,
        )
        agent.instrument = False
        result = await agent.run(_serialize_materials(snapshot, catalog, repair_hints))
        return assemble_final_candidate(result.output, snapshot)


__all__ = ["PydanticFinalModel"]
