"""DEV-09：索引构建分块确定性基础（零 LLM/零 embedding/零 I/O）。

权威设计：模块06《索引构建模块详细设计》§2（领域模型/状态机）/§4（结构树构建）/§5（父子分块）/
§5.4（稳定 Chunk ID）/§6（文本规范化）。本文件只冻结：
- `IndexBuildStatusV1` 状态机（§2 CREATED→CHUNKING→EMBEDDING→WRITING→VALIDATING→READY→ACTIVE→
  RETIRED，任一构建阶段可到 FAILED/CANCELLED）与 `is_legal_index_build_transition`；
- `ChunkingConfigV1` 配置基线（§5.1，默认值按设计基线，参数最终由检索评测冻结）；
- `build_document_tree`（§4 按阅读顺序遍历、Heading 章节栈、原子组、参考文献独立 Section、
  页眉页脚不进正文、无标题结构虚拟 Section）；
- `create_parent_chunks`/`create_child_chunks`（§5.2/§5.3 父子分块）；
- `compute_parent_chunk_id`/`compute_child_chunk_id`（§5.4 稳定 Chunk ID）与
  `normalize_chunk_text`（§6 NFKC+空白压缩+控制字符清理，保留大小写原文）。

Token 计数由调用方按版本化分词器口径供给（`ChunkBlockV1.token_count`），精确分词、Embedding、
FTS 词项与 pgvector 持久化均属后续切片；本文件不访问模型、网络、OCR、ORM 或 Temporal。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import Field, model_validator

from knowledge_system.modules.tasking.domain.core_types import (
    Sha256Hex,
    StrictContractV1,
    canonical_json_sha256,
)

from .document import BlockTypeV1, CanonicalBlockV1, CanonicalDocumentV1


class IndexBuildStatusV1(StrEnum):
    """§2 索引构建状态机。ACTIVE 可退役 RETIRED；终态 RETIRED/FAILED/CANCELLED 无出边。"""

    CREATED = "CREATED"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    WRITING = "WRITING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# §2：正向链 + 任一构建阶段可到 FAILED/CANCELLED；READY 可发布 ACTIVE 或退役 RETIRED。
_LEGAL_BUILD_TRANSITIONS: dict[IndexBuildStatusV1, frozenset[IndexBuildStatusV1]] = {
    IndexBuildStatusV1.CREATED: frozenset(
        {IndexBuildStatusV1.CHUNKING, IndexBuildStatusV1.FAILED, IndexBuildStatusV1.CANCELLED}
    ),
    IndexBuildStatusV1.CHUNKING: frozenset(
        {IndexBuildStatusV1.EMBEDDING, IndexBuildStatusV1.FAILED, IndexBuildStatusV1.CANCELLED}
    ),
    IndexBuildStatusV1.EMBEDDING: frozenset(
        {IndexBuildStatusV1.WRITING, IndexBuildStatusV1.FAILED, IndexBuildStatusV1.CANCELLED}
    ),
    IndexBuildStatusV1.WRITING: frozenset(
        {IndexBuildStatusV1.VALIDATING, IndexBuildStatusV1.FAILED, IndexBuildStatusV1.CANCELLED}
    ),
    IndexBuildStatusV1.VALIDATING: frozenset(
        {IndexBuildStatusV1.READY, IndexBuildStatusV1.FAILED, IndexBuildStatusV1.CANCELLED}
    ),
    IndexBuildStatusV1.READY: frozenset(
        {IndexBuildStatusV1.ACTIVE, IndexBuildStatusV1.RETIRED, IndexBuildStatusV1.CANCELLED}
    ),
    IndexBuildStatusV1.ACTIVE: frozenset({IndexBuildStatusV1.RETIRED}),
    IndexBuildStatusV1.RETIRED: frozenset(),
    IndexBuildStatusV1.FAILED: frozenset(),
    IndexBuildStatusV1.CANCELLED: frozenset(),
}


class ChunkingConfigV1(StrictContractV1):
    """§5.1 配置基线。参数最终由检索评测冻结（CALIBRATION_REQUIRED）。"""

    parent_target_tokens: int = Field(default=1200, ge=1)
    parent_max_tokens: int = Field(default=1800, ge=1)
    child_target_tokens: int = Field(default=350, ge=1)
    child_max_tokens: int = Field(default=550, ge=1)
    overlap_tokens: int = Field(default=60, ge=0)
    preserve_tables: bool = True
    preserve_formulas: bool = True

    @model_validator(mode="after")
    def _validate_target_le_max(self) -> ChunkingConfigV1:
        if self.parent_target_tokens > self.parent_max_tokens:
            raise ValueError("PARENT_TARGET_EXCEEDS_MAX")
        if self.child_target_tokens > self.child_max_tokens:
            raise ValueError("CHILD_TARGET_EXCEEDS_MAX")
        return self


class DocumentNodeKindV1(StrEnum):
    """§4 结构树节点类型。"""

    SECTION = "SECTION"
    REFERENCES = "REFERENCES"
    VIRTUAL_SECTION = "VIRTUAL_SECTION"
    ATOMIC_GROUP = "ATOMIC_GROUP"


class DocumentTreeNodeV1(StrictContractV1):
    """§4 结构树节点。`structure_path` 为从文档标题到本节点的规范化标题路径。"""

    kind: DocumentNodeKindV1
    structure_path: tuple[str, ...]
    heading_level: int = Field(ge=0)
    block_ids: tuple[str, ...] = ()
    page_from: int = Field(ge=1)
    page_to: int = Field(ge=1)
    text: str = ""
    children: tuple[DocumentTreeNodeV1, ...] = ()


class DocumentTreeV1(StrictContractV1):
    """§4 结构树。`header_footer_ids` 保存引用定位但默认不进正文索引。"""

    nodes: tuple[DocumentTreeNodeV1, ...]
    header_footer_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ChunkBlockV1(StrictContractV1):
    """分块输入块：`CanonicalBlock` 的 block_id/block_type/text + 调用方按版本化分词器供给的
    `token_count`（精确分词与 Tokenizer 适配属后续切片）。"""

    block_id: str
    block_type: BlockTypeV1
    text: str | None = None
    token_count: int = Field(ge=0)


class ParentChunkDraftV1(StrictContractV1):
    """§5.2 父块草稿。`context_prefix` 为结构路径前缀（标记为 Context，不伪装原文）。"""

    parent_id: str
    structure_path: tuple[str, ...]
    page_from: int = Field(ge=1)
    page_to: int = Field(ge=1)
    block_ids: tuple[str, ...]
    text: str = ""
    token_count: int = Field(ge=0)
    context_prefix: str = ""


class SearchChunkDraftV1(StrictContractV1):
    """§5.3 子块草稿。`normalized_text` 只用于检索字段，不修改引用原文（§6）。"""

    child_id: str
    parent_id: str
    block_refs: tuple[str, ...]
    text: str
    normalized_text: str
    token_count: int = Field(ge=0)
    text_hash: Sha256Hex


# §4.3：表格/公式/图/Caption 形成不可拆原子组。
_ATOMIC_TYPES: frozenset[BlockTypeV1] = frozenset(
    {
        BlockTypeV1.TABLE,
        BlockTypeV1.FORMULA,
        BlockTypeV1.FIGURE,
        BlockTypeV1.CAPTION,
    }
)

# §5.3：单个大段落按句边界切分的确定性占位（精确 Token 级句切分 CALIBRATION_REQUIRED）。
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])\s*")


def is_legal_index_build_transition(
    current: IndexBuildStatusV1, next_: IndexBuildStatusV1
) -> bool:
    """§2 合法状态迁移：正向链单向推进，任一构建阶段可到 FAILED/CANCELLED，READY 可 ACTIVE/RETIRED。

    ACTIVE 仅可退役 RETIRED；终态 RETIRED/FAILED/CANCELLED 不可再迁移；自迁移非法。
    """

    return next_ in _LEGAL_BUILD_TRANSITIONS.get(current, frozenset())


def compute_chunking_config_hash(config: ChunkingConfigV1) -> str:
    """§5.4 `index_config_identity` 的配置分量：分块配置内容寻址（JCS+SHA-256，64-hex）。"""

    return canonical_json_sha256(
        {
            "parent_target_tokens": config.parent_target_tokens,
            "parent_max_tokens": config.parent_max_tokens,
            "child_target_tokens": config.child_target_tokens,
            "child_max_tokens": config.child_max_tokens,
            "overlap_tokens": config.overlap_tokens,
            "preserve_tables": config.preserve_tables,
            "preserve_formulas": config.preserve_formulas,
        }
    )


def compute_parent_chunk_id(
    *,
    index_config_identity: str,
    structure_path: tuple[str, ...],
    ordered_block_ids: tuple[str, ...],
) -> str:
    """§5.4 父块稳定 ID：`hash(index_config_identity + structure_path + ordered_block_ids)`。

    同一 CanonicalDocument 与 Chunker 配置重复构建产生相同 ID；配置变化创建全新 IndexVersion。
    """

    return canonical_json_sha256(
        {
            "index_config_identity": index_config_identity,
            "structure_path": list(structure_path),
            "ordered_block_ids": list(ordered_block_ids),
        }
    )


def compute_child_chunk_id(
    *,
    parent_id: str,
    child_order: int,
    ordered_block_ids: tuple[str, ...],
    normalized_text_hash: Sha256Hex,
) -> str:
    """§5.4 子块稳定 ID：`hash(parent_id + child_order + ordered_block_ids +
    normalized_text_hash)`。
    """

    return canonical_json_sha256(
        {
            "parent_id": parent_id,
            "child_order": child_order,
            "ordered_block_ids": list(ordered_block_ids),
            "normalized_text_hash": normalized_text_hash,
        }
    )


def normalize_chunk_text(text: str) -> str:
    """§6 检索字段规范化：NFKC + 空白压缩 + 控制字符清理，保留大小写原文（另建 lowercase 词项）。"""

    normalized = unicodedata.normalize("NFKC", text)
    cleaned = "".join(
        ch for ch in normalized if ch in "\n\t" or not unicodedata.category(ch).startswith("C")
    )
    return " ".join(cleaned.split())


def _text_hash(normalized_text: str) -> str:
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def _heading_level(block: CanonicalBlockV1) -> int | None:
    """§4.2：TITLE 级别 0；HEADING 级别取自 `attributes["level"]`（缺失默认 1）。"""

    if block.block_type is BlockTypeV1.TITLE:
        return 0
    if block.block_type is BlockTypeV1.HEADING:
        raw = block.attributes.get("level", 1)
        if isinstance(raw, int) and raw >= 1:
            return raw
        return 1
    return None


@dataclass
class _Node:
    kind: DocumentNodeKindV1
    structure_path: tuple[str, ...]
    heading_level: int
    block_ids: list[str] = field(default_factory=list)
    page_from: int = 1
    page_to: int = 1
    text_parts: list[str] = field(default_factory=list)
    children: list[_Node] = field(default_factory=list)


def _freeze(node: _Node) -> DocumentTreeNodeV1:
    return DocumentTreeNodeV1(
        kind=node.kind,
        structure_path=node.structure_path,
        heading_level=node.heading_level,
        block_ids=tuple(node.block_ids),
        page_from=node.page_from,
        page_to=node.page_to,
        text="\n".join(node.text_parts),
        children=tuple(_freeze(child) for child in node.children),
    )


def _attach(root: list[_Node], stack: list[_Node], node: _Node) -> None:
    if stack:
        stack[-1].children.append(node)
    else:
        root.append(node)


def build_document_tree(document: CanonicalDocumentV1) -> DocumentTreeV1:
    """§4 结构树构建：按阅读顺序遍历块，Heading 章节栈、原子组、参考文献独立 Section、页眉页脚
    不进正文、无标题结构按页建虚拟 Section；异常跳级补逻辑节点并记录 Warning。"""

    warnings: list[str] = []
    header_footer_ids: list[str] = []
    reference_blocks: list[tuple[int, CanonicalBlockV1]] = []

    pages = sorted(document.pages, key=lambda p: p.page_no)
    ordered: list[tuple[int, CanonicalBlockV1]] = []
    for page in pages:
        for block in sorted(page.blocks, key=lambda b: b.reading_order):
            ordered.append((page.page_no, block))

    root: list[_Node] = []
    stack: list[_Node] = []
    saw_heading = False
    virtual_by_page: dict[int, _Node] = {}
    atomic_buf: list[tuple[int, CanonicalBlockV1]] = []

    def flush_atomic() -> None:
        nonlocal atomic_buf
        if not atomic_buf:
            return
        ids = [block.block_id for _, block in atomic_buf]
        texts = [block.text or "" for _, block in atomic_buf]
        pages = [page for page, _ in atomic_buf]
        node = _Node(
            kind=DocumentNodeKindV1.ATOMIC_GROUP,
            structure_path=stack[-1].structure_path if stack else (),
            heading_level=0,
            block_ids=ids,
            page_from=min(pages),
            page_to=max(pages),
            text_parts=texts,
        )
        _attach(root, stack, node)
        atomic_buf = []

    for page_no, block in ordered:
        if block.block_type is BlockTypeV1.HEADER_FOOTER:
            flush_atomic()
            header_footer_ids.append(block.block_id)
            continue
        if block.block_type is BlockTypeV1.REFERENCE:
            flush_atomic()
            reference_blocks.append((page_no, block))
            continue
        level = _heading_level(block)
        if level is not None:
            flush_atomic()
            saw_heading = True
            title = normalize_chunk_text(block.text or "")
            while stack and stack[-1].heading_level >= level:
                stack.pop()
            if stack:
                parent = stack[-1]
                if level > parent.heading_level + 1:
                    warnings.append(f"HEADING_LEVEL_SKIP:{parent.heading_level}->{level}")
                node = _Node(
                    kind=DocumentNodeKindV1.SECTION,
                    structure_path=parent.structure_path + (title,),
                    heading_level=level,
                    page_from=page_no,
                    page_to=page_no,
                )
                parent.children.append(node)
            else:
                node = _Node(
                    kind=DocumentNodeKindV1.SECTION,
                    structure_path=(title,),
                    heading_level=level,
                    page_from=page_no,
                    page_to=page_no,
                )
                root.append(node)
            stack.append(node)
            continue
        # 内容块
        if block.block_type in _ATOMIC_TYPES:
            atomic_buf.append((page_no, block))
            continue
        flush_atomic()
        if not saw_heading:
            vnode = virtual_by_page.get(page_no)
            if vnode is None:
                vnode = _Node(
                    kind=DocumentNodeKindV1.VIRTUAL_SECTION,
                    structure_path=(f"virtual-page-{page_no}",),
                    heading_level=0,
                    page_from=page_no,
                    page_to=page_no,
                )
                virtual_by_page[page_no] = vnode
                root.append(vnode)
            vnode.block_ids.append(block.block_id)
            vnode.text_parts.append(block.text or "")
            vnode.page_to = max(vnode.page_to, page_no)
        elif stack:
            cur = stack[-1]
            cur.block_ids.append(block.block_id)
            cur.text_parts.append(block.text or "")
            cur.page_to = max(cur.page_to, page_no)
    flush_atomic()

    if reference_blocks:
        ref_pages = [page for page, _ in reference_blocks]
        ref_node = _Node(
            kind=DocumentNodeKindV1.REFERENCES,
            structure_path=("references",),
            heading_level=0,
            block_ids=[block.block_id for _, block in reference_blocks],
            page_from=min(ref_pages),
            page_to=max(ref_pages),
            text_parts=[block.text or "" for _, block in reference_blocks],
        )
        root.append(ref_node)

    return DocumentTreeV1(
        nodes=tuple(_freeze(node) for node in root),
        header_footer_ids=tuple(header_footer_ids),
        warnings=tuple(warnings),
    )


def _walk_units(nodes: tuple[DocumentTreeNodeV1, ...]) -> list[DocumentTreeNodeV1]:
    """收集父块候选单元：原子组、参考文献 Section、以及各 Section 的直接正文块。"""

    units: list[DocumentTreeNodeV1] = []
    for node in nodes:
        if node.kind in (DocumentNodeKindV1.ATOMIC_GROUP, DocumentNodeKindV1.REFERENCES):
            if node.block_ids:
                units.append(node)
            continue
        if node.block_ids:
            units.append(node)
        units.extend(_walk_units(node.children))
    return units


def _split_groups(
    block_ids: tuple[str, ...],
    blocks: Mapping[str, ChunkBlockV1],
    max_tokens: int,
) -> list[tuple[str, ...]]:
    groups: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for block_id in block_ids:
        tokens = blocks[block_id].token_count
        if current and current_tokens + tokens > max_tokens:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(block_id)
        current_tokens += tokens
    if current:
        groups.append(current)
    return [tuple(group) for group in groups]


def _make_parent(
    node: DocumentTreeNodeV1,
    block_ids: tuple[str, ...],
    *,
    index_config_identity: str,
    blocks: Mapping[str, ChunkBlockV1],
) -> ParentChunkDraftV1:
    return ParentChunkDraftV1(
        parent_id=compute_parent_chunk_id(
            index_config_identity=index_config_identity,
            structure_path=node.structure_path,
            ordered_block_ids=block_ids,
        ),
        structure_path=node.structure_path,
        page_from=node.page_from,
        page_to=node.page_to,
        block_ids=block_ids,
        text="\n".join(blocks[b].text or "" for b in block_ids),
        token_count=sum(blocks[b].token_count for b in block_ids),
        context_prefix=" / ".join(node.structure_path),
    )


def create_parent_chunks(
    tree: DocumentTreeV1,
    *,
    chunker_version: str,
    blocks: Mapping[str, ChunkBlockV1],
    config: ChunkingConfigV1,
) -> tuple[ParentChunkDraftV1, ...]:
    """§5.2 父块：优先一个语义小节为父块；超上限按段落组拆分不跨 Heading；原子组独立成父块。"""

    identity = f"{chunker_version}:{compute_chunking_config_hash(config)}"
    parents: list[ParentChunkDraftV1] = []
    for unit in _walk_units(tree.nodes):
        total = sum(blocks[b].token_count for b in unit.block_ids if b in blocks)
        if unit.kind is DocumentNodeKindV1.ATOMIC_GROUP or total <= config.parent_max_tokens:
            parents.append(
                _make_parent(unit, unit.block_ids, index_config_identity=identity, blocks=blocks)
            )
            continue
        for group in _split_groups(unit.block_ids, blocks, config.parent_max_tokens):
            parents.append(
                _make_parent(unit, group, index_config_identity=identity, blocks=blocks)
            )
    return tuple(parents)


def _split_large_paragraph(
    text: str, total_tokens: int, max_tokens: int
) -> list[tuple[str, int]]:
    """§5.3：单个大段落按句边界切分；token 按字符长度比例分摊（精确 Token 级句切分
    CALIBRATION_REQUIRED）。
    """

    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if not sentences:
        sentences = [text]
    total_chars = sum(len(s) for s in sentences) or 1

    groups: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for sentence in sentences:
        current.append(sentence)
        current_chars += len(sentence)
        estimated = round(total_tokens * current_chars / total_chars)
        if estimated >= max_tokens and len(current) > 1:
            groups.append(current[:-1])
            current = [sentence]
            current_chars = len(sentence)
    if current:
        groups.append(current)

    result: list[tuple[str, int]] = []
    for group in groups:
        group_chars = sum(len(s) for s in group)
        result.append((" ".join(group), round(total_tokens * group_chars / total_chars)))
    return result


def _overlap_suffix(text: str, tokens: int) -> str:
    """§5.3 相邻文本块 overlap 的确定性占位：取前一块末尾 `tokens` 个空白词（无空白按字符）。"""

    if not text or tokens <= 0:
        return ""
    words = text.split()
    if len(words) >= tokens:
        return " ".join(words[-tokens:])
    return text[-tokens:]


def _chunk_children(
    parent: ParentChunkDraftV1,
    ordered: list[ChunkBlockV1],
    config: ChunkingConfigV1,
) -> list[SearchChunkDraftV1]:
    """§5.3 子块：按原子块累积到 target、下一个块使长度超 max 时结束；大段落按句切分；
    表格/公式不截断。
    """

    children: list[SearchChunkDraftV1] = []

    def build(
        block_refs: tuple[str, ...], text: str, token_count: int
    ) -> SearchChunkDraftV1:
        order = len(children)
        if config.overlap_tokens > 0 and order > 0:
            prefix = _overlap_suffix(children[-1].text, config.overlap_tokens)
            if prefix:
                text = f"{prefix} {text}"
                token_count += config.overlap_tokens
        normalized = normalize_chunk_text(text)
        return SearchChunkDraftV1(
            child_id=compute_child_chunk_id(
                parent_id=parent.parent_id,
                child_order=order,
                ordered_block_ids=block_refs,
                normalized_text_hash=_text_hash(normalized),
            ),
            parent_id=parent.parent_id,
            block_refs=block_refs,
            text=text,
            normalized_text=normalized,
            token_count=token_count,
            text_hash=_text_hash(normalized),
        )

    current_ids: list[str] = []
    current_texts: list[str] = []
    current_tokens = 0

    for blk in ordered:
        text = blk.text or ""
        is_atomic = blk.block_type in _ATOMIC_TYPES
        if current_ids and current_tokens + blk.token_count > config.child_max_tokens:
            children.append(
                build(tuple(current_ids), "\n".join(current_texts), current_tokens)
            )
            current_ids, current_texts, current_tokens = [], [], 0

        if blk.token_count > config.child_max_tokens:
            if is_atomic:
                children.append(build((blk.block_id,), text, blk.token_count))
            else:
                for sub_text, sub_tokens in _split_large_paragraph(
                    text, blk.token_count, config.child_max_tokens
                ):
                    children.append(build((blk.block_id,), sub_text, sub_tokens))
            continue

        current_ids.append(blk.block_id)
        current_texts.append(text)
        current_tokens += blk.token_count

    if current_ids:
        children.append(build(tuple(current_ids), "\n".join(current_texts), current_tokens))
    return children


def create_child_chunks(
    parents: tuple[ParentChunkDraftV1, ...],
    *,
    blocks: Mapping[str, ChunkBlockV1],
    config: ChunkingConfigV1,
) -> tuple[SearchChunkDraftV1, ...]:
    """§5.3 子块分块：对每个父块按其有序块累积生成子块，表格/公式不按 Token 截断。"""

    children: list[SearchChunkDraftV1] = []
    for parent in parents:
        ordered = [blocks[b] for b in parent.block_ids if b in blocks]
        children.extend(_chunk_children(parent, ordered, config))
    return tuple(children)


__all__ = [
    "ChunkBlockV1",
    "ChunkingConfigV1",
    "DocumentNodeKindV1",
    "DocumentTreeNodeV1",
    "DocumentTreeV1",
    "IndexBuildStatusV1",
    "ParentChunkDraftV1",
    "SearchChunkDraftV1",
    "build_document_tree",
    "compute_child_chunk_id",
    "compute_chunking_config_hash",
    "compute_parent_chunk_id",
    "create_child_chunks",
    "create_parent_chunks",
    "is_legal_index_build_transition",
    "normalize_chunk_text",
]
