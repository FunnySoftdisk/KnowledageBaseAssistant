"""受控单次模型调用的持久化事务边界端口。

一次`ModelGatewayService.complete()`横跨两段持久化事务：预流段（ModelCall ACCEPTED→SENDING +
预算 RESERVED→CONSUMING，即端口docstring里的`MODEL_REQUEST_ACCEPTED`事务）与流后段（终态收敛 +
预算结算）。真实PG下预流段必须先提交，出网许可签发（`PostgresEgressPermitAuthority`自持短事务）
才能读到已提交的`SENDING`；因此编排把两段提交暴露为本端口，由基础设施层绑定同一Session的
ledger/budget实现。端口只给`commit`/`rollback`两个动作，不含Session/ORM/网络。
"""

from __future__ import annotations

from typing import Protocol


class ModelGatewayTransaction(Protocol):
    """一次受控调用的显式提交边界；实现位于基础设施层，绑定ledger/budget共用Session。"""

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


__all__ = ["ModelGatewayTransaction"]
