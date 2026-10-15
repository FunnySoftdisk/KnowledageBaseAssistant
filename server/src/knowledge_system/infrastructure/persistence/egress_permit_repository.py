"""Transactional PG authority for one-shot model egress and secret-read admission."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_system.modules.model_gateway.application.egress import (
    EgressAdmissionDecisionV1,
    EgressAdmissionOutcome,
    EgressAdmissionRequestV1,
    EgressExecutionPermitV1,
)
from knowledge_system.modules.model_gateway.application.secrets import ProviderSecretReadRequestV1
from knowledge_system.modules.model_gateway.domain.egress_admission import (
    EgressBodyDigestV1,
    verify_egress_projection,
)
from knowledge_system.modules.model_gateway.domain.egress_permit import (
    EgressPermitStatus,
    EgressPermitV1,
)

from .egress_permit_models import EgressPermitRecord
from .model_call_models import ModelCallRecord


class EgressPermitContractError(RuntimeError):
    """Stable failure code that never includes request bodies or secret material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EgressPermitIssueDisposition(StrEnum):
    INSERTED = "INSERTED"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"


class PostgresEgressPermitAuthority:
    """Own short transactions; no PG lock is held while Bridge performs HTTP I/O."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def issue_ready(self, permit: EgressPermitV1) -> EgressPermitIssueDisposition:
        if permit.status is not EgressPermitStatus.READY or permit.row_version != 1:
            raise EgressPermitContractError("EGRESS_PERMIT_ISSUE_STATE_INVALID")
        async with self._session_factory() as session, session.begin():
            call = await session.get(ModelCallRecord, permit.model_call_id, with_for_update=True)
            if call is None:
                raise EgressPermitContractError("EGRESS_PERMIT_MODEL_CALL_NOT_FOUND")
            _require_call_snapshot(call, permit)
            values = _record_values(permit)
            statement = (
                pg_insert(EgressPermitRecord)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[EgressPermitRecord.model_call_id])
                .returning(EgressPermitRecord.model_call_id)
            )
            inserted = (await session.execute(statement)).scalar_one_or_none()
            if inserted is not None:
                return EgressPermitIssueDisposition.INSERTED
            existing = await session.get(
                EgressPermitRecord,
                permit.model_call_id,
                with_for_update=True,
            )
            if existing is None or not _same_issued_facts(existing, permit):
                raise EgressPermitContractError("EGRESS_PERMIT_IDEMPOTENCY_CONFLICT")
            return EgressPermitIssueDisposition.IDEMPOTENT_REPLAY

    async def admit(self, *, request: EgressAdmissionRequestV1) -> EgressAdmissionDecisionV1:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            call, permit = await _lock_call_and_permit(session, request.model_call_id)
            if call is None or permit is None:
                return EgressAdmissionDecisionV1(
                    outcome=EgressAdmissionOutcome.REJECTED,
                    reason_code="EGRESS_PERMIT_NOT_FOUND",
                )
            reason = _current_call_rejection(call, permit)
            if reason is None and request.route_ref != permit.route_ref:
                reason = "EGRESS_ROUTE_MISMATCH"
            if reason is None:
                reason = verify_egress_projection(
                    approved=_body_from_record(permit),
                    actual=request.body,
                )
            if reason is None and permit.expires_at <= now:
                _expire(permit, now)
                reason = "EGRESS_PERMIT_EXPIRED"
            if reason is None and permit.status != EgressPermitStatus.READY.value:
                reason = "EGRESS_PERMIT_ALREADY_USED"
            if reason is not None:
                return EgressAdmissionDecisionV1(
                    outcome=EgressAdmissionOutcome.REJECTED,
                    reason_code=reason,
                )

            permit.status = EgressPermitStatus.ADMITTED.value
            permit.admitted_at = now
            permit.row_version += 1
            permit.updated_at = now
            return EgressAdmissionDecisionV1(
                outcome=EgressAdmissionOutcome.ADMITTED,
                permit=EgressExecutionPermitV1(
                    provider="alibaba_bailian",
                    provider_model=permit.provider_model,
                    secret_ref=permit.secret_ref,
                    secret_version=permit.secret_version,
                ),
            )

    async def authorize(self, request: ProviderSecretReadRequestV1) -> bool:
        """Consume the admitted permit exactly once before OpenBao is contacted."""
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            call, permit = await _lock_call_and_permit(session, request.model_call_id)
            if call is None or permit is None:
                return False
            if _current_call_rejection(call, permit) is not None:
                return False
            if permit.expires_at <= now:
                _expire(permit, now)
                return False
            if (
                permit.status != EgressPermitStatus.ADMITTED.value
                or request.route_ref != permit.route_ref
                or request.secret_ref != permit.secret_ref
                or request.secret_version != permit.secret_version
            ):
                return False
            permit.status = EgressPermitStatus.SECRET_READ.value
            permit.secret_read_at = now
            permit.row_version += 1
            permit.updated_at = now
            return True

    async def get(self, model_call_id: UUID) -> EgressPermitV1 | None:
        async with self._session_factory() as session:
            record = await session.get(EgressPermitRecord, model_call_id)
            return None if record is None else _v1_from_record(record)


async def _lock_call_and_permit(
    session: AsyncSession, model_call_id: UUID
) -> tuple[ModelCallRecord | None, EgressPermitRecord | None]:
    call = await session.get(ModelCallRecord, model_call_id, with_for_update=True)
    permit = (
        await session.scalars(
            select(EgressPermitRecord)
            .where(EgressPermitRecord.model_call_id == model_call_id)
            .with_for_update()
        )
    ).first()
    return call, permit


def _current_call_rejection(
    call: ModelCallRecord | None, permit: EgressPermitRecord | None
) -> str | None:
    if call is None or permit is None:
        return "EGRESS_PERMIT_NOT_FOUND"
    if call.status != "SENDING":
        return "EGRESS_MODEL_CALL_NOT_SENDABLE"
    if call.authorization_version != permit.authorization_version:
        return "EGRESS_AUTHORIZATION_VERSION_CHANGED"
    if call.execution_generation != permit.execution_generation:
        return "EGRESS_EXECUTION_GENERATION_CHANGED"
    if call.cancellation_generation != permit.cancellation_generation:
        return "EGRESS_CANCELLATION_GENERATION_CHANGED"
    if (
        call.approved_model_id != permit.provider_model
        or call.approved_model_revision != permit.model_revision
    ):
        return "EGRESS_MODEL_BINDING_CHANGED"
    return None


def _require_call_snapshot(call: ModelCallRecord, permit: EgressPermitV1) -> None:
    if call.status != "SENDING":
        raise EgressPermitContractError("EGRESS_MODEL_CALL_NOT_SENDABLE")
    if call.authorization_version != permit.authorization_version:
        raise EgressPermitContractError("EGRESS_AUTHORIZATION_VERSION_CHANGED")
    if call.execution_generation != permit.execution_generation:
        raise EgressPermitContractError("EGRESS_EXECUTION_GENERATION_CHANGED")
    if call.cancellation_generation != permit.cancellation_generation:
        raise EgressPermitContractError("EGRESS_CANCELLATION_GENERATION_CHANGED")
    if (
        call.approved_model_id != permit.provider_model
        or call.approved_model_revision != permit.body.model_revision
        or permit.provider_model != permit.body.model_id
    ):
        raise EgressPermitContractError("EGRESS_MODEL_BINDING_CHANGED")


def _record_values(permit: EgressPermitV1) -> dict[str, object]:
    return {
        "model_call_id": permit.model_call_id,
        "route_ref": permit.route_ref,
        "provider": permit.provider,
        "provider_model": permit.provider_model,
        "secret_ref": permit.secret_ref,
        "secret_version": permit.secret_version,
        "model_revision": permit.body.model_revision,
        "parameters_digest": permit.body.parameters_digest,
        "schema_digest": permit.body.schema_digest,
        "input_digest": permit.body.input_digest,
        "authorization_version": permit.authorization_version,
        "execution_generation": permit.execution_generation,
        "cancellation_generation": permit.cancellation_generation,
        "status": permit.status.value,
        "expires_at": permit.expires_at,
        "admitted_at": permit.admitted_at,
        "secret_read_at": permit.secret_read_at,
        "row_version": permit.row_version,
        "created_at": permit.created_at,
        "updated_at": permit.created_at,
    }


def _same_issued_facts(record: EgressPermitRecord, permit: EgressPermitV1) -> bool:
    expected = _record_values(permit)
    immutable_columns = (
        "model_call_id",
        "route_ref",
        "provider",
        "provider_model",
        "secret_ref",
        "secret_version",
        "model_revision",
        "parameters_digest",
        "schema_digest",
        "input_digest",
        "authorization_version",
        "execution_generation",
        "cancellation_generation",
        "expires_at",
        "created_at",
    )
    return all(getattr(record, name) == expected[name] for name in immutable_columns)


def _body_from_record(record: EgressPermitRecord) -> EgressBodyDigestV1:
    return EgressBodyDigestV1(
        model_id=record.provider_model,
        model_revision=record.model_revision,
        parameters_digest=record.parameters_digest,
        schema_digest=record.schema_digest,
        input_digest=record.input_digest,
    )


def _expire(record: EgressPermitRecord, now: datetime) -> None:
    if record.status in {EgressPermitStatus.READY.value, EgressPermitStatus.ADMITTED.value}:
        record.status = EgressPermitStatus.EXPIRED.value
        record.row_version += 1
        record.updated_at = now


def _v1_from_record(record: EgressPermitRecord) -> EgressPermitV1:
    return EgressPermitV1(
        model_call_id=record.model_call_id,
        route_ref=record.route_ref,
        provider="alibaba_bailian",
        provider_model=record.provider_model,
        secret_ref=record.secret_ref,
        secret_version=record.secret_version,
        body=_body_from_record(record),
        authorization_version=record.authorization_version,
        execution_generation=record.execution_generation,
        cancellation_generation=record.cancellation_generation,
        status=EgressPermitStatus(record.status),
        expires_at=record.expires_at,
        admitted_at=record.admitted_at,
        secret_read_at=record.secret_read_at,
        row_version=record.row_version,
        created_at=record.created_at,
    )


__all__ = [
    "EgressPermitContractError",
    "EgressPermitIssueDisposition",
    "PostgresEgressPermitAuthority",
]
