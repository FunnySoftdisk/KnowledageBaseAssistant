"""Final 链指标契约（DEV-06 OTel/Metric 观测离线切片）。

依据日志指标告警字典 §5 与 §6.2：任务域与 `Final` 综合链的可观测指标统一以 `knowledge_system_`
前缀、计数器后缀 `_total`、时间 `_seconds`、字节 `_bytes`。本模块冻结版本化指标注册表
（名称/类型/允许标签）与确定性校验：业务代码经 `TelemetryService.record_metric` 提交前必须通过
本契约，未定义指标、类型不符、标签越界或命中高基数/Secret 标签一律失败关闭，不静默吞掉。

标签值基数（如 `answer_status` 具体取值、`model_alias` 别名枚举）由各领域 Enum 约束，标签值
级基数治理（exemplar/relabel 配置）属基础设施；本切片只做名称级契约。OTel SDK/Collector/
Prometheus 真实埋点与部署留待 EXTERNAL_REQUIRED。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from .core_types import StrictContractV1


class MetricKindV1(StrEnum):
    COUNTER = "COUNTER"
    GAUGE = "GAUGE"
    HISTOGRAM = "HISTOGRAM"


class MetricDefinitionV1(StrictContractV1):
    """版本化指标定义：名称、类型与允许的标签键集合。"""

    name: str
    kind: MetricKindV1
    labels: frozenset[str]


class MetricContractViolation(ValueError):
    """稳定code可公开；异常中不保存标签值、指标取值或Secret。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# 禁止作为指标标签的键：Secret/认证材料（镜像 span_policy._SENSITIVE_ATTRIBUTE_NAMES）+ §5 禁止的
# 高基数业务标识（user_id/task_id/文件名/URL/trace_id/错误文本等）。
_FORBIDDEN_LABEL_NAMES = frozenset(
    {
        # Secret/认证材料
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "apikey",
        "x_api_key",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "bearer",
        "password",
        "passwd",
        "secret",
        "secret_ref",
        "secretref",
        "credential",
        "credentials",
        "private_key",
        "signing_key",
        "jwt",
        # 高基数业务标识与正文
        "user_id",
        "task_id",
        "trace_id",
        "span_id",
        "url",
        "filename",
        "file_name",
        "error_text",
        "error_message",
        "message",
        "content",
        "prompt",
        "history",
        "digest",
        "fingerprint",
        "artifact_id",
        "invocation_id",
        "attempt_id",
        "observation_id",
        "plan_item_id",
        "criterion_id",
        "citation_id",
        "claim_id",
        "grant_id",
        "approval_id",
        "organization_id",
        "tenant_id",
        "conversation_id",
        "snapshot_id",
        "run_id",
        "case_id",
    }
)


def _freeze_metric(name: str, kind: MetricKindV1, labels: frozenset[str]) -> MetricDefinitionV1:
    """冻结单一指标定义并在导入期自审计命名约定与标签安全性。"""

    if not name.startswith("knowledge_system_"):
        raise ValueError(f"METRIC_NAME_PREFIX_VIOLATION: {name}")
    if kind is MetricKindV1.COUNTER and not name.endswith("_total"):
        raise ValueError(f"METRIC_COUNTER_MISSING_TOTAL_SUFFIX: {name}")
    if kind is not MetricKindV1.COUNTER and name.endswith("_total"):
        raise ValueError(f"METRIC_NON_COUNTER_TOTAL_SUFFIX: {name}")
    if name.endswith("_seconds") and kind is MetricKindV1.COUNTER:
        raise ValueError(f"METRIC_SECONDS_ON_COUNTER: {name}")
    forbidden = labels & _FORBIDDEN_LABEL_NAMES
    if forbidden:
        raise ValueError(f"METRIC_FORBIDDEN_LABEL: {name}: {sorted(forbidden)}")
    return MetricDefinitionV1(name=name, kind=kind, labels=labels)


