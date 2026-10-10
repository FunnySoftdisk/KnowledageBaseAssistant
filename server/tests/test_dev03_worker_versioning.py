"""DEV-03E Worker Build ID格式校验与PINNED版本兼容检查的单元验收。"""

from __future__ import annotations

import unittest

from knowledge_system.modules.temporal.application.worker_versioning import (
    is_valid_build_id,
    worker_build_id_compatible,
)
from knowledge_system.modules.temporal.domain.topology import build_id


class Dev03WorkerVersioningTests(unittest.TestCase):
    def test_build_id_roundtrips_to_valid(self) -> None:
        self.assertTrue(is_valid_build_id(build_id("0.1.0", "abc1234")))
        self.assertTrue(is_valid_build_id("0.1.0" + "+" + "a" * 40))
        self.assertTrue(is_valid_build_id("2026.09.1" + "+" + "F" * 7))

    def test_invalid_build_id_rejected(self) -> None:
        self.assertFalse(is_valid_build_id(""))  # 空
        self.assertFalse(is_valid_build_id("0.1.0"))  # 无`+`
        self.assertFalse(is_valid_build_id("0.1.0+abc123"))  # git_sha过短(6)
        self.assertFalse(is_valid_build_id("0.1.0" + "+" + "a" * 41))  # git_sha过长
        self.assertFalse(is_valid_build_id("0.1.0+abc12g4"))  # 非十六进制
        self.assertFalse(is_valid_build_id("+abc1234"))  # release为空
        self.assertFalse(is_valid_build_id("a+b+abc1234"))  # release含`+`

    def test_pinned_requires_exact_match(self) -> None:
        self.assertTrue(worker_build_id_compatible("0.1.0+abc1234", "0.1.0+abc1234"))
        # 同release不同git_sha：PINNED下拒绝。
        self.assertFalse(worker_build_id_compatible("0.1.0+abc1234", "0.1.0+def5678"))
        # 非法Build ID：拒绝。
        self.assertFalse(worker_build_id_compatible("0.1.0+abc1234", "not-a-build-id"))


if __name__ == "__main__":
    unittest.main()
