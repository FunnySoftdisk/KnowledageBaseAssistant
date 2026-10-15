"""模型网关受控调用的组合根：把真实基础设施适配器接成一次`ModelGatewayService`。

`compose_model_gateway_service()`为一次`complete()`装配一个绑定全新Session的
`ModelGatewayService`——`ModelCallLedgerRepository`/`BudgetLedgerRepository`共享同一Session、
经`SqlAlchemyModelGatewayTransaction`提供显式提交边界；出网许可签发经`PostgresEgressPermitIssuer`
（共享工厂级`PostgresEgressPermitAuthority` + Secret版本解析器）；流经`BifrostHttpClient`。
返回的`transaction`既实现`ModelGatewayTransaction`（`commit`/`rollback`）也是异步上下文管理器，
退出时回滚未提交事务并关闭Session。每个`complete()`调用都应用本函数装配一份，用完即随上下文
退出释放Session，不跨调用复用。

SecretRef由身份HMAC确定性派生、SecretVersion由OpenBao元数据解析，均不进入本组合根载荷；
`identity_hmac_key`是部署级标识密钥，不是Provider Key。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_system.infrastructure.persistence import (
    BudgetLedgerRepository,
    ModelCallLedgerRepository,
    PostgresEgressPermitAuthority,
    SqlAlchemyContextPackRepository,
)
from knowledge_system.modules.model_gateway.application.bifrost import BifrostStreamPort
from knowledge_system.modules.model_gateway.application.policy_adapter import ModelGatewayService

from .egress_permit_issuer import (
    PostgresEgressPermitIssuer,
    ProviderSecretVersionResolver,
)


class SqlAlchemyModelGatewayTransaction:
    """绑定ledger/budget共用Session的显式提交边界；退出时回滚未提交并关闭Session。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def commit(self) -> None:
        if self._session.in_transaction():
            await self._session.commit()

    async def rollback(self) -> None:
        if self._session.in_transaction():
            await self._session.rollback()

    async def __aenter__(self) -> SqlAlchemyModelGatewayTransaction:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self._session.in_transaction():
            await self._session.rollback()
        await self._session.close()


@dataclass(frozen=True, slots=True)
class ModelGatewayWiring:
    """一次受控调用的装配：显式事务边界 + 绑定其Session的`ModelGatewayService`。"""

    transaction: SqlAlchemyModelGatewayTransaction
    service: ModelGatewayService


def compose_model_gateway_service(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    permit_authority: PostgresEgressPermitAuthority,
    version_resolver: ProviderSecretVersionResolver,
    bifrost: BifrostStreamPort,
    identity_hmac_key: bytes,
    workspace_id: str,
    provider: Literal["alibaba_bailian"] = "alibaba_bailian",
    region: str = "cn-beijing",
    mount: str = "knowledge-model",
) -> ModelGatewayWiring:
    """装配一次受控调用：全新Session + 会话级账本 + 显式提交边界 + 签发方 + Bifrost。"""
    issuer = PostgresEgressPermitIssuer(
        authority=permit_authority,
        version_resolver=version_resolver,
        identity_hmac_key=identity_hmac_key,
        workspace_id=workspace_id,
        provider=provider,
        region=region,
        mount=mount,
    )
    session = session_factory()
    transaction = SqlAlchemyModelGatewayTransaction(session)
    service = ModelGatewayService(
        ledger=ModelCallLedgerRepository(session),
        budget=BudgetLedgerRepository(session),
        permit_issuer=issuer,
        bifrost=bifrost,
        transactions=transaction,
        context_packs=SqlAlchemyContextPackRepository(session),
    )
    return ModelGatewayWiring(transaction=transaction, service=service)


__all__ = [
    "ModelGatewayWiring",
    "SqlAlchemyModelGatewayTransaction",
    "compose_model_gateway_service",
]
