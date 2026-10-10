"""DEV-03E：Worker Build ID格式校验与PINNED版本兼容检查。

Build ID形如`{release_version}+{git_sha}`（见`topology.build_id`）。本骨架只有PINNED一种
版本行为：Worker Build ID必须与已登记Build ID精确一致才允许在队列上处理任务，禁止用
“兼容集”放行旧Build ID，防止旧代码推进新契约后重放/回放失配。校验为纯函数，供发布前
门禁与Worker装配复用，不接触Secret、不发起网络。
"""

from __future__ import annotations

from knowledge_system.modules.temporal.domain.topology import WORKER_VERSIONING_BEHAVIOR

_GIT_SHA_HEX = frozenset("0123456789abcdef")


def is_valid_build_id(build_id: str) -> bool:
    """`{release}+{git_sha}`：release非空且不含`+`，git_sha为7~40位十六进制（不区分大小写）。"""

    if "+" not in build_id:
        return False
    release, git_sha = build_id.rsplit("+", 1)
    if not release or "+" in release:
        return False
    if not 7 <= len(git_sha) <= 40:
        return False
    return all(ch in _GIT_SHA_HEX for ch in git_sha.lower())


def worker_build_id_compatible(registered_build_id: str, worker_build_id: str) -> bool:
    """PINNED版本行为：Worker Build ID必须与已登记Build ID精确一致。"""

    if WORKER_VERSIONING_BEHAVIOR != "PINNED":
        return False
    return is_valid_build_id(worker_build_id) and worker_build_id == registered_build_id


__all__ = ["is_valid_build_id", "worker_build_id_compatible"]
