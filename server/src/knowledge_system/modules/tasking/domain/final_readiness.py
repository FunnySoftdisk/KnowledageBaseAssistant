"""AGT-08 §5.36：Final Ready 门禁的确定性判定。

§5.36 把 Schema/Profile/Prompt/Namespace/模型 Binding/Migration/API/SSE/UI/错误/Metric/Alert、
90 例 Gold 报告、Replay/故障注入与 Canary 全部列为 `REQUIRED_BEFORE_READY`；任一缺失则
`AGT-08=NOT_READY`。本切片固化该检查清单与 `compute_final_readiness` 纯函数：给定已就位项集合，
确定性输出 `FinalReadinessReportV1`（READY/NOT_READY + 缺失项 + JCS 摘要），零 LLM、零 I/O。
"""

from __future__ import annotations

from typing import Literal

from .core_types import Sha256Hex, StableKey, StrictContractV1, canonical_json_sha256

FinalReadinessVerdictV1 = Literal["READY", "NOT_READY"]

FINAL_READY_REQUIRED_ITEMS: frozenset[StableKey] = frozenset(
    {
        "SCHEMA",
        "PROFILE",
        "PROMPT",
        "NAMESPACE",
        "MODEL_BINDING",
        "MIGRATION",
        "API",
        "SSE",
        "UI",
        "ERROR_CODE",
        "METRIC",
        "ALERT",
        "GOLD_REPORT_90",
        "REPLAY_FAULT_INJECTION",
        "CANARY",
    }
)


class FinalReadinessReportV1(StrictContractV1):
    """不可变 Ready 门禁报告：READY/NOT_READY + 缺失必做项 + JCS 摘要。"""

    contract_version: Literal["final_readiness_report_v1"]
    verdict: FinalReadinessVerdictV1
    missing_required_items: tuple[StableKey, ...] = ()
    readiness_digest: Sha256Hex = "0" * 64


def compute_final_readiness(present: tuple[str, ...]) -> FinalReadinessReportV1:
    """任一 `REQUIRED_BEFORE_READY` 项缺失即 NOT_READY；未知项失败关闭，缺失项排序输出。"""

    present_set = set(present)
    unknown = present_set - set(FINAL_READY_REQUIRED_ITEMS)
    if unknown:
        raise ValueError("FINAL_READY_UNKNOWN_ITEM")
    missing = tuple(sorted(set(FINAL_READY_REQUIRED_ITEMS) - present_set))
    verdict: FinalReadinessVerdictV1 = "NOT_READY" if missing else "READY"
    record = FinalReadinessReportV1(
        contract_version="final_readiness_report_v1",
        verdict=verdict,
        missing_required_items=missing,
        readiness_digest="0" * 64,
    )
    digest = canonical_json_sha256(record.model_dump(mode="json", exclude={"readiness_digest"}))
    return record.model_copy(update={"readiness_digest": digest})


__all__ = [
    "FINAL_READY_REQUIRED_ITEMS",
    "FinalReadinessReportV1",
    "FinalReadinessVerdictV1",
    "compute_final_readiness",
]
