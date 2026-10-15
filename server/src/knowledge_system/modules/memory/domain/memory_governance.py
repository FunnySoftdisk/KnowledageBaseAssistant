"""DEV-08：三作用域记忆的写入治理纯规则（零 LLM/零 I/O）。

对应模块16 §7.2.1/§7.3/§7.4/§7.5/§8.1/§8.2.1 的确定性规则。全部为模块级纯函数，
不访问模型、网络、ORM 或 Temporal；归一化只用于比较，不改变展示正文。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from .memory_types import (
    CorrectionDispositionV1,
    CorrectionPlanV1,
    DuplicateResolutionV1,
    DuplicateVerdictV1,
    MemoryChangeKindV1,
    MemoryGovernanceViolationV1,
    MemoryKindV1,
    MemoryProposalV1,
    MemoryRecordV1,
    MemoryScopeV1,
    MemoryStateV1,
    MemoryTrustV1,
    ProvenanceSourceTypeV1,
)

# 信任强度排序（§7.4）：用户显式 > 用户派生 > 证据支持 > 助手推断；弱者不得覆盖强者。
_TRUST_RANK: dict[MemoryTrustV1, int] = {
    MemoryTrustV1.USER_EXPLICIT: 4,
    MemoryTrustV1.USER_DERIVED: 3,
    MemoryTrustV1.EVIDENCE_SUPPORTED: 2,
    MemoryTrustV1.ASSISTANT_DERIVED: 1,
}

# 否定标记（粗粒度确定性启发；完整矛盾判定属受约束 DuplicateResolver，见 §8.2.1）。
_NEGATION_MARKERS = ("不", "没", "非", "禁止", "勿", "不要")

# 敏感内容拒绝（§7.2 直接拒绝项；保守失败关闭）。
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z0-9 ]+", re.IGNORECASE),
    re.compile(r"api[ _-]?key", re.IGNORECASE),
    re.compile(r"private[ _-]?key", re.IGNORECASE),
    re.compile(r"(password|passwd|secret)", re.IGNORECASE),
    re.compile(r"bearer\s+[\w.-]+", re.IGNORECASE),
    re.compile(r"(密码|口令|私钥|密钥|令牌)"),
)


def _is_user_trust(trust: MemoryTrustV1) -> bool:
    return trust in (MemoryTrustV1.USER_EXPLICIT, MemoryTrustV1.USER_DERIVED)


def normalize_memory_statement(statement: str) -> str:
    """§8.1：仅用于比较。NFKC + 空白折叠 + 大小写；
    不删除/改写否定、数值、单位、代码、公式、引用 ID。"""

    normalized = unicodedata.normalize("NFKC", statement)
    return " ".join(normalized.split()).casefold()


def compute_canonical_hash(
    scope: MemoryScopeV1,
    kind: MemoryKindV1,
    canonical_key: str | None,
    normalized_statement: str,
) -> str:
    """§8.1：SHA256(scope + kind + canonical_key + normalized_statement)，空 key 以空串参与。"""

    payload = f"{scope.value}{kind.value}{canonical_key or ''}{normalized_statement}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clamp_importance(
    trust: MemoryTrustV1,
    kind: MemoryKindV1,
    *,
    explicit: bool,
    proposed: float,
) -> tuple[float, bool]:
    """§7.2.1：把模型 importance 钳制到确定性先验区间，返回 (钳制值, 是否发生钳制)。"""

    if explicit:
        lower, upper = 1.0, 1.0
    elif kind is MemoryKindV1.SUPPORTED_FINDING:
        lower, upper = 0.7, 1.0
    elif _is_user_trust(trust) and kind in (MemoryKindV1.GOAL, MemoryKindV1.CONSTRAINT):
        lower, upper = 0.7, 1.0
    elif _is_user_trust(trust) and kind in (MemoryKindV1.DECISION, MemoryKindV1.PREFERENCE):
        lower, upper = 0.5, 0.9
    else:
        lower, upper = 0.3, 0.7
    clamped = min(max(proposed, lower), upper)
    return clamped, clamped != proposed


def reject_sensitive_content(statement: str) -> bool:
    """§7.2：密码/Token/API Key/私钥/安全策略命中内容直接拒绝（保守失败关闭）。"""

    return any(pattern.search(statement) for pattern in _SENSITIVE_PATTERNS)


def assess_proposal_legality(
    proposal: MemoryProposalV1,
    *,
    explicit: bool,
) -> tuple[MemoryGovernanceViolationV1, ...]:
    """§7.2/§3.2：自动提取只能 CONVERSATION；USER_PRIVATE 需显式；ASSISTANT_DERIVED 只写 TASK；
    SUPPORTED_FINDING 需 EVIDENCE Provenance。返回违反列表（空即合法）。"""

    violations: list[MemoryGovernanceViolationV1] = []
    if proposal.scope is MemoryScopeV1.USER_PRIVATE and not explicit:
        violations.append(
            MemoryGovernanceViolationV1(
                code="MEMORY_SCOPE_PROMOTION_FORBIDDEN",
                field="scope",
                detail="USER_PRIVATE 只接受用户显式记住命令，不从模型推断自动晋升",
            )
        )
    if (
        proposal.trust is MemoryTrustV1.ASSISTANT_DERIVED
        and proposal.scope is not MemoryScopeV1.TASK
    ):
        violations.append(
            MemoryGovernanceViolationV1(
                code="ASSISTANT_DERIVED_SCOPE_FORBIDDEN",
                field="trust",
                detail="ASSISTANT_DERIVED 只能写 TASK",
            )
        )
    if proposal.kind is MemoryKindV1.SUPPORTED_FINDING and not any(
        ref.source_type is ProvenanceSourceTypeV1.EVIDENCE for ref in proposal.provenance
    ):
        violations.append(
            MemoryGovernanceViolationV1(
                code="SUPPORTED_FINDING_REQUIRES_EVIDENCE",
                field="provenance",
                detail="SUPPORTED_FINDING 必须携带 EVIDENCE Provenance",
            )
        )
    return tuple(violations)


def _has_negation(text: str) -> bool:
    return any(marker in text for marker in _NEGATION_MARKERS)


def _number_tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"\d+(?:\.\d+)?", text))


def _differing_fields(left: str, right: str) -> tuple[str, ...]:
    fields = ["statement"]
    if _has_negation(left) != _has_negation(right):
        fields.append("negation")
    if _number_tokens(left) != _number_tokens(right):
        fields.append("number")
    return tuple(fields)


def resolve_duplicate(
    proposal: MemoryProposalV1,
    existing: MemoryRecordV1,
) -> DuplicateResolutionV1:
    """DuplicateResolver 的确定性层（§8.2/§8.2.1）。同 Key 才判 SAME/OVERLAP/CONFLICT；
    无 Key 或不同 Key 仅语义相似，判 UNRELATED。语义级精判属受约束判定模型，留后续切片。"""

    key = proposal.canonical_key
    existing_key = existing.canonical_key
    if key is None or existing_key is None or key != existing_key:
        return DuplicateResolutionV1(
            verdict=DuplicateVerdictV1.UNRELATED,
            differing_fields=(),
            rationale="canonical_key 缺失或不一致，仅语义相似不算重复",
        )
    proposed_norm = normalize_memory_statement(proposal.statement)
    existing_norm = normalize_memory_statement(existing.content)
    if proposed_norm == existing_norm:
        return DuplicateResolutionV1(
            verdict=DuplicateVerdictV1.SAME,
            differing_fields=(),
            rationale="同 canonical_key 且规范化值等价",
        )
    differing = _differing_fields(proposed_norm, existing_norm)
    if proposed_norm in existing_norm or existing_norm in proposed_norm:
        return DuplicateResolutionV1(
            verdict=DuplicateVerdictV1.OVERLAP,
            differing_fields=differing,
            rationale="同 Key 一方信息更全且无矛盾",
        )
    return DuplicateResolutionV1(
        verdict=DuplicateVerdictV1.CONFLICT,
        differing_fields=differing,
        rationale="同 Key 数值/否定/时间/实体不一致，保留双方进冲突组",
    )


def plan_correction_or_conflict(
    proposal: MemoryProposalV1,
    existing: MemoryRecordV1,
) -> CorrectionPlanV1:
    """§7.4：纠正覆盖 / 同 Key 合并 / 冲突组。用户显式优先；助手不能覆盖用户。"""

    correction = proposal.correction_of is not None and proposal.correction_of == existing.id
    same_key = (
        proposal.canonical_key is not None and proposal.canonical_key == existing.canonical_key
    )
    if not correction and not same_key:
        return CorrectionPlanV1(disposition=CorrectionDispositionV1.NEW)

    user_rewrite = (
        same_key
        and proposal.trust is MemoryTrustV1.USER_EXPLICIT
        and normalize_memory_statement(proposal.statement)
        != normalize_memory_statement(existing.content)
    )
    if correction or user_rewrite:
        if _TRUST_RANK[proposal.trust] >= _TRUST_RANK[existing.trust]:
            return CorrectionPlanV1(
                disposition=CorrectionDispositionV1.CORRECT,
                supersedes_id=existing.id,
            )
        return CorrectionPlanV1(
            disposition=CorrectionDispositionV1.CONFLICT,
            conflict_group_id=existing.conflict_group_id,
        )

    if normalize_memory_statement(proposal.statement) == normalize_memory_statement(
        existing.content
    ):
        return CorrectionPlanV1(
            disposition=CorrectionDispositionV1.MERGE,
            append_provenance=True,
        )
    return CorrectionPlanV1(
        disposition=CorrectionDispositionV1.CONFLICT,
        conflict_group_id=existing.conflict_group_id,
    )


_LEGAL_TRANSITIONS: dict[MemoryStateV1, frozenset[MemoryStateV1]] = {
    MemoryStateV1.PROPOSED: frozenset({MemoryStateV1.ACTIVE, MemoryStateV1.QUARANTINED}),
    MemoryStateV1.ACTIVE: frozenset(
        {
            MemoryStateV1.SUPERSEDED,
            MemoryStateV1.RETRACTED,
            MemoryStateV1.EXPIRED,
            MemoryStateV1.INVALIDATED,
        }
    ),
    MemoryStateV1.QUARANTINED: frozenset(
        {MemoryStateV1.ACTIVE, MemoryStateV1.SUPERSEDED, MemoryStateV1.RETRACTED}
    ),
    MemoryStateV1.SUPERSEDED: frozenset(),
    MemoryStateV1.RETRACTED: frozenset(),
    MemoryStateV1.EXPIRED: frozenset(),
    MemoryStateV1.INVALIDATED: frozenset(),
}


def is_legal_state_transition(current: MemoryStateV1, next_: MemoryStateV1) -> bool:
    """§7.5/§4.1：终态（SUPERSEDED/RETRACTED/EXPIRED/INVALIDATED）不可再迁移；自迁移非法。"""

    return next_ in _LEGAL_TRANSITIONS.get(current, frozenset())


_VERSION_BUMP_KINDS: frozenset[MemoryChangeKindV1] = frozenset(
    {
        MemoryChangeKindV1.ACTIVATED,
        MemoryChangeKindV1.CORRECTED,
        MemoryChangeKindV1.CONFLICT_MARKED,
        MemoryChangeKindV1.PIN_CHANGED,
        MemoryChangeKindV1.SCOPE_CHANGED,
        MemoryChangeKindV1.TRUST_CHANGED,
        MemoryChangeKindV1.VALIDITY_CHANGED,
        MemoryChangeKindV1.INVALIDATED,
        MemoryChangeKindV1.FORGOTTEN,
        MemoryChangeKindV1.PROVENANCE_REORDERED,
    }
)


def should_bump_version(kind: MemoryChangeKindV1) -> bool:
    """§7.4：改变可召回视图才递增所属聚合版本；
    幂等重放/仅 PROPOSED/校验失败/Embedding 重建不递增。"""

    return kind in _VERSION_BUMP_KINDS


__all__ = [
    "assess_proposal_legality",
    "clamp_importance",
    "compute_canonical_hash",
    "is_legal_state_transition",
    "normalize_memory_statement",
    "plan_correction_or_conflict",
    "reject_sensitive_content",
    "resolve_duplicate",
    "should_bump_version",
]