# §6.2 任务和 Agent：任务终态事务（task_result）+ Final 综合决策（final_synthesis）25 项指标。
FINAL_CHAIN_METRIC_CONTRACT_V1: dict[str, MetricDefinitionV1] = {
    definition.name: definition
    for definition in (
        _freeze_metric(
            "knowledge_system_tasks_total",
            MetricKindV1.COUNTER,
            frozenset({"scenario_label", "terminal_status"}),
        ),
        _freeze_metric(
            "knowledge_system_task_result_commits_total",
            MetricKindV1.COUNTER,
            frozenset({"contract_version", "answer_status", "outcome"}),
        ),
        _freeze_metric(
            "knowledge_system_task_result_validation_failures_total",
            MetricKindV1.COUNTER,
            frozenset({"contract_version", "stage", "reason"}),
        ),
        _freeze_metric(
            "knowledge_system_task_result_output_limit_exceeded_total",
            MetricKindV1.COUNTER,
            frozenset({"limit_kind", "size_bucket"}),
        ),
        _freeze_metric(
            "knowledge_system_task_result_atomicity_violations_total",
            MetricKindV1.COUNTER,
            frozenset({"stage", "reason"}),
        ),
        _freeze_metric(
            "knowledge_system_task_result_reconcile_failures_total",
            MetricKindV1.COUNTER,
            frozenset({"stage", "reason"}),
        ),
        _freeze_metric(
            "knowledge_system_task_result_staging_orphans",
            MetricKindV1.GAUGE,
            frozenset({"age_bucket"}),
        ),
        _freeze_metric(
            "knowledge_system_task_result_staging_oldest_seconds",
            MetricKindV1.GAUGE,
            frozenset({"environment"}),
        ),
        _freeze_metric(
            "knowledge_system_task_result_citations",
            MetricKindV1.HISTOGRAM,
            frozenset({"answer_status", "count_bucket"}),
        ),
        _freeze_metric(
            "knowledge_system_task_result_artifacts",
            MetricKindV1.HISTOGRAM,
            frozenset({"answer_status", "count_bucket"}),
        ),
        _freeze_metric(
            "knowledge_system_task_result_commit_duration_seconds",
            MetricKindV1.HISTOGRAM,
            frozenset({"answer_status", "outcome"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_attempts_total",
            MetricKindV1.COUNTER,
            frozenset({"model_alias", "outcome", "repair_used"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_stage_duration_seconds",
            MetricKindV1.HISTOGRAM,
            frozenset({"stage", "outcome"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_snapshot_rebuilds_total",
            MetricKindV1.COUNTER,
            frozenset({"outcome", "rebuild_count_bucket"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_candidate_validation_total",
            MetricKindV1.COUNTER,
            frozenset({"outcome", "reason"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_completion_decisions_total",
            MetricKindV1.COUNTER,
            frozenset({"answer_status", "usability"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_criterion_status_total",
            MetricKindV1.COUNTER,
            frozenset({"completion_role", "status", "evaluation_mode"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_claim_rejections_total",
            MetricKindV1.COUNTER,
            frozenset({"reason", "claim_kind"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_security_rejections_total",
            MetricKindV1.COUNTER,
            frozenset({"reason"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_repair_attempts_total",
            MetricKindV1.COUNTER,
            frozenset({"outcome"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_model_calls_total",
            MetricKindV1.COUNTER,
            frozenset({"request_kind", "provider_alias", "outcome"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_input_tokens",
            MetricKindV1.HISTOGRAM,
            frozenset({"request_kind", "token_bucket"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_output_tokens",
            MetricKindV1.HISTOGRAM,
            frozenset({"request_kind", "token_bucket"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_contract_violations_total",
            MetricKindV1.COUNTER,
            frozenset({"violation_kind", "stage"}),
        ),
        _freeze_metric(
            "knowledge_system_final_synthesis_public_model_calls_total",
            MetricKindV1.COUNTER,
            frozenset({"environment", "provider_alias"}),
        ),
    )
}


def is_forbidden_label_name(name: str) -> bool:
    """标签键是否命中高基数/Secret 集合（大小写不敏感）。"""

    return name.strip().lower() in _FORBIDDEN_LABEL_NAMES


def metric_definition(name: str) -> MetricDefinitionV1:
    """按名称取指标定义；未注册以稳定`METRIC_UNDEFINED`失败关闭。"""

    definition = FINAL_CHAIN_METRIC_CONTRACT_V1.get(name)
    if definition is None:
        raise MetricContractViolation("METRIC_UNDEFINED")
    return definition


def validate_metric_attributes(name: str, attributes: Mapping[str, object]) -> None:
    """提交`record_metric`前的唯一契约门禁：指标已注册、标签键允许且不命中禁止集合。"""

    definition = metric_definition(name)
    for label in attributes:
        if is_forbidden_label_name(label):
            raise MetricContractViolation("METRIC_LABEL_FORBIDDEN")
        if label not in definition.labels:
            raise MetricContractViolation("METRIC_LABEL_NOT_ALLOWED")


__all__ = [
    "FINAL_CHAIN_METRIC_CONTRACT_V1",
    "MetricContractViolation",
    "MetricDefinitionV1",
    "MetricKindV1",
    "is_forbidden_label_name",
    "metric_definition",
    "validate_metric_attributes",
]
