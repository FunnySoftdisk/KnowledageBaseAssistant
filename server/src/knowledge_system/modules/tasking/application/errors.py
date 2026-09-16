"""任务入口应用错误；code映射到HTTP状态由API层负责。"""

from __future__ import annotations


class TaskEntryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
