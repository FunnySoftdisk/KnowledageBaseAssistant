"""DEV-09 子批次 3：索引构建分块确定性基础的离线盲测（零 LLM/零 embedding/零 I/O）。"""

from __future__ import annotations

import unittest
from uuid import uuid4

from pydantic import ValidationError

from knowledge_system.modules.knowledge.public import (
    AcademicMetadataV1,
    BlockTypeV1,
    BoundingBoxV1,
    CanonicalBlockV1,
    CanonicalDocumentV1,
    CanonicalPageV1,
    ChunkBlockV1,
    ChunkingConfigV1,
    DocumentNodeKindV1,
    DocumentQualityReportV1,
    DocumentTreeNodeV1,
    DocumentTreeV1,
    IndexBuildStatusV1,
    ParentChunkDraftV1,
    ParserManifestV1,
    SearchChunkDraftV1,
    build_document_tree,
    compute_child_chunk_id,
    compute_chunking_config_hash,
    compute_parent_chunk_id,
    create_child_chunks,
    create_parent_chunks,
    is_legal_index_build_transition,
    normalize_chunk_text,
)

DIGEST = "a" * 64


def _bbox() -> BoundingBoxV1:
    return BoundingBoxV1(x0=0.0, y0=0.0, x1=100.0, y1=20.0)


def _block(
    block_id: str,
    *,
    block_type: BlockTypeV1 = BlockTypeV1.PARAGRAPH,
    text: str = "body",
    reading_order: int = 0,
    attributes: dict[str, object] | None = None,
) -> CanonicalBlockV1:
    return CanonicalBlockV1(
        block_id=block_id,
        block_type=block_type,
        reading_order=reading_order,
        bbox=_bbox(),
        text=text,
        confidence=0.9,
        source_parser="test",
        attributes=attributes or {},
    )


def _heading(
    block_id: str,
    title: str,
    *,
    level: int = 1,
    reading_order: int = 0,
) -> CanonicalBlockV1:
    return _block(
        block_id,
        block_type=BlockTypeV1.HEADING,
        text=title,
        reading_order=reading_order,
        attributes={"level": level},
    )


def _quality() -> DocumentQualityReportV1:
    return DocumentQualityReportV1(
        text_coverage=1.0,
        garble_rate=0.0,
        reading_order=1.0,
        table_completeness=1.0,
        formula_completeness=1.0,
        caption_association=1.0,
        citation_structure=1.0,
        locator_validity=1.0,
        composite_score=1.0,
    )


def _document(*blocks: CanonicalBlockV1) -> CanonicalDocumentV1:
    return CanonicalDocumentV1(
        schema_version=1,
        document_id=uuid4(),
        file_version_id=uuid4(),
        source_sha256=DIGEST,
        parser_manifest=ParserManifestV1(name="test", version="1", config_hash=DIGEST),
        metadata=AcademicMetadataV1(),
        pages=(
            CanonicalPageV1(
                page_no=1, width=612.0, height=792.0, rotation=0, blocks=blocks
            ),
        ),
        quality=_quality(),
    )


def _chunk_block(
    block_id: str,
    *,
    block_type: BlockTypeV1 = BlockTypeV1.PARAGRAPH,
    text: str = "hello",
    tokens: int = 10,
) -> ChunkBlockV1:
    return ChunkBlockV1(
        block_id=block_id, block_type=block_type, text=text, token_count=tokens
    )


def _parent(
    parent_id: str,
    block_ids: tuple[str, ...],
    *,
    structure_path: tuple[str, ...] = ("S",),
    text: str = "text",
    token_count: int = 10,
) -> ParentChunkDraftV1:
    return ParentChunkDraftV1(
        parent_id=parent_id,
        structure_path=structure_path,
        page_from=1,
        page_to=1,
        block_ids=block_ids,
        text=text,
        token_count=token_count,
    )


