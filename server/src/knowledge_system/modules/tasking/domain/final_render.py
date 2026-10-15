"""AGT-08 §5.34：零 LLM `FinalAnswerRendererV1` 确定性 Markdown 渲染。

`final-answer-markdown-v1` 从已接受候选、Completion Decision、Citation/Artifact 集合与 Output
Contract 确定性生成 UTF-8/LF/NFC Markdown：按结构生成标题、段落、列表、表格、代码与文件清单，
转义文本，插入 `[c:c_001]` Marker，非 COMPLETE 时强制追加本地化「限制与未完成项」。它不改写或
补充任何 Claim，持久 Markdown 不含临时下载 URL（Artifact Link 只输出转义显示名）。

确定性约定：
- 标题为 `# title`，Section 为 `## heading`；段落块把引用 Claim 以单空格连接，事实 Claim 文本后
  追加 ` [c:…]` Marker（仅事实 Claim 拥有 Citation）；
- 列表 `- ` / `1. `，表格首行为表头（无独立表头字段时的唯一确定性选择），代码用反引号围栏
  （围栏长度自适应避免注入），Artifact Link 输出无 URL 的 `- 显示名`；
- 文件清单与「限制与未完成项」为尾部 H2 附录，分别来自已选 Result Artifact 与 gap_reports；
- 转义字符为 `` \\ ` * _ [ ] < > # | ``，整体输出做 NFC + LF 归一，并强制 Answer ≤ 1 MiB。
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, Final, Literal

from pydantic import StringConstraints, TypeAdapter

from .core_types import Sha256Hex, StrictContractV1, canonical_json_sha256
from .final_admission import FinalSupportCatalogEntryV1
from .final_candidate import (
    FinalArtifactLinkBlockV1,
    FinalClaimDraftV1,
    FinalCodeBlockV1,
    FinalListBlockV1,
    FinalParagraphBlockV1,
    FinalSynthesisCandidateV1,
    FinalTableBlockV1,
)
from .final_citation import FinalCitationV1, claim_citation_ids
from .final_gate import CompletionDecisionV1
from .input_contracts import OutputContractV1
from .runtime_decision import FinalOutcomeKindV1

FINAL_ANSWER_RENDERER_PROFILE = "final-answer-markdown-v1"
ANSWER_MAX_BYTES = 1_048_576  # 1 MiB

_MD_ESCAPE = str.maketrans(
    {
        "\\": "\\\\",
        "`": "\\`",
        "*": "\\*",
        "_": "\\_",
        "[": "\\[",
        "]": "\\]",
        "<": "\\<",
        ">": "\\>",
        "#": "\\#",
        "|": "\\|",
    }
)


class FinalAnswerV1(StrictContractV1):
    """不可变 Answer：UTF-8/LF/NFC Markdown 文本 + JCS 摘要。"""

    contract_version: Literal["final_answer_v1"]
    language: Annotated[str, StringConstraints(min_length=1, max_length=35)]
    format: Literal["MARKDOWN"]
    markdown: Annotated[str, StringConstraints(min_length=1, max_length=ANSWER_MAX_BYTES)]
    answer_digest: Sha256Hex = "0" * 64


FINAL_ANSWER_OUTPUT_SCHEMA_VERSION: Final = "final_answer_v1"
FINAL_ANSWER_RENDERER_PROFILE_DIGEST: Final = canonical_json_sha256(
    {
        "profile_name": FINAL_ANSWER_RENDERER_PROFILE,
        "output_schema_version": FINAL_ANSWER_OUTPUT_SCHEMA_VERSION,
        "output_schema": TypeAdapter(FinalAnswerV1).json_schema(),
    }
)


def _escape_markdown(text: str) -> str:
    return text.translate(_MD_ESCAPE)


def _heading(kind: Literal["artifacts", "limitations"], language: str) -> str:
    primary = language.split("-")[0].lower()
    if primary == "zh":
        return "结果制品" if kind == "artifacts" else "限制与未完成项"
    return "Result Artifacts" if kind == "artifacts" else "Limitations and Incomplete Items"


def _claim_with_markers(
    claim: FinalClaimDraftV1,
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    citations: tuple[FinalCitationV1, ...],
) -> str:
    text = _escape_markdown(claim.text)
    markers = claim_citation_ids(claim, catalog, citations)
    if markers:
        text += " " + " ".join(f"[c:{citation_id}]" for citation_id in markers)
    return text


def _render_paragraph(
    block: FinalParagraphBlockV1,
    claims_by_id: dict[str, FinalClaimDraftV1],
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    citations: tuple[FinalCitationV1, ...],
) -> str:
    return " ".join(
        _claim_with_markers(claims_by_id[local_id], catalog, citations)
        for local_id in block.claim_local_ids
    )


def _render_list(
    block: FinalListBlockV1,
    claims_by_id: dict[str, FinalClaimDraftV1],
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    citations: tuple[FinalCitationV1, ...],
) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(block.items):
        prefix = f"{index + 1}. " if block.ordered else "- "
        body = " ".join(
            _claim_with_markers(claims_by_id[local_id], catalog, citations) for local_id in item
        )
        lines.append(prefix + body)
    return lines


def _render_table(
    block: FinalTableBlockV1,
    claims_by_id: dict[str, FinalClaimDraftV1],
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    citations: tuple[FinalCitationV1, ...],
) -> list[str]:
    rows = [
        "| "
        + " | ".join(
            " ".join(
                _claim_with_markers(claims_by_id[local_id], catalog, citations) for local_id in cell
            )
            for cell in row
        )
        + " |"
        for row in block.rows
    ]
    if not rows:
        return []
    separator = "| " + " | ".join("---" for _ in range(block.column_count)) + " |"
    return [rows[0], separator, *rows[1:]]


def _code_fence(code: str) -> str:
    fence = "```"
    while fence in code:
        fence += "`"
    return fence


def _render_code(block: FinalCodeBlockV1) -> list[str]:
    fence = _code_fence(block.code)
    return [f"{fence}{block.language}", block.code.strip("\n"), fence]


def _render_markdown(
    candidate: FinalSynthesisCandidateV1,
    decision: CompletionDecisionV1,
    citations: tuple[FinalCitationV1, ...],
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    language: str,
) -> str:
    claims_by_id = {claim.local_claim_id: claim for claim in candidate.claims}
    catalog_by_id = {entry.catalog_id: entry for entry in catalog}
    display_by_catalog = {
        block.catalog_id: block.display_name
        for section in candidate.sections
        for block in section.blocks
        if isinstance(block, FinalArtifactLinkBlockV1)
    }

    parts: list[str] = [f"# {_escape_markdown(candidate.title)}"]
    for section in candidate.sections:
        body: list[str] = []
        for block in section.blocks:
            if isinstance(block, FinalParagraphBlockV1):
                body.append(_render_paragraph(block, claims_by_id, catalog, citations))
            elif isinstance(block, FinalListBlockV1):
                body.extend(_render_list(block, claims_by_id, catalog, citations))
            elif isinstance(block, FinalTableBlockV1):
                body.extend(_render_table(block, claims_by_id, catalog, citations))
            elif isinstance(block, FinalCodeBlockV1):
                body.extend(_render_code(block))
            elif isinstance(block, FinalArtifactLinkBlockV1):
                body.append(f"- {_escape_markdown(block.display_name)}")
        parts.append(f"## {_escape_markdown(section.heading)}")
        parts.append("\n".join(body))

    selected = candidate.selected_result_artifact_catalog_ids
    if selected:
        rows = [
            f"- {_escape_markdown(display_by_catalog.get(catalog_id, catalog_id))}"
            f"（{catalog_by_id[catalog_id].artifact_kind}）"
            for catalog_id in selected
        ]
        parts.append(f"## {_heading('artifacts', language)}")
        parts.append("\n".join(rows))

    if decision.decision != FinalOutcomeKindV1.COMPLETE:
        parts.append(f"## {_heading('limitations', language)}")
        rows = []
        for gap in candidate.gap_reports:
            rows.append(f"- {_escape_markdown(gap.description)}")
            for step in gap.next_steps:
                rows.append(f"  - {_escape_markdown(step)}")
        if rows:
            parts.append("\n".join(rows))

    return "\n\n".join(parts)


def render_final_answer(
    *,
    candidate: FinalSynthesisCandidateV1,
    decision: CompletionDecisionV1,
    citations: tuple[FinalCitationV1, ...],
    catalog: tuple[FinalSupportCatalogEntryV1, ...],
    output_contract: OutputContractV1,
) -> FinalAnswerV1:
    """确定性渲染最终 Answer；仅支持 Markdown Output Contract，失败关闭。"""

    if output_contract.format != "MARKDOWN":
        raise ValueError("FINAL_ANSWER_RENDER_UNSUPPORTED_FORMAT")

    markdown = _render_markdown(candidate, decision, citations, catalog, output_contract.language)
    markdown = unicodedata.normalize("NFC", markdown).replace("\r\n", "\n").replace("\r", "\n")
    if len(markdown.encode("utf-8")) > ANSWER_MAX_BYTES:
        raise ValueError("ANSWER_SIZE_LIMIT_EXCEEDED")

    record = FinalAnswerV1(
        contract_version="final_answer_v1",
        language=output_contract.language,
        format="MARKDOWN",
        markdown=markdown,
        answer_digest="0" * 64,
    )
    digest = canonical_json_sha256(record.model_dump(mode="json", exclude={"answer_digest"}))
    return record.model_copy(update={"answer_digest": digest})


__all__ = [
    "ANSWER_MAX_BYTES",
    "FINAL_ANSWER_RENDERER_PROFILE",
    "FINAL_ANSWER_RENDERER_PROFILE_DIGEST",
    "FinalAnswerV1",
    "render_final_answer",
]
