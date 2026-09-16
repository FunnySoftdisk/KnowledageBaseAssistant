"""CORE-17~25：把CreateTaskRequestV1与Subject编排为TaskCreationWriteSet并原子执行。

仍待后续里程碑闭合：Blob staging、Policy Snapshot、explicit_constraint_extractor、
authorization_snapshot与preflight证明。对应列以裸UUID/空JSONB占位，均在字段矩阵
`deferred_foreign_keys`中登记，不宣称完整通过。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from knowledge_system.infrastructure.persistence import (
    TaskCreationTransactionService,
    TaskCreationWriteSet,
)
from knowledge_system.infrastructure.persistence.foundation_models import ArtifactRecord
from knowledge_system.infrastructure.persistence.task_models import (
    ConversationMessageRecord,
    IdempotencyRecord,
    IntelligentTaskRecord,
    OutboxMessageRecord,
    TaskAttachmentBindingRecord,
    TaskEventRecord,
    TaskInputSnapshotRecord,
)
from knowledge_system.modules.iam.domain.subject import Subject
from knowledge_system.modules.tasking.domain.core_types import (
    ArtifactRefV1,
    TaskAttachmentBindingRefV1,
    canonical_json_sha256,
)
from knowledge_system.modules.tasking.domain.input_contracts import (
    CreateTaskRequestV1,
    KnowledgeScopeV1,
    NetworkControlV1,
    create_task_request_digest,
    normalize_knowledge_scope,
    normalize_network_control,
)
from knowledge_system.modules.tasking.domain.output_contract_resolver import (
    FixedWorkflowOutputProfileV1,
    OutputContractRegistrySnapshotV1,
    OutputContractResolutionError,
    resolve_output_contract,
)

from .errors import TaskEntryError
from .gateway import TaskEntryGateway

CREATE_TASK_SCOPE: Final = "CREATE_TASK"
IDEMPOTENCY_KEY_HASH_VERSION: Final = "hmac_sha256_v1"
QUERY_SCHEMA_ID: Final = "task_query_text"
QUERY_SCHEMA_VERSION: Final = "v1"
DEFAULT_DEADLINE_SECONDS: Final = 600
DEFAULT_IDEMPOTENCY_TTL_HOURS: Final = 24


@dataclass(frozen=True, slots=True)
class CreateTaskResult:
    disposition: str
    task_id: UUID
    response_body: Mapping[str, object]


class TaskCreationService:
    """请求→写集→事务的编排；读取端口与写事务均注入，不直接持有Session。"""

    def __init__(
        self,
        transaction_service: TaskCreationTransactionService,
        entry: TaskEntryGateway,
        idempotency_hmac_secret: bytes,
        *,
        locale: str = "zh-CN",
        timezone: str = "Asia/Shanghai",
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
        fixed_workflow: FixedWorkflowOutputProfileV1 | None = None,
        registry: OutputContractRegistrySnapshotV1 | None = None,
    ) -> None:
        if deadline_seconds < 1:
            raise ValueError("DEADLINE_SECONDS_INVALID")
        if len(idempotency_hmac_secret) < 32:
            raise ValueError("IDEMPOTENCY_HMAC_SECRET_TOO_SHORT")
        self._transaction_service = transaction_service
        self._entry = entry
        self._idempotency_hmac_secret = idempotency_hmac_secret
        self._locale = locale
        self._timezone = timezone
        self._deadline_seconds = deadline_seconds
        self._fixed_workflow = fixed_workflow or FixedWorkflowOutputProfileV1(
            decision="SUPERVISOR", default_contract=None
        )
        self._registry = registry or OutputContractRegistrySnapshotV1(
            schema_refs=(), template_or_skill_refs=()
        )

    async def create(
        self,
        request: CreateTaskRequestV1,
        subject: Subject,
        *,
        idempotency_key: str,
    ) -> CreateTaskResult:
        conversation = await self._entry.load_conversation(
            request.conversation_id, subject.user_id
        )
        if conversation is None:
            raise TaskEntryError("CONVERSATION_NOT_FOUND")
        if conversation.organization_id != subject.organization_id:
            raise TaskEntryError("CONVERSATION_NOT_FOUND")

        task_id = uuid4()
        input_id = uuid4()
        message_id = uuid4()
        event_id = uuid4()
        outbox_id = uuid4()
        idempotency_id = uuid4()
        query_artifact_id = uuid4()

        attachment_bindings, binding_refs = await self._build_attachments(
            request, subject, task_id, message_id
        )

        now = datetime.now(UTC)
        query_bytes = request.query.encode("utf-8", errors="strict")
        query_sha256 = hashlib.sha256(query_bytes).hexdigest()
        deadline_at = now + timedelta(seconds=self._deadline_seconds)
        output_contract = self._resolve_output_contract(request)
        knowledge_scope = normalize_knowledge_scope(request.knowledge_scope)
        network = normalize_network_control(request.network_policy)

        query_artifact = self._build_query_artifact(
            query_artifact_id, query_sha256, len(query_bytes), subject
        )
        task = IntelligentTaskRecord(
            id=task_id,
            organization_id=subject.organization_id,
            owner_id=subject.user_id,
            conversation_id=request.conversation_id,
            query_artifact_id=query_artifact_id,
            initial_input_id=input_id,
            current_input_id=input_id,
            authorization_ref_id=uuid4(),
            status="QUEUED",
            temporal_workflow_id=f"agent-task-v1-{task_id}",
            last_event_sequence=0,
            budget={},
            budget_used={},
            deadline_at=deadline_at,
            version=1,
        )
        message = ConversationMessageRecord(
            id=message_id,
            conversation_id=request.conversation_id,
            role="USER",
            message_kind="USER_QUERY",
            content_artifact_id=query_artifact_id,
            task_id=task_id,
            sequence=conversation.next_message_sequence,
            revision=1,
            content_hash=query_sha256,
            state="ACTIVE",
        )
        snapshot = self._build_snapshot(
            input_id,
            task_id,
            message_id,
            query_artifact_id,
            subject,
            request,
            binding_refs,
            knowledge_scope,
            network,
            output_contract,
        )
        event = TaskEventRecord(
            id=event_id,
            task_id=task_id,
            sequence=1,
            event_type="TASK_CREATED",
            event_schema_version="task_event_v1",
            workflow_relevant=True,
            business_ref_type="TASK",
            business_ref_id=task_id,
            business_version=1,
            observation_expected_count=0,
            payload_json={"task_id": str(task_id), "status": "QUEUED"},
            payload_digest=canonical_json_sha256(
                {"task_id": str(task_id), "status": "QUEUED"}
            ),
        )
        outbox = OutboxMessageRecord(
            id=outbox_id,
            event_id=event_id,
            aggregate_type="TASK",
            aggregate_id=task_id,
            aggregate_version=1,
            destination="TEMPORAL_START",
            schema_version="agent_task_workflow_start_v1",
            payload_json={"task_id": str(task_id), "event_id": str(event_id)},
            payload_digest=canonical_json_sha256(
                {"task_id": str(task_id), "event_id": str(event_id)}
            ),
            publish_status="PENDING",
            available_at=now,
            attempt_count=0,
            row_version=1,
        )
        response_body = self._response_body(task_id, input_id, deadline_at)
        idempotency = IdempotencyRecord(
            id=idempotency_id,
            scope=CREATE_TASK_SCOPE,
            actor_id=subject.user_id,
            key_digest=self._key_digest(idempotency_key),
            key_hash_version=IDEMPOTENCY_KEY_HASH_VERSION,
            request_hash=create_task_request_digest(request),
            status="COMPLETED",
            resource_type="TASK",
            resource_id=task_id,
            response_code=202,
            response_body_json=dict(response_body),
            response_digest=canonical_json_sha256(response_body),
            expires_at=now + timedelta(hours=DEFAULT_IDEMPOTENCY_TTL_HOURS),
        )
        write_set = TaskCreationWriteSet(
            task=task,
            query_message=message,
            initial_input=snapshot,
            attachment_bindings=attachment_bindings,
            task_created_event=event,
            starter_outbox=outbox,
            idempotency=idempotency,
            query_artifact=query_artifact,
        )
        result = await self._transaction_service.execute(write_set)
        return CreateTaskResult(
            disposition=result.disposition.value,
            task_id=result.task_id,
            response_body=result.response_body,
        )

    def _key_digest(self, idempotency_key: str) -> str:
        return hmac.new(
            self._idempotency_hmac_secret,
            idempotency_key.encode("utf-8", errors="strict"),
            hashlib.sha256,
        ).hexdigest()

    async def _build_attachments(
        self,
        request: CreateTaskRequestV1,
        subject: Subject,
        task_id: UUID,
        message_id: UUID,
    ) -> tuple[
        tuple[TaskAttachmentBindingRecord, ...],
        tuple[TaskAttachmentBindingRefV1, ...],
    ]:
        bindings: list[TaskAttachmentBindingRecord] = []
        binding_refs: list[TaskAttachmentBindingRefV1] = []
        for attachment in request.attachments:
            snapshot = await self._entry.load_attachment(attachment.artifact_id, subject.user_id)
            if snapshot is None:
                raise TaskEntryError("TASK_ATTACHMENT_INVALID")
            if snapshot.organization_id != subject.organization_id:
                raise TaskEntryError("TASK_ATTACHMENT_INVALID")
            if snapshot.purpose != "TASK_ATTACHMENT":
                raise TaskEntryError("TASK_ATTACHMENT_INVALID")
            if snapshot.state != "AVAILABLE":
                raise TaskEntryError("TASK_ATTACHMENT_INVALID")
            if snapshot.sha256 != attachment.sha256:
                raise TaskEntryError("TASK_ATTACHMENT_INVALID")
            binding_id = uuid4()
            bindings.append(
                TaskAttachmentBindingRecord(
                    binding_id=binding_id,
                    task_id=task_id,
                    artifact_id=snapshot.artifact_id,
                    owner_id=subject.user_id,
                    introduced_by_message_id=message_id,
                    source_sha256=snapshot.sha256,
                    preflight_run_id=uuid4(),
                    detected_media_type=snapshot.detected_media_type,
                    ordinal=attachment.ordinal,
                    state="ACTIVE",
                    row_version=1,
                )
            )
            binding_refs.append(
                TaskAttachmentBindingRefV1(
                    binding_id=binding_id,
                    task_id=task_id,
                    artifact_ref=ArtifactRefV1(
                        artifact_id=snapshot.artifact_id,
                        artifact_type=snapshot.artifact_type,
                        sha256=snapshot.sha256,
                        schema_id=snapshot.schema_id,
                        schema_version=snapshot.schema_version,
                    ),
                    ordinal=attachment.ordinal,
                )
            )
        return tuple(bindings), tuple(binding_refs)

    def _resolve_output_contract(self, request: CreateTaskRequestV1) -> dict[str, object]:
        try:
            resolution = resolve_output_contract(
                explicit_contract=request.output_contract,
                authenticated_locale=self._locale,
                fixed_workflow=self._fixed_workflow,
                registry=self._registry,
            )
        except OutputContractResolutionError as error:
            raise TaskEntryError(error.code) from error
        return resolution.contract.model_dump(mode="json")

    @staticmethod
    def _build_query_artifact(
        artifact_id: UUID,
        sha256: str,
        size_bytes: int,
        subject: Subject,
    ) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=artifact_id,
            organization_id=subject.organization_id,
            owner_id=subject.user_id,
            purpose="QUERY",
            artifact_type="TEXT",
            storage_backend="S3",
            object_key=f"task-query/{sha256}",
            size_bytes=size_bytes,
            sha256=sha256,
            detected_media_type="text/plain",
            schema_id=QUERY_SCHEMA_ID,
            schema_version=QUERY_SCHEMA_VERSION,
            schema_digest=hashlib.sha256(
                f"{QUERY_SCHEMA_ID}:{QUERY_SCHEMA_VERSION}".encode()
            ).hexdigest(),
            data_labels_json={},
            encryption_profile="dev",
            state="AVAILABLE",
            legal_hold=False,
            row_version=1,
            created_by_actor_id=subject.user_id,
        )

    def _build_snapshot(
        self,
        input_id: UUID,
        task_id: UUID,
        message_id: UUID,
        message_artifact_id: UUID,
        subject: Subject,
        request: CreateTaskRequestV1,
        binding_refs: tuple[TaskAttachmentBindingRefV1, ...],
        knowledge_scope: KnowledgeScopeV1,
        network: NetworkControlV1,
        output_contract: dict[str, object],
    ) -> TaskInputSnapshotRecord:
        attachment_refs_json: list[object] = [
            ref.model_dump(mode="json") for ref in binding_refs
        ]
        knowledge_scope_json: dict[str, object] = knowledge_scope.model_dump(mode="json")
        selected_resource_refs_json: list[object] = [
            ref.model_dump(mode="json") for ref in request.selected_resource_refs
        ]
        snapshot_content: dict[str, object] = {
            "contract_version": "task_input_snapshot_v1",
            "input_id": str(input_id),
            "task_id": str(task_id),
            "input_revision": 1,
            "message_id": str(message_id),
            "message_artifact_id": str(message_artifact_id),
            "actor_id": str(subject.user_id),
            "organization_id": str(subject.organization_id),
            "conversation_id": str(request.conversation_id),
            "attachment_binding_refs": attachment_refs_json,
            "knowledge_scope": knowledge_scope_json,
            "network_policy": network.value,
            "network_policy_source": network.source.value,
            "output_contract": output_contract,
            "selected_resource_refs": selected_resource_refs_json,
            "task_constraint_refs": [],
            "policy_snapshot_ref": {},
            "locale": self._locale,
            "timezone": self._timezone,
        }
        return TaskInputSnapshotRecord(
            input_id=input_id,
            task_id=task_id,
            input_revision=1,
            message_id=message_id,
            message_artifact_id=message_artifact_id,
            actor_id=subject.user_id,
            organization_id=subject.organization_id,
            conversation_id=request.conversation_id,
            attachment_binding_refs_json=attachment_refs_json,
            knowledge_scope_json=knowledge_scope_json,
            network_policy=network.value,
            network_policy_source=network.source.value,
            output_contract_json=output_contract,
            selected_resource_refs_json=selected_resource_refs_json,
            task_constraint_refs_json=[],
            policy_snapshot_ref_json={},
            locale=self._locale,
            timezone=self._timezone,
            contract_version="task_input_snapshot_v1",
            snapshot_digest=canonical_json_sha256(snapshot_content),
        )

    @staticmethod
    def _response_body(task_id: UUID, input_id: UUID, deadline_at: datetime) -> dict[str, object]:
        return {
            "task_id": str(task_id),
            "input_id": str(input_id),
            "status": "QUEUED",
            "deadline_at": deadline_at.isoformat(),
            "active_plan_version": None,
        }