class IndexBuildStatusV1Tests(unittest.TestCase):
    def test_forward_chain(self) -> None:
        chain = [
            IndexBuildStatusV1.CREATED,
            IndexBuildStatusV1.CHUNKING,
            IndexBuildStatusV1.EMBEDDING,
            IndexBuildStatusV1.WRITING,
            IndexBuildStatusV1.VALIDATING,
            IndexBuildStatusV1.READY,
            IndexBuildStatusV1.ACTIVE,
            IndexBuildStatusV1.RETIRED,
        ]
        for current, next_ in zip(chain, chain[1:], strict=False):
            self.assertTrue(is_legal_index_build_transition(current, next_))

    def test_ready_can_retire_directly(self) -> None:
        self.assertTrue(
            is_legal_index_build_transition(
                IndexBuildStatusV1.READY, IndexBuildStatusV1.RETIRED
            )
        )

    def test_any_build_stage_can_fail_or_cancel(self) -> None:
        for stage in (
            IndexBuildStatusV1.CREATED,
            IndexBuildStatusV1.CHUNKING,
            IndexBuildStatusV1.EMBEDDING,
            IndexBuildStatusV1.WRITING,
            IndexBuildStatusV1.VALIDATING,
        ):
            self.assertTrue(
                is_legal_index_build_transition(stage, IndexBuildStatusV1.FAILED)
            )
            self.assertTrue(
                is_legal_index_build_transition(stage, IndexBuildStatusV1.CANCELLED)
            )

    def test_terminal_has_no_outgoing(self) -> None:
        for terminal in (
            IndexBuildStatusV1.RETIRED,
            IndexBuildStatusV1.FAILED,
            IndexBuildStatusV1.CANCELLED,
        ):
            for other in IndexBuildStatusV1:
                self.assertFalse(is_legal_index_build_transition(terminal, other))

    def test_active_only_retires(self) -> None:
        self.assertTrue(
            is_legal_index_build_transition(
                IndexBuildStatusV1.ACTIVE, IndexBuildStatusV1.RETIRED
            )
        )
        for other in IndexBuildStatusV1:
            if other is not IndexBuildStatusV1.RETIRED:
                self.assertFalse(
                    is_legal_index_build_transition(IndexBuildStatusV1.ACTIVE, other)
                )

    def test_self_transition_illegal(self) -> None:
        for status in IndexBuildStatusV1:
            self.assertFalse(is_legal_index_build_transition(status, status))

    def test_backward_illegal(self) -> None:
        self.assertFalse(
            is_legal_index_build_transition(
                IndexBuildStatusV1.CHUNKING, IndexBuildStatusV1.CREATED
            )
        )


class ChunkingConfigV1Tests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = ChunkingConfigV1()
        self.assertEqual(cfg.parent_target_tokens, 1200)
        self.assertEqual(cfg.parent_max_tokens, 1800)
        self.assertEqual(cfg.child_target_tokens, 350)
        self.assertEqual(cfg.child_max_tokens, 550)
        self.assertEqual(cfg.overlap_tokens, 60)
        self.assertTrue(cfg.preserve_tables)
        self.assertTrue(cfg.preserve_formulas)

    def test_parent_target_exceeds_max_raises(self) -> None:
        with self.assertRaises(ValidationError):
            ChunkingConfigV1(parent_target_tokens=2000, parent_max_tokens=1800)

    def test_child_target_exceeds_max_raises(self) -> None:
        with self.assertRaises(ValidationError):
            ChunkingConfigV1(child_target_tokens=600, child_max_tokens=550)

    def test_equal_target_and_max_ok(self) -> None:
        cfg = ChunkingConfigV1(
            parent_target_tokens=100, parent_max_tokens=100,
            child_target_tokens=100, child_max_tokens=100,
        )
        self.assertEqual(cfg.parent_max_tokens, 100)


class NormalizeChunkTextTests(unittest.TestCase):
    def test_nfkc_fullwidth(self) -> None:
        self.assertEqual(normalize_chunk_text("ＡＢＣ"), "ABC")

    def test_nfkc_ligature(self) -> None:
        self.assertEqual(normalize_chunk_text("ﬁle"), "file")

    def test_whitespace_compression(self) -> None:
        self.assertEqual(normalize_chunk_text("  a   b\n c "), "a b c")

    def test_control_chars_removed(self) -> None:
        self.assertEqual(normalize_chunk_text("a\x00b\x1fc"), "abc")

    def test_case_preserved(self) -> None:
        self.assertEqual(normalize_chunk_text("Hello World"), "Hello World")


