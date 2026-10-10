"""DEV-03C：Child `AgentAttemptWorkflow`——一次Attempt的可靠执行闭环。

只承载确定性调度：认知Activity（MODEL队列）→ 终态四重CAS（CONTROL队列）→ 返回
`AgentAttemptResultV1`。认知Activity在本批以显式测试替身替换，不调真实模型。

隔离语义：Child启动时携带的`lease_generation`/`attempt_row_version`是本次Generation的
fencing令牌；终态CAS不匹配即`committed=False`，Child以`ATTEMPT_FENCED_STALE_GENERATION`
静默返回，旧Generation/迟到结果不进PG。取消路径：捕获`asyncio.CancelledError`后尽力以
CAS把Attempt落为CANCELLED再重抛，让Temporal标记Child CANCELLED，无孤儿Attempt。
"""

from __future__ import annotations

import asyncio
from typing import Literal, cast

from temporalio import workflow
from temporalio.exceptions import ApplicationError, is_cancelled_exception

from knowledge_system.modules.temporal.domain.activity_profiles import (
    ActivityProfileKindV1,
)
from knowledge_system.modules.temporal.domain.contracts import (
    AgentAttemptResultV1,
    AgentAttemptWorkflowInputV1,
    AttemptCommitOutcomeV1,
    AttemptCompletedV1,
    AttemptFailedV1,
    AttemptTerminalCommitV1,
    ExecuteAttemptCommandV1,
    ExecuteAttemptResultV1,
)
from knowledge_system.modules.temporal.domain.topology import (
    CONTROL_TASK_QUEUE,
    MODEL_TASK_QUEUE,
)
from knowledge_system.modules.temporal.workflows.attempt_activities import (
    COMMIT_ATTEMPT_TERMINAL_STATE_ACTIVITY,
    EXECUTE_ATTEMPT_ACTIVITY,
)
from knowledge_system.modules.temporal.workflows.profiles import activity_execute_kwargs


@workflow.defn(name="AgentAttemptWorkflow")
class AgentAttemptWorkflow:
    """Child Workflow：一次Attempt的认知执行与四重CAS终态提交。"""

    def __init__(self) -> None:
        self._attempt_input: AgentAttemptWorkflowInputV1 | None = None

    @workflow.run
    async def run(self, attempt_input: AgentAttemptWorkflowInputV1) -> AgentAttemptResultV1:
        self._attempt_input = attempt_input
        try:
            execution = await self._execute_attempt()
        except (asyncio.CancelledError, Exception) as err:
            # 取消不以单一异常形式抵达：阻塞的认知Activity被取消时`execute_activity`抛
            # `ActivityError`（根因`CancelledError`），而非`asyncio.CancelledError`。以
            # `is_cancelled_exception`判别；非取消异常照常重抛，保持失败语义不变。
            if is_cancelled_exception(err):
                await self._commit_cancelled()
            raise

        outcome = await self._commit_terminal(execution)
        if not outcome.committed:
            # 被更新Generation/并发推进隔离：本次结果作废，静默返回不污染PG。
            return AgentAttemptResultV1(
                AttemptFailedV1(
                    task_attempt_id=self._require_input().task_attempt_id,
                    error_code="ATTEMPT_FENCED_STALE_GENERATION",
                    reason=outcome.conflict_reason,
                )
            )
        if execution.error_code is not None:
            return AgentAttemptResultV1(
                AttemptFailedV1(
                    task_attempt_id=self._require_input().task_attempt_id,
                    error_code=execution.error_code,
                    reason=execution.failure_class,
                )
            )
        return AgentAttemptResultV1(
            AttemptCompletedV1(task_attempt_id=self._require_input().task_attempt_id)
        )

    async def _execute_attempt(self) -> ExecuteAttemptResultV1:
        attempt = self._require_input()
        return cast(
            ExecuteAttemptResultV1,
            await workflow.execute_activity(
                EXECUTE_ATTEMPT_ACTIVITY,
                ExecuteAttemptCommandV1(
                    task_id=attempt.task_id,
                    task_attempt_id=attempt.task_attempt_id,
                    attempt_kind=attempt.attempt_kind,
                    plan_version=attempt.plan_version,
                ),
                result_type=ExecuteAttemptResultV1,
                **activity_execute_kwargs(
                    ActivityProfileKindV1.MODEL_REQUEST, MODEL_TASK_QUEUE
                ),
            ),
        )

    async def _commit_terminal(
        self, execution: ExecuteAttemptResultV1
    ) -> AttemptCommitOutcomeV1:
        attempt = self._require_input()
        terminal_state: Literal["COMPLETED", "FAILED"] = (
            "FAILED" if execution.error_code is not None else "COMPLETED"
        )
        return cast(
            AttemptCommitOutcomeV1,
            await workflow.execute_activity(
                COMMIT_ATTEMPT_TERMINAL_STATE_ACTIVITY,
                AttemptTerminalCommitV1(
                    task_id=attempt.task_id,
                    task_attempt_id=attempt.task_attempt_id,
                    lease_generation=attempt.lease_generation,
                    expected_row_version=attempt.attempt_row_version,
                    terminal_state=terminal_state,
                    result_kind=execution.result_kind,
                    result_schema_version=execution.result_schema_version,
                    result_digest=execution.result_digest,
                    error_code=execution.error_code,
                    failure_class=execution.failure_class,
                ),
                result_type=AttemptCommitOutcomeV1,
                **activity_execute_kwargs(
                    ActivityProfileKindV1.CONTROL_TRANSACTION, CONTROL_TASK_QUEUE
                ),
            ),
        )

    async def _commit_cancelled(self) -> None:
        """取消清理：尽力CAS落CANCELLED；清理自身再被取消也不掩盖原始取消。"""

        attempt = self._require_input()
        try:
            await workflow.execute_activity(
                COMMIT_ATTEMPT_TERMINAL_STATE_ACTIVITY,
                AttemptTerminalCommitV1(
                    task_id=attempt.task_id,
                    task_attempt_id=attempt.task_attempt_id,
                    lease_generation=attempt.lease_generation,
                    expected_row_version=attempt.attempt_row_version,
                    terminal_state="CANCELLED",
                ),
                **activity_execute_kwargs(
                    ActivityProfileKindV1.CONTROL_TRANSACTION, CONTROL_TASK_QUEUE
                ),
            )
        except (asyncio.CancelledError, Exception):
            # 尽力而为的清理：清理Activity自身再被取消也不掩盖原始取消。
            pass

    def _require_input(self) -> AgentAttemptWorkflowInputV1:
        if self._attempt_input is None:
            raise ApplicationError(
                "ATTEMPT_STATE_NOT_INITIALIZED",
                type="ATTEMPT_STATE_NOT_INITIALIZED",
                non_retryable=True,
            )
        return self._attempt_input


__all__ = ["AgentAttemptWorkflow"]
