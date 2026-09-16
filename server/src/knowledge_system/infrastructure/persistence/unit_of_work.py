"""显式SQLAlchemy Unit of Work。"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker

from .planning import PlanTransactionRepository
from .repositories import TaskTransactionRepository


class UnitOfWorkStateError(RuntimeError):
    """Unit of Work生命周期使用错误。"""


class SqlAlchemyUnitOfWork:
    """默认回滚；只有调用`commit()`才使写入可见。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._transaction: AsyncSessionTransaction | None = None
        self._tasks: TaskTransactionRepository | None = None
        self._plans: PlanTransactionRepository | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise UnitOfWorkStateError("UNIT_OF_WORK_NOT_ENTERED")
        return self._session

    @property
    def tasks(self) -> TaskTransactionRepository:
        if self._tasks is None:
            raise UnitOfWorkStateError("UNIT_OF_WORK_NOT_ENTERED")
        return self._tasks

    @property
    def plans(self) -> PlanTransactionRepository:
        if self._plans is None:
            raise UnitOfWorkStateError("UNIT_OF_WORK_NOT_ENTERED")
        return self._plans

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        if self._session is not None:
            raise UnitOfWorkStateError("UNIT_OF_WORK_ALREADY_ENTERED")
        self._session = self._session_factory()
        self._transaction = await self._session.begin()
        self._tasks = TaskTransactionRepository(self._session)
        self._plans = PlanTransactionRepository(self._session)
        return self

    async def commit(self) -> None:
        transaction = self._active_transaction()
        await transaction.commit()

    async def rollback(self) -> None:
        transaction = self._active_transaction()
        await transaction.rollback()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        session = self.session
        transaction = self._transaction
        try:
            if transaction is not None and transaction.is_active:
                await transaction.rollback()
        finally:
            await session.close()
            self._session = None
            self._transaction = None
            self._tasks = None
            self._plans = None

    def _active_transaction(self) -> AsyncSessionTransaction:
        if self._transaction is None or not self._transaction.is_active:
            raise UnitOfWorkStateError("UNIT_OF_WORK_TRANSACTION_NOT_ACTIVE")
        return self._transaction
