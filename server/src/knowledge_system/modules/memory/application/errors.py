"""记忆与上下文应用错误；code 映射到 HTTP 状态由 API 层负责。"""

from __future__ import annotations


class MemoryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
