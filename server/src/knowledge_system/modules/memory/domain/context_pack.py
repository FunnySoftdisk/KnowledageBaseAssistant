"""DEV-08：Context Pack 领域契约 + Token 预算与确定性裁剪规则（零 LLM/零 I/O）。

权威设计：模块16 §4.3/§4.4/§5.1/§11.2/§11.3/§11.4。本文件只冻结：
- Context Item/Bucket/操作画像枚举；
- ``ContextBudgetV1`` 契约与 ``available_input = max_context - reserved_output -
  protocol_overhead - safety_margin``、``elastic_tokens = available_input - mandatory_tokens``
  的确定性预算算术（§4.4）；
- 弹性预算比例分配（§11.2 初始目标基线，可被 Context Policy 按 Agent Manifest 覆盖，最终权重由
  上下文评测集冻结——本文件不把它们伪装成运行校准事实）；
- P0 精确去重 → 按 Bucket 优先级 P1/P2 裁剪 → 超限稳定失败码的确定性选择（§11.3/§11.4）。

不访问模型/网络/ORM/Temporal；Token 计数由调用方按 ``token_accounting`` 口径供给（精确计数与
Tokenizer 适配、RRF/MMR 排序、向量召回、P3-P5 摘要压缩均属后续切片）。
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import Field

from knowledge_system.modules.tasking.domain.core_types import Sha256Hex, StrictContractV1

from .memory_types import ProvenanceRefV1


class ContextBucketV1(StrEnum):
    """弹性预算桶（§4.4）。SUMMARY 不参与 §11.2 通用比例，其语义属 P3-P5 摘要切片。"""

    RECENT = "RECENT"
    MEMORY = "MEMORY"
    TASK_ARTIFACT = "TASK_ARTIFACT"
    EVIDENCE = "EVIDENCE"
    SUMMARY = "SUMMARY"


class ContextItemTypeV1(StrEnum):
    """Context Item 类型（§4.3）；前四类是强制项，其余为弹性项。"""

    SYSTEM_POLICY = "SYSTEM_POLICY"
    TOOL_SCHEMA = "TOOL_SCHEMA"
    OUTPUT_SCHEMA = "OUTPUT_SCHEMA"
    CURRENT_MESSAGE = "CURRENT_MESSAGE"
    RECENT_MESSAGE = "RECENT_MESSAGE"
    MEMORY = "MEMORY"
    TASK_ARTIFACT = "TASK_ARTIFACT"
    OBSERVATION = "OBSERVATION"
    EVIDENCE = "EVIDENCE"
    SUMMARY = "SUMMARY"


class ContextOperationProfileV1(StrEnum):
    """当前 Agent 动作画像（§11.2）。事实核验与外部研究不使用本通用弹性比例。"""

    KNOWLEDGE_RESEARCH = "KNOWLEDGE_RESEARCH"
    MULTI_MATERIAL_ANALYSIS = "MULTI_MATERIAL_ANALYSIS"
    SYNTHESIS_ACTION = "SYNTHESIS_ACTION"
    FINAL_SYNTHESIS = "FINAL_SYNTHESIS"
    FACT_VERIFICATION = "FACT_VERIFICATION"
    EXTERNAL_RESEARCH = "EXTERNAL_RESEARCH"
    GOVERNANCE_EXPLANATION = "GOVERNANCE_EXPLANATION"
    SUPERVISOR_PLANNING = "SUPERVISOR_PLANNING"


# §11.2 初始目标比例，作用于 elastic_tokens：(RECENT, MEMORY, TASK_ARTIFACT, EVIDENCE) 百分比。
# FACT_VERIFICATION/EXTERNAL_RESEARCH 采用模块09/11 的专用闭世界预算，不在此表。
_ELASTIC_RATIO_PERCENT: dict[ContextOperationProfileV1, tuple[int, int, int, int]] = {
    ContextOperationProfileV1.KNOWLEDGE_RESEARCH: (15, 10, 10, 65),
    ContextOperationProfileV1.MULTI_MATERIAL_ANALYSIS: (10, 10, 35, 45),
    ContextOperationProfileV1.SYNTHESIS_ACTION: (10, 10, 40, 40),
    ContextOperationProfileV1.FINAL_SYNTHESIS: (0, 0, 50, 50),
    ContextOperationProfileV1.GOVERNANCE_EXPLANATION: (5, 5, 40, 50),
    ContextOperationProfileV1.SUPERVISOR_PLANNING: (15, 10, 60, 15),
}

# §11.2 强制项：安全/系统策略、已批准 Tool/Output Schema、当前用户完整请求。
_MANDATORY_ITEM_TYPES = frozenset(
    {
        ContextItemTypeV1.SYSTEM_POLICY,
        ContextItemTypeV1.TOOL_SCHEMA,
        ContextItemTypeV1.OUTPUT_SCHEMA,
        ContextItemTypeV1.CURRENT_MESSAGE,
    }
)

# 弹性项 → 预算桶（§4.4/§5.1）。OBSERVATION 计入 TASK_ARTIFACT 桶（§5.1）。
_ITEM_BUCKET: dict[ContextItemTypeV1, ContextBucketV1] = {
    ContextItemTypeV1.RECENT_MESSAGE: ContextBucketV1.RECENT,
    ContextItemTypeV1.MEMORY: ContextBucketV1.MEMORY,
    ContextItemTypeV1.TASK_ARTIFACT: ContextBucketV1.TASK_ARTIFACT,
    ContextItemTypeV1.OBSERVATION: ContextBucketV1.TASK_ARTIFACT,
    ContextItemTypeV1.EVIDENCE: ContextBucketV1.EVIDENCE,
    ContextItemTypeV1.SUMMARY: ContextBucketV1.SUMMARY,
}


class ContextBudgetV1(StrictContractV1):
    """上下文 Token 预算（§4.4）。``bucket_limits`` 由画像比例分配自 ``elastic_tokens``。"""

    max_context_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    protocol_overhead_tokens: int = Field(ge=0)
    safety_margin_tokens: int = Field(ge=0)
    mandatory_tokens: int = Field(ge=0)
    elastic_tokens: int = Field(ge=0)
    bucket_limits: dict[ContextBucketV1, int]


class ContextItemManifestV1(StrictContractV1):
    """单个 Context Item 的 Manifest（§4.3）；正文在 Body，此清单只记录身份与 Token 账。"""

    item_id: UUID
    item_type: ContextItemTypeV1
    source_ref: ProvenanceRefV1 | None = None
    content_hash: Sha256Hex
    token_count: int = Field(ge=0)
    priority: int = Field(ge=0)
    rank_score: float | None = None
    compression_level: int = Field(ge=0)
    selection_reason: str = Field(min_length=1)
    trust: str = Field(min_length=1)


class ContextSelectionResultV1(StrictContractV1):
    """确定性裁剪产物（§11.3/§11.4）：选中项、因预算被裁剪的弹性项、实际总 Token 与失败码。"""

    selected: tuple[ContextItemManifestV1, ...]
    dropped: tuple[ContextItemManifestV1, ...]
    total_tokens: int = Field(ge=0)
    overflow: bool
    error_code: str | None = None


def is_mandatory_item_type(item_type: ContextItemTypeV1) -> bool:
    """§11.2：强制项（System/Tool/Output Schema 与当前用户请求）始终装配，不参与弹性裁剪。"""

    return item_type in _MANDATORY_ITEM_TYPES


def bucket_for_item_type(item_type: ContextItemTypeV1) -> ContextBucketV1 | None:
    """弹性项 → 预算桶；强制项无弹性桶，返回 ``None``。"""

    return _ITEM_BUCKET.get(item_type)


def allocate_elastic_budget(
    profile: ContextOperationProfileV1, elastic_tokens: int
) -> dict[ContextBucketV1, int]:
    """§11.2：把 ``elastic_tokens`` 按画像比例向下取整分配；取整余量不再分配（保守）。

    事实核验/外部研究采用专用闭世界预算，调用此函数以 ``PROFILE_USES_DEDICATED_CONTEXT_BUDGET``
    失败关闭。SUMMARY 桶不独立分配（摘要替换其来源桶内容，语义属 P3-P5 摘要切片）。
    """

    if profile not in _ELASTIC_RATIO_PERCENT:
        raise ValueError("PROFILE_USES_DEDICATED_CONTEXT_BUDGET")
    if elastic_tokens < 0:
        raise ValueError("ELASTIC_TOKENS_NEGATIVE")
    recent, memory, task_artifact, evidence = _ELASTIC_RATIO_PERCENT[profile]
    return {
        ContextBucketV1.RECENT: elastic_tokens * recent // 100,
        ContextBucketV1.MEMORY: elastic_tokens * memory // 100,
        ContextBucketV1.TASK_ARTIFACT: elastic_tokens * task_artifact // 100,
        ContextBucketV1.EVIDENCE: elastic_tokens * evidence // 100,
        ContextBucketV1.SUMMARY: 0,
    }


def compute_context_budget(
    *,
    max_context_tokens: int,
    reserved_output_tokens: int,
    protocol_overhead_tokens: int,
    safety_margin_tokens: int,
    mandatory_tokens: int,
    profile: ContextOperationProfileV1,
) -> ContextBudgetV1:
    """§4.4：预算算术。强制项超过可用输入即失败关闭，不压缩当前用户请求或安全策略。"""

    if min(
        max_context_tokens,
        reserved_output_tokens,
        protocol_overhead_tokens,
        safety_margin_tokens,
        mandatory_tokens,
    ) < 0:
        raise ValueError("BUDGET_FIELD_NEGATIVE")
    available_input = (
        max_context_tokens
        - reserved_output_tokens
        - protocol_overhead_tokens
        - safety_margin_tokens
    )
    if available_input < 0:
        raise ValueError("MODEL_CONTEXT_BUDGET_INVALID")
    if mandatory_tokens > available_input:
        raise ValueError("MODEL_CONTEXT_MANDATORY_EXCEEDED")
    elastic_tokens = available_input - mandatory_tokens
    return ContextBudgetV1(
        max_context_tokens=max_context_tokens,
        reserved_output_tokens=reserved_output_tokens,
        protocol_overhead_tokens=protocol_overhead_tokens,
        safety_margin_tokens=safety_margin_tokens,
        mandatory_tokens=mandatory_tokens,
        elastic_tokens=elastic_tokens,
        bucket_limits=allocate_elastic_budget(profile, elastic_tokens),
    )


def select_context_items(
    candidates: tuple[ContextItemManifestV1, ...],
    budget: ContextBudgetV1,
) -> ContextSelectionResultV1:
    """§11.3/§11.4：P0 精确去重 → 强制项必选 → 弹性项按优先级/相关度填桶 → 超限裁剪。

    强制项始终装配；单个强制项超过可用输入窗口（无法分割的原子项）以
    ``CONTEXT_ATOMIC_ITEM_TOO_LARGE`` 失败关闭。弹性项按 (priority 降序, rank_score 降序,
    item_id 升序) 稳定排序后填入所属桶，超出桶余量即被裁剪（非 Required，可删）。
    """

    available_input = budget.mandatory_tokens + budget.elastic_tokens

    # P0 精确去重（§11.4-1）：按内容哈希保留首个，调用方负责候选进入顺序。
    seen: set[str] = set()
    deduped: list[ContextItemManifestV1] = []
    for item in candidates:
        if item.content_hash in seen:
            continue
        seen.add(item.content_hash)
        deduped.append(item)

    mandatory = [item for item in deduped if is_mandatory_item_type(item.item_type)]
    elastic = [item for item in deduped if not is_mandatory_item_type(item.item_type)]

    # 单个强制项超过整个可用窗口，禁止静默截断（§11.4-7）。
    for item in mandatory:
        if item.token_count > available_input:
            return ContextSelectionResultV1(
                selected=(),
                dropped=tuple(deduped),
                total_tokens=0,
                overflow=True,
                error_code="CONTEXT_ATOMIC_ITEM_TOO_LARGE",
            )

    elastic.sort(
        key=lambda item: (
            -item.priority,
            -(item.rank_score if item.rank_score is not None else float("-inf")),
            item.item_id,
        )
    )

    selected = list(mandatory)
    remaining = dict(budget.bucket_limits)
    dropped: list[ContextItemManifestV1] = []
    for item in elastic:
        bucket = bucket_for_item_type(item.item_type)
        if bucket is None:
            dropped.append(item)
            continue
        limit = remaining[bucket]
        if item.token_count <= limit:
            selected.append(item)
            remaining[bucket] = limit - item.token_count
        else:
            dropped.append(item)

    return ContextSelectionResultV1(
        selected=tuple(selected),
        dropped=tuple(dropped),
        total_tokens=sum(item.token_count for item in selected),
        overflow=bool(dropped),
    )


__all__ = [
    "ContextBucketV1",
    "ContextBudgetV1",
    "ContextItemManifestV1",
    "ContextItemTypeV1",
    "ContextOperationProfileV1",
    "ContextSelectionResultV1",
    "allocate_elastic_budget",
    "bucket_for_item_type",
    "compute_context_budget",
    "is_mandatory_item_type",
    "select_context_items",
]