class ComputeChunkIdTests(unittest.TestCase):
    def test_parent_id_deterministic(self) -> None:
        args = dict(
            index_config_identity="cfg",
            structure_path=("a", "b"),
            ordered_block_ids=("1", "2"),
        )
        self.assertEqual(compute_parent_chunk_id(**args), compute_parent_chunk_id(**args))

    def test_parent_id_64_hex(self) -> None:
        key = compute_parent_chunk_id(
            index_config_identity="cfg",
            structure_path=(),
            ordered_block_ids=(),
        )
        self.assertRegex(key, r"^[0-9a-f]{64}$")

    def test_parent_id_differs_on_each_component(self) -> None:
        base = dict(
            index_config_identity="cfg",
            structure_path=("a",),
            ordered_block_ids=("1",),
        )
        self.assertNotEqual(
            compute_parent_chunk_id(**base),
            compute_parent_chunk_id(**{**base, "index_config_identity": "cfg2"}),
        )
        self.assertNotEqual(
            compute_parent_chunk_id(**base),
            compute_parent_chunk_id(**{**base, "structure_path": ("a", "b")}),
        )
        self.assertNotEqual(
            compute_parent_chunk_id(**base),
            compute_parent_chunk_id(**{**base, "ordered_block_ids": ("1", "2")}),
        )

    def test_child_id_64_hex(self) -> None:
        key = compute_child_chunk_id(
            parent_id="p",
            child_order=0,
            ordered_block_ids=("1",),
            normalized_text_hash=DIGEST,
        )
        self.assertRegex(key, r"^[0-9a-f]{64}$")

    def test_child_id_differs_on_each_component(self) -> None:
        base = dict(
            parent_id="p",
            child_order=0,
            ordered_block_ids=("1",),
            normalized_text_hash=DIGEST,
        )
        self.assertNotEqual(
            compute_child_chunk_id(**base),
            compute_child_chunk_id(**{**base, "parent_id": "q"}),
        )
        self.assertNotEqual(
            compute_child_chunk_id(**base),
            compute_child_chunk_id(**{**base, "child_order": 1}),
        )
        self.assertNotEqual(
            compute_child_chunk_id(**base),
            compute_child_chunk_id(**{**base, "ordered_block_ids": ("1", "2")}),
        )
        self.assertNotEqual(
            compute_child_chunk_id(**base),
            compute_child_chunk_id(**{**base, "normalized_text_hash": "b" * 64}),
        )

    def test_config_hash_deterministic(self) -> None:
        self.assertEqual(
            compute_chunking_config_hash(ChunkingConfigV1()),
            compute_chunking_config_hash(ChunkingConfigV1()),
        )
        self.assertNotEqual(
            compute_chunking_config_hash(ChunkingConfigV1()),
            compute_chunking_config_hash(
                ChunkingConfigV1(parent_target_tokens=1000)
            ),
        )


