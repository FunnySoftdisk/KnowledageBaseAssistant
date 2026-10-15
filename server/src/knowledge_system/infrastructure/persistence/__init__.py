"""PostgreSQL持久化公共入口。"""

from . import attempt_models as attempt_models
from . import audit_models as audit_models
from . import budget_models as budget_models
from . import egress_permit_models as egress_permit_models
from . import foundation_models as foundation_models
from . import indexing_models as indexing_models
from . import memory_models as memory_models
from . import model_call_models as model_call_models
from . import observation_models as observation_models
from . import outbox as outbox
from . import planning_models as planning_models
from . import runtime_state_models as runtime_state_models
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
from .context_pack_repository import (
    ContextPackIntegrityError,
    SqlAlchemyContextPackRepository,
    item_record_from_manifest,
    pack_reuse_hit_from_record,
    record_from_pack,
)
from .egress_permit_repository import (
    EgressPermitContractError,
    EgressPermitIssueDisposition,
    PostgresEgressPermitAuthority,
)
from .engine import DatabaseEngineSettings, build_async_engine, build_session_factory
from .identity import IdentityRepository
from .memory_repository import (
    MemoryRecordIntegrityError,
    SqlAlchemyMemoryRepository,
    memory_from_record,
    record_from_candidate,
)
from .model_call_repository import (
    ModelCallLedgerRepository,
    ModelCallRecordIntegrityError,
    record_from_v1,
    v1_from_record,
)
from .observation_repository import (
    DispositionAppendResult,
    ObservationInsertResult,
    ObservationRecordIntegrityError,
    TaskObservationRepository,
    disposition_event_from_record,
    disposition_record_from_event,
    observation_from_record,
    record_from_observation,
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
from .runtime_state_repository import (
    RuntimeStateRecordIntegrityError,
    RuntimeStateRowVersionConflict,
    TaskRuntimeStateRepository,
    record_from_state,
    state_from_record,
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
from .task_runtime_read import (
    RuntimeFactsIntegrityError,
    RuntimeFactsNotRunnableError,
    TaskRuntimeReadRepository,
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
    "ContextPackIntegrityError",
    "DatabaseEngineSettings",
    "DispositionAppendResult",
    "EgressPermitContractError",
    "EgressPermitIssueDisposition",
    "GoalCompletionConflictError",
    "GoalCompletionWriteSet",
    "IdempotencyKeyReusedError",
    "IdentityRepository",
    "MemoryRecordIntegrityError",
    "ModelCallLedgerRepository",
    "ModelCallRecordIntegrityError",
    "ObservationInsertResult",
    "ObservationRecordIntegrityError",
    "OutboxStartContextMissingError",
    "PersistenceContractError",
    "PlanActivationConflictError",
    "PlanActivationWriteSet",
    "PlanAttemptCompletion",
    "PlanTransactionRepository",
    "PostgresEgressPermitAuthority",
    "RuntimeFactsIntegrityError",
    "RuntimeFactsNotRunnableError",
    "RuntimeStateRecordIntegrityError",
    "RuntimeStateRowVersionConflict",
    "SqlAlchemyAuditWriter",
    "SqlAlchemyContextPackRepository",
    "SqlAlchemyMemoryRepository",
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
    "TaskObservationRepository",
    "TaskReadRepository",
    "TaskResultCommitContractError",
    "TaskResultRecordIntegrityError",
    "TaskResultRepository",
    "TaskRuntimeReadRepository",
    "TaskRuntimeStateRepository",
    "TaskStartContext",
    "TaskTransactionRepository",
    "UnitOfWorkStateError",
    "build_async_engine",
    "build_session_factory",
    "disposition_event_from_record",
    "disposition_record_from_event",
    "item_record_from_manifest",
    "entry_record_from_v1",
    "entry_v1_from_record",
    "memory_from_record",
    "observation_from_record",
    "pack_reuse_hit_from_record",
    "record_from_commit",
    "record_from_candidate",
    "record_from_observation",
    "record_from_pack",
    "record_from_state",
    "record_from_v1",
    "reservation_record_from_v1",
    "reservation_v1_from_record",
    "state_from_record",
    "v1_from_record",
    "attempt_models",
    "audit_models",
    "budget_models",
    "egress_permit_models",
    "foundation_models",
    "indexing_models",
    "memory_models",
    "model_call_models",
    "observation_models",
    "outbox",
    "planning_models",
    "runtime_state_models",
    "task_models",
    "task_result_models",
]
