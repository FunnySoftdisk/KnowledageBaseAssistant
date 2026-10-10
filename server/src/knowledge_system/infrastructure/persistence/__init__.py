"""PostgreSQL持久化公共入口。"""

from . import attempt_models as attempt_models
from . import audit_models as audit_models
from . import budget_models as budget_models
from . import foundation_models as foundation_models
from . import model_call_models as model_call_models
from . import outbox as outbox
from . import planning_models as planning_models
from . import task_models as task_models
from . import task_result_models as task_result_models
from .audit_repository import AuditChainError, AuditRepository
from .audit_writer import SqlAlchemyAuditWriter
from .base import Base
from .budget_repository import (
    BudgetLedgerContractError,
    BudgetLedgerRepository,
    BudgetRecordIntegrityError,
    entry_record_from_v1,
    entry_v1_from_record,
    reservation_record_from_v1,
    reservation_v1_from_record,
)
from .engine import DatabaseEngineSettings, build_async_engine, build_session_factory
from .identity import IdentityRepository
from .model_call_repository import (
    ModelCallLedgerRepository,
    ModelCallRecordIntegrityError,
    record_from_v1,
    v1_from_record,
)
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
    TaskCancellationWriteSet,
    TaskCreationWriteSet,
    TaskNotFoundError,
    TaskTransactionRepository,
)
from .task_cancel import (
    TaskCancelConflictError,
    TaskCancelDisposition,
    TaskCancellationTransactionResult,
    TaskCancellationTransactionService,
    TaskCancelTerminalError,
)
from .task_creation import (
    IdempotencyKeyReusedError,
    TaskCreationDisposition,
    TaskCreationTransactionResult,
    TaskCreationTransactionService,
    TaskCreationWriteConflictError,
)
from .task_read import TaskReadRepository
from .task_result_repository import (
    TaskResultCommitContractError,
    TaskResultRecordIntegrityError,
    TaskResultRepository,
    record_from_commit,
)
from .unit_of_work import SqlAlchemyUnitOfWork, UnitOfWorkStateError

__all__ = [
    "AuditChainError",
    "AuditRepository",
    "Base",
    "BudgetLedgerContractError",
    "BudgetLedgerRepository",
    "BudgetRecordIntegrityError",
    "ClaimedOutboxMessage",
    "DatabaseEngineSettings",
    "GoalCompletionConflictError",
    "GoalCompletionWriteSet",
    "IdempotencyKeyReusedError",
    "IdentityRepository",
    "ModelCallLedgerRepository",
    "ModelCallRecordIntegrityError",
    "OutboxStartContextMissingError",
    "PersistenceContractError",
    "PlanActivationConflictError",
    "PlanActivationWriteSet",
    "PlanAttemptCompletion",
    "PlanTransactionRepository",
    "SqlAlchemyAuditWriter",
    "SqlAlchemyOutboxRepository",
    "SqlAlchemyUnitOfWork",
    "TaskCancelConflictError",
    "TaskCancelDisposition",
    "TaskCancelTerminalError",
    "TaskCancellationTransactionResult",
    "TaskCancellationTransactionService",
    "TaskCancellationWriteSet",
    "TaskCreationWriteSet",
    "TaskCreationDisposition",
    "TaskCreationTransactionResult",
    "TaskCreationTransactionService",
    "TaskCreationWriteConflictError",
    "TaskNotFoundError",
    "TaskReadRepository",
    "TaskResultCommitContractError",
    "TaskResultRecordIntegrityError",
    "TaskResultRepository",
    "TaskStartContext",
    "TaskTransactionRepository",
    "UnitOfWorkStateError",
    "build_async_engine",
    "build_session_factory",
    "entry_record_from_v1",
    "entry_v1_from_record",
    "record_from_commit",
    "record_from_v1",
    "reservation_record_from_v1",
    "reservation_v1_from_record",
    "v1_from_record",
    "attempt_models",
    "audit_models",
    "budget_models",
    "foundation_models",
    "model_call_models",
    "outbox",
    "planning_models",
    "task_models",
    "task_result_models",
]