class BuildDocumentTreeTests(unittest.TestCase):
    def test_heading_stack(self) -> None:
        tree = build_document_tree(
            _document(
                _heading("h1", "Intro", level=1, reading_order=0),
                _block("p1", text="one", reading_order=1),
                _heading("h2", "Sub", level=2, reading_order=2),
                _block("p2", text="two", reading_order=3),
                _heading("h1b", "Next", level=1, reading_order=4),
                _block("p3", text="three", reading_order=5),
            )
        )
        self.assertEqual([n.structure_path for n in tree.nodes], [("Intro",), ("Next",)])
        intro = tree.nodes[0]
        self.assertEqual(intro.block_ids, ("p1",))
        self.assertEqual(len(intro.children), 1)
        sub = intro.children[0]
        self.assertEqual(sub.structure_path, ("Intro", "Sub"))
        self.assertEqual(sub.block_ids, ("p2",))
        self.assertEqual(tree.nodes[1].block_ids, ("p3",))

    def test_level_skip_warning(self) -> None:
        tree = build_document_tree(
            _document(
                _heading("h1", "A", level=1, reading_order=0),
                _heading("h3", "C", level=3, reading_order=1),
            )
        )
        self.assertIn("HEADING_LEVEL_SKIP:1->3", tree.warnings)

    def test_atomic_group(self) -> None:
        tree = build_document_tree(
            _document(
                _heading("h1", "S", level=1, reading_order=0),
                _block("p1", text="a", reading_order=1),
                _block("t1", block_type=BlockTypeV1.TABLE, reading_order=2),
                _block("c1", block_type=BlockTypeV1.CAPTION, reading_order=3),
                _block("p2", text="b", reading_order=4),
            )
        )
        section = tree.nodes[0]
        self.assertEqual(section.block_ids, ("p1", "p2"))
        self.assertEqual(len(section.children), 1)
        atomic = section.children[0]
        self.assertEqual(atomic.kind, DocumentNodeKindV1.ATOMIC_GROUP)
        self.assertEqual(atomic.block_ids, ("t1", "c1"))

    def test_references_section(self) -> None:
        tree = build_document_tree(
            _document(
                _heading("h1", "S", level=1, reading_order=0),
                _block("p1", text="a", reading_order=1),
                _block("r1", block_type=BlockTypeV1.REFERENCE, reading_order=2),
                _block("r2", block_type=BlockTypeV1.REFERENCE, reading_order=3),
            )
        )
        self.assertEqual(tree.nodes[0].structure_path, ("S",))
        refs = tree.nodes[1]
        self.assertEqual(refs.kind, DocumentNodeKindV1.REFERENCES)
        self.assertEqual(refs.block_ids, ("r1", "r2"))

    def test_header_footer_excluded(self) -> None:
        tree = build_document_tree(
            _document(
                _heading("h1", "S", level=1, reading_order=0),
                _block("p1", text="a", reading_order=1),
                _block("hf1", block_type=BlockTypeV1.HEADER_FOOTER, reading_order=2),
                _block("p2", text="b", reading_order=3),
            )
        )
        self.assertEqual(tree.nodes[0].block_ids, ("p1", "p2"))
        self.assertEqual(tree.header_footer_ids, ("hf1",))

    def test_virtual_section_without_heading(self) -> None:
        tree = build_document_tree(
            _document(
                _block("p1", text="a", reading_order=0),
                _block("p2", text="b", reading_order=1),
            )
        )
        self.assertEqual(len(tree.nodes), 1)
        self.assertEqual(tree.nodes[0].kind, DocumentNodeKindV1.VIRTUAL_SECTION)
        self.assertEqual(tree.nodes[0].structure_path, ("virtual-page-1",))
        self.assertEqual(tree.nodes[0].block_ids, ("p1", "p2"))


class CreateParentChunksTests(unittest.TestCase):
    def _blocks(self, **overrides: object) -> dict[str, ChunkBlockV1]:
        blocks: dict[str, ChunkBlockV1] = {}
        for block_id, tokens in overrides.items():
            blocks[block_id] = _chunk_block(block_id, tokens=int(tokens))
        return blocks

    def test_section_becomes_single_parent(self) -> None:
        tree = build_document_tree(
            _document(
                _heading("h1", "S", level=1, reading_order=0),
                _block("p1", text="a", reading_order=1),
                _block("p2", text="b", reading_order=2),
            )
        )
        blocks = self._blocks(p1=10, p2=10)
        parents = create_parent_chunks(
            tree, chunker_version="v1", blocks=blocks, config=ChunkingConfigV1()
        )
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0].structure_path, ("S",))
        self.assertEqual(parents[0].block_ids, ("p1", "p2"))

    def test_over_max_splits_by_group(self) -> None:
        tree = build_document_tree(
            _document(
                _heading("h1", "S", level=1, reading_order=0),
                _block("p1", text="a", reading_order=1),
                _block("p2", text="b", reading_order=2),
            )
        )
        blocks = self._blocks(p1=70, p2=70)
        config = ChunkingConfigV1(parent_target_tokens=50, parent_max_tokens=100)
        parents = create_parent_chunks(
            tree, chunker_version="v1", blocks=blocks, config=config
        )
        self.assertEqual(len(parents), 2)
        self.assertEqual([p.block_ids for p in parents], [("p1",), ("p2",)])

    def test_atomic_group_own_parent(self) -> None:
        tree = build_document_tree(
            _document(
                _heading("h1", "S", level=1, reading_order=0),
                _block("t1", block_type=BlockTypeV1.TABLE, reading_order=1),
                _block("c1", block_type=BlockTypeV1.CAPTION, reading_order=2),
            )
        )
        blocks = self._blocks(t1=500, c1=500)
        config = ChunkingConfigV1(parent_target_tokens=50, parent_max_tokens=100)
        parents = create_parent_chunks(
            tree, chunker_version="v1", blocks=blocks, config=config
        )
        # 原子组不按 Token 拆，即使远超 parent_max 也整组独立成父块。
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0].block_ids, ("t1", "c1"))


