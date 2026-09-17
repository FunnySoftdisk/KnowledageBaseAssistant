"""PostgreSQL持久化公共入口。"""

from . import attempt_models as attempt_models
from . import audit_models as audit_models
from . import foundation_models as foundation_models
from . import outbox as outbox
from . import planning_models as planning_models
from . import task_models as task_models
from .audit_repository import AuditChainError, AuditRepository
from .audit_writer import SqlAlchemyAuditWriter
from .base import Base
from .engine import DatabaseEngineSettings, build_async_engine, build_session_factory
from .identity import IdentityRepository
from .outbox import (
    ClaimedOutboxMessage,
    OutboxStartContextMissingError,
    SqlAlchemyOutboxRepository,
    TaskStartContext,
)
from .planning import (
    GoalCompletionConflictError,
    GoalCompletionWriteSet,
    PlanActivationConflictError,
    PlanActivationWriteSet,
    PlanAttemptCompletion,
    PlanTransactionRepository,
)
from .repositories import (
    PersistenceContractError,
    TaskCreationWriteSet,
    TaskNotFoundError,
    TaskTransactionRepository,
)
from .task_creation import (
    IdempotencyKeyReusedError,
    TaskCreationDisposition,
    TaskCreationTransactionResult,
    TaskCreationTransactionService,
    TaskCreationWriteConflictError,
)
from .task_read import TaskReadRepository
from .unit_of_work import SqlAlchemyUnitOfWork, UnitOfWorkStateError

__all__ = [
    "AuditChainError",
    "AuditRepository",
    "Base",
    "ClaimedOutboxMessage",
    "DatabaseEngineSettings",
    "GoalCompletionConflictError",
    "GoalCompletionWriteSet",
    "IdempotencyKeyReusedError",
    "IdentityRepository",
    "OutboxStartContextMissingError",
    "PersistenceContractError",
    "PlanActivationConflictError",
    "PlanActivationWriteSet",
    "PlanAttemptCompletion",
    "PlanTransactionRepository",
    "SqlAlchemyAuditWriter",
    "SqlAlchemyOutboxRepository",
    "SqlAlchemyUnitOfWork",
    "TaskCreationWriteSet",
    "TaskCreationDisposition",
    "TaskCreationTransactionResult",
    "TaskCreationTransactionService",
    "TaskCreationWriteConflictError",
    "TaskNotFoundError",
    "TaskReadRepository",
    "TaskStartContext",
    "TaskTransactionRepository",
    "UnitOfWorkStateError",
    "build_async_engine",
    "build_session_factory",
    "attempt_models",
    "audit_models",
    "foundation_models",
    "outbox",
    "planning_models",
    "task_models",
]
