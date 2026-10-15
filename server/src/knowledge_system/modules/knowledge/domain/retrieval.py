"""DEV-09：检索服务查询与证据契约（零 LLM/零 embedding/零 I/O）。

权威设计：模块07《检索服务模块详细设计》§2（核心模型）/§3.2（QueryAnalyzer）/§5（查询规范化）。
本文件只冻结检索侧的确定性领域契约与纯规则：
- `EvidenceSourceV1` 十 source_kind 判别联合（§2，八模型类：内部 KNOWLEDGE_VERSION 一态、
  任务文件 TASK_* 六态、外部 EXTERNAL_* 三态；外部分支只能由模块11 Capture/Admission 链生成，
  不能成为 `RagPipeline` 召回候选）；
- `CitationLocatorV1` 七 locator 判别联合（§2：PAGE/SECTION/BLOCK/ROW_RANGE/REGION/URL/
  RUN_ARTIFACT，含 §2 校验：SECTION 非空、ROW_RANGE row_to>=row_from 且 Row Group 非负、
  REGION 归一化坐标 0..1 且 x0<x1/y0<y1；Locator 只定位、不授予读取权限）；
- `EvidenceV1`（TEXT evidence_kind）与 `EvidenceSetV1`（结构化/图片证据属模块17 §4 分支，
  留待后续切片）；
- `QueryAnalyzer` 确定性纯函数 `normalize_query`（§5 NFKC+空白压缩，不删除标识含义标点）、
  `classify_query`（§3.2 语言/精确短语/DOI/arXiv/年份/公式编号/缩写/否定/时间限定特征）、
  `lexical_terms`（§5 simple 精确词项 + Phrase 条件；中文分词与英文词干由调用方按与索引相同
  版本的分词器/词干器供给，沿用 token_count 的版本化口径约定）。

本文件不访问模型、网络、embedding、ORM、Temporal 或真实分词器；RAG 流水线召回/融合/重排
（RRF/reranker/parent 扩展）、`evidence_id` HMAC、充分性评估（模块07 §9/§6）、授权范围解析
（§4）均属后续切片。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

import rfc8785
from pydantic import AnyHttpUrl, Field, StringConstraints, model_validator

from knowledge_system.modules.tasking.domain.core_types import Sha256Hex, StrictContractV1

# ---------------------------------------------------------------------------
# EvidenceSourceV1：十 source_kind 判别联合（§2）
# ---------------------------------------------------------------------------


class KnowledgeEvidenceSourceV1(StrictContractV1):
    """§2：内部知识版本来源。"""

    source_kind: Literal["KNOWLEDGE_VERSION"]
    knowledge_version_id: UUID


class TaskToolArtifactEvidenceSourceV1(StrictContractV1):
    """§2：任务工具产物来源（PDF/Markdown 经 tool_artifact_evidence_v1）。"""

    source_kind: Literal["TASK_TOOL_ARTIFACT"]
    corpus_id: UUID
    task_id: UUID
    action_attempt_id: UUID
    grant_id: UUID
    artifact_id: UUID
    source_sha256: Sha256Hex


class TaskAttachmentTextEvidenceSourceV1(StrictContractV1):
    """§2：任务附件文本来源。"""

    source_kind: Literal["TASK_ATTACHMENT_TEXT"]
    corpus_id: UUID
    task_id: UUID
    attachment_binding_id: UUID
    artifact_id: UUID
    source_sha256: Sha256Hex


class TaskStructuredArtifactEvidenceSourceV1(StrictContractV1):
    """§2：任务结构化产物来源（structured_artifact_query_v1）。"""

    source_kind: Literal["TASK_STRUCTURED_ARTIFACT"]
    structured_query_id: UUID
    task_id: UUID
    action_attempt_id: UUID
    grant_id: UUID
    artifact_id: UUID
    source_sha256: Sha256Hex


class TaskAttachmentStructuredEvidenceSourceV1(StrictContractV1):
    """§2：任务附件结构化来源。"""

    source_kind: Literal["TASK_ATTACHMENT_STRUCTURED"]
    structured_query_id: UUID
    task_id: UUID
    attachment_binding_id: UUID
    artifact_id: UUID
    source_sha256: Sha256Hex


class TaskImageArtifactEvidenceSourceV1(StrictContractV1):
    """§2：任务图片产物来源（image_artifact_evidence_v1）。"""

    source_kind: Literal["TASK_IMAGE_ARTIFACT"]
    image_evidence_run_id: UUID
    task_id: UUID
    action_attempt_id: UUID
    grant_id: UUID
    artifact_id: UUID
    source_sha256: Sha256Hex
    normalized_image_sha256: Sha256Hex


class TaskAttachmentImageEvidenceSourceV1(StrictContractV1):
    """§2：任务附件图片来源。"""

    source_kind: Literal["TASK_ATTACHMENT_IMAGE"]
    image_evidence_run_id: UUID
    task_id: UUID
    attachment_binding_id: UUID
    artifact_id: UUID
    source_sha256: Sha256Hex
    normalized_image_sha256: Sha256Hex


class ExternalEvidenceSourceV1(StrictContractV1):
    """§2：外部来源（仅模块11 Capture/Admission 链生成，不进入 RagPipeline 召回候选）。"""

    source_kind: Literal["EXTERNAL_WEB", "EXTERNAL_SCHOLARLY_RECORD", "EXTERNAL_CODE"]
    external_capture_id: UUID
    task_id: UUID


EvidenceSourceV1 = Annotated[
    KnowledgeEvidenceSourceV1
    | TaskToolArtifactEvidenceSourceV1
    | TaskAttachmentTextEvidenceSourceV1
    | TaskStructuredArtifactEvidenceSourceV1
    | TaskAttachmentStructuredEvidenceSourceV1
    | TaskImageArtifactEvidenceSourceV1
    | TaskAttachmentImageEvidenceSourceV1
    | ExternalEvidenceSourceV1,
    Field(discriminator="source_kind"),
]

# ---------------------------------------------------------------------------
# CitationLocatorV1：七 locator 判别联合（§2）
# ---------------------------------------------------------------------------

_PathSegment = Annotated[str, StringConstraints(min_length=1, max_length=256)]
_RowGroup = Annotated[int, Field(ge=0)]


class PageCitationLocatorV1(StrictContractV1):
    """§2：页定位。"""

    kind: Literal["PAGE"]
    page_no: int = Field(ge=1)


class SectionCitationLocatorV1(StrictContractV1):
    """§2：章节路径定位（非空，每段 1..256）。"""

    kind: Literal["SECTION"]
    section_path: tuple[_PathSegment, ...] = Field(min_length=1)


class BlockCitationLocatorV1(StrictContractV1):
    """§2：块定位（block_id 为 CanonicalDocument 稳定 Block ID）。"""

    kind: Literal["BLOCK"]
    block_id: str


class RowRangeCitationLocatorV1(StrictContractV1):
    """§2：CSV/JSONL 行范围或 Parquet Row Group 定位（row_to>=row_from，Row Group 非负）。"""

    kind: Literal["ROW_RANGE"]
    row_from: int = Field(ge=1)
    row_to: int = Field(ge=1)
    parquet_row_groups: tuple[_RowGroup, ...] = ()

    @model_validator(mode="after")
    def _validate_row_range(self) -> RowRangeCitationLocatorV1:
        if self.row_to < self.row_from:
            raise ValueError("ROW_RANGE_INVALID")
        return self


class RegionCitationLocatorV1(StrictContractV1):
    """§2：图片区域定位（归一化 bbox 四坐标 0..1 且 x0<x1、y0<y1）。"""

    kind: Literal["REGION"]
    region_id: UUID
    bbox: tuple[float, float, float, float]

    @model_validator(mode="after")
    def _validate_bbox(self) -> RegionCitationLocatorV1:
        x0, y0, x1, y1 = self.bbox
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise ValueError("REGION_BBOX_INVALID")
        return self


class UrlCitationLocatorV1(StrictContractV1):
    """§2：URL 定位（仅 HTTPS 类 URL；accessed_at 为访问时间事实）。"""

    kind: Literal["URL"]
    canonical_url: AnyHttpUrl
    accessed_at: datetime


class RunArtifactCitationLocatorV1(StrictContractV1):
    """§2：运行产物定位。"""

    kind: Literal["RUN_ARTIFACT"]
    run_id: UUID
    artifact_id: UUID


CitationLocatorV1 = Annotated[
    PageCitationLocatorV1
    | SectionCitationLocatorV1
    | BlockCitationLocatorV1
    | RowRangeCitationLocatorV1
    | RegionCitationLocatorV1
    | UrlCitationLocatorV1
    | RunArtifactCitationLocatorV1,
    Field(discriminator="kind"),
]

# ---------------------------------------------------------------------------
# Evidence / EvidenceSet（§2，TEXT 分支；结构化/图片证据属模块17 §4 分支留待后续）
# ---------------------------------------------------------------------------


class EvidenceRepresentationV1(StrEnum):
    """§2：证据表示级别（FULL/CROPPED/STRUCTURED_SUMMARY）。"""

    FULL = "FULL"
    CROPPED = "CROPPED"
    STRUCTURED_SUMMARY = "STRUCTURED_SUMMARY"


class ScoreBreakdownV1(StrictContractV1):
    """§2 最小冻结子集：融合与重排两个分数阶段；词法/向量原始分在设计 Candidate 层。"""

    fusion_score: float | None = None
    rerank_score: float | None = None


class CitationRefV1(StrictContractV1):
    """§2：证据引用（定位器只定位、不授予读取权限）。

    完整溯源（Artifact SHA / Block ID / 页码 / Section Path）由 `Evidence.source` 与
    `locator` 共同承载，展开时仍按来源、版本、Digest 与当前 ACL 复核（§2）。
    """

    locator: CitationLocatorV1


class EvidenceV1(StrictContractV1):
    """§2：文本证据。`content_hash` 为 text 的 SHA-256；`token_count` 由调用方按版本化
    分词器供给（沿用 token_count 口径）；`evidence_id` 的 HMAC 生成属后续切片，本切片只冻结字段。"""

    evidence_kind: Literal["TEXT"]
    evidence_id: str
    source: EvidenceSourceV1
    parent_chunk_id: str
    child_chunk_ids: tuple[str, ...]
    text: str
    content_hash: Sha256Hex
    token_count: int = Field(ge=0)
    representation: EvidenceRepresentationV1
    citations: tuple[CitationRefV1, ...] = ()
    scores: ScoreBreakdownV1


class EvidenceSufficiencyV1(StrEnum):
    """§9：证据充分性诊断（评估器属后续切片，本切片只冻结枚举）。"""

    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTING = "CONFLICTING"


class EvidenceSetV1(StrictContractV1):
    """§2：证据集。`evidences` 本切片只含 TEXT 证据；结构化/图片证据分支留待后续切片。

    `degradation_flags` 以不可变 tuple 承载（设计 `set[str]`，此处对齐代码库不可变集合惯例）。
    """

    retrieval_run_id: UUID
    normalized_query: str
    evidences: tuple[EvidenceV1, ...]
    sufficiency: EvidenceSufficiencyV1
    degradation_flags: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# QueryAnalyzer（§3.2/§5，确定性纯规则）
# ---------------------------------------------------------------------------


class QueryLanguageV1(StrEnum):
    """§5：查询语言（用于选择检索通道；精确语言识别属调用方职责）。"""

    ZH = "ZH"
    EN = "EN"
    MIXED = "MIXED"


class NormalizedQueryV1(StrictContractV1):
    """§5：规范化查询（保留原始查询与规范化文本）。"""

    original: str
    normalized_text: str


class QueryFeaturesV1(StrictContractV1):
    """§3.2：查询特征（确定性抽取，不改变用户问题语义）。"""

    language: QueryLanguageV1
    exact_phrases: tuple[str, ...] = ()
    dois: tuple[str, ...] = ()
    arxiv_ids: tuple[str, ...] = ()
    years: tuple[int, ...] = ()
    formula_numbers: tuple[str, ...] = ()
    abbreviations: tuple[str, ...] = ()
    negated: bool = False
    time_qualified: bool = False


class LexicalQueryV1(StrictContractV1):
    """§5：词法查询。`chinese_terms`/`english_stems` 由调用方按与索引相同版本的分词器/词干器
    供给；`simple_terms`（lowercase 精确词项）与 `phrase_conditions`（引号短语）在此确定性抽取。"""

    simple_terms: tuple[str, ...] = ()
    phrase_conditions: tuple[str, ...] = ()
    chinese_terms: tuple[str, ...] = ()
    english_stems: tuple[str, ...] = ()


_CJK = re.compile(r"[一-鿿]")
_LATIN = re.compile(r"[A-Za-z]")
_QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”|「([^」]+)」')
_DOI = re.compile(r"\b10\.\d{4,9}/[A-Za-z0-9._;()/+-]+")
_ARXIV = re.compile(r"\b(?:arXiv:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?\b")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b(?!\.\d)")
_FORMULA = re.compile(r"(?:式|公式|Eq\.?|Equation|equation)\s*\(?(\d+(?:\.\d+)?)\)?")
_ABBREV = re.compile(r"\b[A-Z]{2,}\b")
_NEGATION = re.compile(
    r"不|非|无|未|没有|排除|not\b|\bno\b|without|exclude", re.IGNORECASE
)
_TIME_QUAL = re.compile(
    r"近|最近|过去|去年|今年|以来|之内|之后|之前|recent|recently|since|after|before|"
    r"last year|past year|in the last",
    re.IGNORECASE,
)


def _normalize_query_text(text: str) -> str:
    """§5：NFKC + 控制字符清理 + 空白压缩；保留大小写与标识含义标点（不删除公式/标点）。"""

    normalized = unicodedata.normalize("NFKC", text)
    cleaned = "".join(
        ch for ch in normalized if ch in "\n\t" or not unicodedata.category(ch).startswith("C")
    )
    return " ".join(cleaned.split())


def _dedup(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)


def _extract_quoted(text: str) -> tuple[str, ...]:
    contents: list[str] = []
    for match in _QUOTED.finditer(text):
        content = next(group for group in match.groups() if group is not None)
        if content.strip():
            contents.append(content.strip())
    return _dedup(contents)


def normalize_query(query: str) -> NormalizedQueryV1:
    """§5：查询规范化——NFKC、空白压缩、控制字符清理，保留原始查询与大小写。"""

    return NormalizedQueryV1(original=query, normalized_text=_normalize_query_text(query))


def classify_query(normalized: NormalizedQueryV1) -> QueryFeaturesV1:
    """§3.2：确定性抽取查询特征（语言/精确短语/DOI/arXiv/年份/公式编号/缩写/否定/时间限定）。

    年份提取排除紧跟 `.`+数字（避免误取 arXiv 号年份段）；精确语言识别与语义解析属调用方职责。
    """

    text = normalized.normalized_text
    has_cjk = bool(_CJK.search(text))
    has_latin = bool(_LATIN.search(text))
    if has_cjk and has_latin:
        language = QueryLanguageV1.MIXED
    elif has_cjk:
        language = QueryLanguageV1.ZH
    else:
        language = QueryLanguageV1.EN

    return QueryFeaturesV1(
        language=language,
        exact_phrases=_extract_quoted(text),
        dois=_dedup(_DOI.findall(text)),
        arxiv_ids=_dedup(_ARXIV.findall(text)),
        years=tuple(sorted({int(year) for year in _YEAR.findall(text)})),
        formula_numbers=_dedup(_FORMULA.findall(text)),
        abbreviations=_dedup(_ABBREV.findall(text)),
        negated=bool(_NEGATION.search(text)),
        time_qualified=bool(_TIME_QUAL.search(text)),
    )


def lexical_terms(
    normalized: NormalizedQueryV1,
    *,
    chinese_terms: tuple[str, ...] = (),
    english_stems: tuple[str, ...] = (),
) -> LexicalQueryV1:
    """§5：词法查询——simple 精确词项 + Phrase 条件确定性抽取，中文/英文词干由调用方供给。"""

    text = normalized.normalized_text
    simple: list[str] = []
    for token in re.split(r"[^\w]+", text, flags=re.UNICODE):
        if token:
            simple.append(token.lower())
    return LexicalQueryV1(
        simple_terms=_dedup(simple),
        phrase_conditions=_extract_quoted(text),
        chinese_terms=tuple(chinese_terms),
        english_stems=tuple(english_stems),
    )


# ---------------------------------------------------------------------------
# Candidate 与候选融合（§3.4/§6，确定性纯规则）
# ---------------------------------------------------------------------------


class CandidateSourceV1(StrEnum):
    """§3.3 召回通道：Identifier / Lexical / Vector。"""

    IDENTIFIER = "IDENTIFIER"
    LEXICAL = "LEXICAL"
    VECTOR = "VECTOR"


class CandidateV1(StrictContractV1):
    """§2/§6 召回候选：`source` 为最高排位通道；`lexical_score`/`vector_score` 保留各通道原始分。

    `text_hash`/`parent_chunk_id` 供 §6.3 去重（相同文本、同一 Parent 高重叠子块）；均可在未接入
    检索基础设施前省略。
    """

    chunk_id: str
    source: CandidateSourceV1
    lexical_score: float | None = None
    vector_score: float | None = None
    fusion_score: float | None = None
    rerank_score: float | None = None
    text_hash: Sha256Hex | None = None
    parent_chunk_id: str | None = None


def rrf(result_lists: list[list[CandidateV1]], *, k: int = 60) -> list[CandidateV1]:
    """§6.2 倒数排名融合：`RRF(d) = Σ 1 / (k + rank_i(d))`，rank 从 1 起，k 默认 60。

    相同 `chunk_id` 合并来源与原始分（各分数字段取首个非 None），`fusion_score` 为累加 RRF 分；
    结果按 `fusion_score` 降序稳定排序（并列保持首次出现顺序）。每个列表内 `chunk_id` 假定唯一
    （召回器已按 chunk 去重）。
    """

    if k <= 0:
        raise ValueError("RRF_K_POSITIVE")

    order: list[str] = []
    fusion_acc: dict[str, float] = {}
    lexical: dict[str, float | None] = {}
    vector: dict[str, float | None] = {}
    source: dict[str, CandidateSourceV1] = {}
    text_hash: dict[str, Sha256Hex | None] = {}
    parent: dict[str, str | None] = {}

    for result_list in result_lists:
        for rank, candidate in enumerate(result_list):
            contribution = 1.0 / (k + rank + 1)
            cid = candidate.chunk_id
            if cid not in fusion_acc:
                order.append(cid)
                fusion_acc[cid] = 0.0
                lexical[cid] = candidate.lexical_score
                vector[cid] = candidate.vector_score
                source[cid] = candidate.source
                text_hash[cid] = candidate.text_hash
                parent[cid] = candidate.parent_chunk_id
            fusion_acc[cid] += contribution
            if lexical[cid] is None:
                lexical[cid] = candidate.lexical_score
            if vector[cid] is None:
                vector[cid] = candidate.vector_score
            if text_hash[cid] is None:
                text_hash[cid] = candidate.text_hash
            if parent[cid] is None:
                parent[cid] = candidate.parent_chunk_id

    merged = [
        CandidateV1(
            chunk_id=cid,
            source=source[cid],
            lexical_score=lexical[cid],
            vector_score=vector[cid],
            fusion_score=fusion_acc[cid],
            rerank_score=None,
            text_hash=text_hash[cid],
            parent_chunk_id=parent[cid],
        )
        for cid in order
    ]
    merged.sort(key=lambda c: c.fusion_score if c.fusion_score is not None else 0.0, reverse=True)
    return merged


def _dedup_score(candidate: CandidateV1) -> float:
    """去重时的代表分数：优先 rerank/fusion，其次词法/向量，取首个可用最高分。"""

    scores = [
        score
        for score in (
            candidate.rerank_score,
            candidate.fusion_score,
            candidate.lexical_score,
            candidate.vector_score,
        )
        if score is not None
    ]
    return max(scores) if scores else float("-inf")


def dedup_candidates(candidates: list[CandidateV1]) -> list[CandidateV1]:
    """§6.3 去重：先按相同 `chunk_id` 保最高分，再按相同 `text_hash` 保最高分；保持原相对顺序。

    同一 Parent 高重叠子块的合并需要子块命中片段与文本重叠度量（CALIBRATION_REQUIRED），本切片
    仅冻结前两级确定性去重，Parent 高重叠合并留待检索基础设施携带命中片段后实现。
    """

    # 第一级：chunk_id 去重（dict 保持首次出现顺序，替换不改变键位）。
    seen: dict[str, CandidateV1] = {}
    for candidate in candidates:
        existing = seen.get(candidate.chunk_id)
        if existing is None or _dedup_score(candidate) > _dedup_score(existing):
            seen[candidate.chunk_id] = candidate
    unique = list(seen.values())

    # 第二级：text_hash 去重（相同文本不同 chunk_id 只保留最高分）。
    by_hash: dict[str, CandidateV1] = {}
    for candidate in unique:
        if candidate.text_hash is None:
            continue
        existing = by_hash.get(candidate.text_hash)
        if existing is None or _dedup_score(candidate) > _dedup_score(existing):
            by_hash[candidate.text_hash] = candidate
    kept_ids = {candidate.chunk_id for candidate in by_hash.values()}
    return [
        candidate
        for candidate in unique
        if candidate.text_hash is None or candidate.chunk_id in kept_ids
    ]


# ---------------------------------------------------------------------------
# 证据 ID 与充分性（§9/§15，确定性纯规则）
# ---------------------------------------------------------------------------


def compute_evidence_id(
    *,
    server_key: bytes,
    retrieval_run_id: UUID,
    knowledge_version_id: UUID,
    ordered_chunk_ids: tuple[str, ...],
    content_hash: Sha256Hex,
) -> str:
    """§9 证据 ID：`HMAC(server_key, retrieval_run_id + knowledge_version_id +
    ordered_chunk_ids + content_hash)`，按 RFC 8785 规范 JSON 序列化四元组后做 HMAC-SHA256。

    模型只能引用 EvidenceSet 中的 ID，不得自造文件名/页码。`server_key` 由调用方从 SecretRef 注入
    （不落地本模块）。TASK_*/EXTERNAL 来源的身份分量（corpus/artifact/capture）留待后续切片接入。
    """

    if not server_key:
        raise ValueError("SERVER_KEY_REQUIRED")
    payload = rfc8785.dumps(
        {
            "retrieval_run_id": str(retrieval_run_id),
            "knowledge_version_id": str(knowledge_version_id),
            "ordered_chunk_ids": list(ordered_chunk_ids),
            "content_hash": content_hash,
        }
    )
    return hmac.new(server_key, payload, hashlib.sha256).hexdigest()


class SufficiencySignalsV1(StrictContractV1):
    """§9 充分性确定性特征（调用方从 EvidenceSet + QueryFeatures + 降级标志聚合）。"""

    result_count: int = Field(ge=0)
    top_score: float | None = None
    top1_top2_gap: float | None = Field(default=None, ge=0.0)
    required_key_entities: int = Field(default=0, ge=0)
    covered_key_entities: int = Field(default=0, ge=0)
    sub_question_count: int = Field(default=0, ge=0)
    covered_sub_questions: int = Field(default=0, ge=0)
    conflicting: bool = False
    single_weak_source: bool = False
    degraded: bool = False

    @model_validator(mode="after")
    def _coverage_bounds(self) -> SufficiencySignalsV1:
        if self.covered_key_entities > self.required_key_entities:
            raise ValueError("KEY_ENTITY_COVERAGE_EXCEEDS_REQUIRED")
        if self.covered_sub_questions > self.sub_question_count:
            raise ValueError("SUB_QUESTION_COVERAGE_EXCEEDS_TOTAL")
        return self


class SufficiencyPolicyV1(StrictContractV1):
    """§9/§15 充分性阈值；CALIBRATION_REQUIRED（与 DEV-08 `compute_context_budget` 同口径）。"""

    min_key_entity_coverage: float = Field(ge=0.0, le=1.0)
    min_sub_question_coverage: float = Field(ge=0.0, le=1.0)
    min_top_score: float


def evaluate_sufficiency(
    signals: SufficiencySignalsV1, policy: SufficiencyPolicyV1
) -> EvidenceSufficiencyV1:
    """§9 确定性充分性评估：冲突优先；零结果→INSUFFICIENT；覆盖/分数/来源强度全达标→SUFFICIENT，
    部分达标→PARTIAL，否则→INSUFFICIENT。`top1_top2_gap` 仅作特征（§15），不参与硬门禁。

    单一弱来源或发生降级时最多判 PARTIAL，不判 SUFFICIENT。仅作检索诊断，不决定回答/任务终态。
    """

    if signals.conflicting:
        return EvidenceSufficiencyV1.CONFLICTING
    if signals.result_count == 0:
        return EvidenceSufficiencyV1.INSUFFICIENT

    if signals.required_key_entities > 0:
        key_ok = (
            signals.covered_key_entities / signals.required_key_entities
            >= policy.min_key_entity_coverage
        )
    else:
        key_ok = True
    if signals.sub_question_count > 0:
        sub_ok = (
            signals.covered_sub_questions / signals.sub_question_count
            >= policy.min_sub_question_coverage
        )
    else:
        sub_ok = True
    score_ok = signals.top_score is not None and signals.top_score >= policy.min_top_score
    strong = not signals.single_weak_source and not signals.degraded

    if key_ok and sub_ok and score_ok and strong:
        return EvidenceSufficiencyV1.SUFFICIENT
    if key_ok or sub_ok or score_ok:
        return EvidenceSufficiencyV1.PARTIAL
    return EvidenceSufficiencyV1.INSUFFICIENT


class QueryProfileV1(StrEnum):
    """§15 查询画像：决定检索/扩展链路（结构邻接 / 目标覆盖对齐 / 多样性聚类 / 实体引用表键关系）。

    画像判定由调用方（Scope 元数据/语义分析）供给，本切片仅冻结枚举契约。
    """

    SINGLE_DOCUMENT = "SINGLE_DOCUMENT"
    CROSS_DOCUMENT = "CROSS_DOCUMENT"
    CORPUS_THEME = "CORPUS_THEME"
    RELATIONAL = "RELATIONAL"


__all__ = [
    "BlockCitationLocatorV1",
    "CandidateSourceV1",
    "CandidateV1",
    "CitationLocatorV1",
    "CitationRefV1",
    "EvidenceRepresentationV1",
    "EvidenceSetV1",
    "EvidenceSourceV1",
    "EvidenceSufficiencyV1",
    "EvidenceV1",
    "ExternalEvidenceSourceV1",
    "KnowledgeEvidenceSourceV1",
    "LexicalQueryV1",
    "NormalizedQueryV1",
    "PageCitationLocatorV1",
    "QueryFeaturesV1",
    "QueryLanguageV1",
    "QueryProfileV1",
    "RegionCitationLocatorV1",
    "RowRangeCitationLocatorV1",
    "RunArtifactCitationLocatorV1",
    "ScoreBreakdownV1",
    "SectionCitationLocatorV1",
    "SufficiencyPolicyV1",
    "SufficiencySignalsV1",
    "TaskAttachmentImageEvidenceSourceV1",
    "TaskAttachmentStructuredEvidenceSourceV1",
    "TaskAttachmentTextEvidenceSourceV1",
    "TaskImageArtifactEvidenceSourceV1",
    "TaskStructuredArtifactEvidenceSourceV1",
    "TaskToolArtifactEvidenceSourceV1",
    "UrlCitationLocatorV1",
    "classify_query",
    "compute_evidence_id",
    "dedup_candidates",
    "evaluate_sufficiency",
    "lexical_terms",
    "normalize_query",
    "rrf",
]