class CreateChildChunksTests(unittest.TestCase):
    def _config(self, **overrides: object) -> ChunkingConfigV1:
        fields: dict[str, object] = {
            "parent_target_tokens": 100,
            "parent_max_tokens": 100,
            "child_target_tokens": 10,
            "child_max_tokens": 30,
            "overlap_tokens": 0,
        }
        fields.update(overrides)
        return ChunkingConfigV1(**fields)

    def _chunks(
        self,
        parent: ParentChunkDraftV1,
        blocks: dict[str, ChunkBlockV1],
        config: ChunkingConfigV1,
    ) -> tuple[SearchChunkDraftV1, ...]:
        return create_child_chunks((parent,), blocks=blocks, config=config)

    def test_accumulation_until_max(self) -> None:
        blocks = {
            "b1": _chunk_block("b1", tokens=10),
            "b2": _chunk_block("b2", tokens=10),
            "b3": _chunk_block("b3", tokens=10),
        }
        parent = _parent("P", ("b1", "b2", "b3"))
        chunks = self._chunks(parent, blocks, self._config())
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].block_refs, ("b1", "b2", "b3"))

    def test_split_on_max(self) -> None:
        blocks = {
            f"b{i}": _chunk_block(f"b{i}", tokens=10) for i in range(1, 5)
        }
        parent = _parent("P", tuple(f"b{i}" for i in range(1, 5)))
        chunks = self._chunks(parent, blocks, self._config())
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].block_refs, ("b1", "b2", "b3"))
        self.assertEqual(chunks[1].block_refs, ("b4",))
        self.assertNotEqual(chunks[0].child_id, chunks[1].child_id)

    def test_big_paragraph_sentence_split(self) -> None:
        text = "第一句。第二句。第三句。第四句。"
        blocks = {"big": _chunk_block("big", tokens=100, text=text)}
        parent = _parent("P", ("big",))
        chunks = self._chunks(parent, blocks, self._config(child_max_tokens=30))
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.block_refs, ("big",))
        self.assertEqual("".join(c.text for c in chunks).replace(" ", ""), text.replace(" ", ""))

    def test_table_not_truncated(self) -> None:
        text = "x" * 200
        blocks = {"t1": _chunk_block("t1", block_type=BlockTypeV1.TABLE, tokens=100, text=text)}
        parent = _parent("P", ("t1",))
        chunks = self._chunks(parent, blocks, self._config(child_max_tokens=30))
        # 表格为原子块，不按 Token 截断，单子块保留全文。
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, text)

    def test_overlap_prefixes_next_chunk(self) -> None:
        blocks = {
            "b1": _chunk_block("b1", tokens=20, text="hello world foo"),
            "b2": _chunk_block("b2", tokens=20, text="bar baz"),
        }
        parent = _parent("P", ("b1", "b2"))
        chunks = self._chunks(parent, blocks, self._config(child_max_tokens=30, overlap_tokens=2))
        self.assertEqual(len(chunks), 2)
        self.assertIn("world foo", chunks[1].text)
        self.assertIn("bar baz", chunks[1].text)

    def test_child_id_stable_and_parent_linked(self) -> None:
        blocks = {"b1": _chunk_block("b1", tokens=10)}
        parent = _parent("P", ("b1",))
        chunks = self._chunks(parent, blocks, self._config())
        self.assertEqual(chunks[0].parent_id, "P")
        self.assertRegex(chunks[0].child_id, r"^[0-9a-f]{64}$")
        self.assertRegex(chunks[0].text_hash, r"^[0-9a-f]{64}$")


class DocumentTreeContractTests(unittest.TestCase):
    def test_node_recursive_children(self) -> None:
        node = DocumentTreeNodeV1(
            kind=DocumentNodeKindV1.SECTION,
            structure_path=("S",),
            heading_level=1,
            page_from=1,
            page_to=1,
            children=(
                DocumentTreeNodeV1(
                    kind=DocumentNodeKindV1.ATOMIC_GROUP,
                    structure_path=("S",),
                    heading_level=0,
                    page_from=1,
                    page_to=1,
                ),
            ),
        )
        self.assertEqual(len(node.children), 1)

    def test_tree_extra_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            DocumentTreeV1(nodes=(), extra=1)  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
