"""DEV-04 final-hop PG permit contract and fail-closed snapshot checks."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError

from knowledge_system.infrastructure.persistence.egress_permit_models import EgressPermitRecord
from knowledge_system.infrastructure.persistence.egress_permit_repository import (
    EgressPermitContractError,
    _body_from_record,
    _current_call_rejection,
    _require_call_snapshot,
    _same_issued_facts,
    _v1_from_record,
)
from knowledge_system.infrastructure.persistence.model_call_models import ModelCallRecord
from knowledge_system.modules.model_gateway.domain.egress_admission import EgressBodyDigestV1
from knowledge_system.modules.model_gateway.domain.egress_permit import (
    EgressPermitStatus,
    EgressPermitV1,
)

DIGEST = "a" * 64
SECRET_REF = "openbao-kv-v2://knowledge-model/model-provider/" + "b" * 64


def _permit(**updates: object) -> EgressPermitV1:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "model_call_id": uuid4(),
        "route_ref": "route/planning-strong/v1",
        "provider": "alibaba_bailian",
        "provider_model": "qwen-plus",
        "secret_ref": SECRET_REF,
        "secret_version": 1,
        "body": EgressBodyDigestV1(
            model_id="qwen-plus",
            model_revision="qwen-plus",
            parameters_digest=DIGEST,
            schema_digest="c" * 64,
            input_digest="d" * 64,
        ),
        "authorization_version": 3,
        "execution_generation": 5,
        "cancellation_generation": 2,
        "status": EgressPermitStatus.READY,
        "expires_at": now + timedelta(minutes=2),
        "row_version": 1,
        "created_at": now,
    }
    values.update(updates)
    return EgressPermitV1.model_validate(values)


def _call(permit: EgressPermitV1, **updates: object) -> ModelCallRecord:
    values: dict[str, object] = {
        "id": permit.model_call_id,
        "status": "SENDING",
        "authorization_version": permit.authorization_version,
        "execution_generation": permit.execution_generation,
        "cancellation_generation": permit.cancellation_generation,
        "approved_model_id": permit.provider_model,
        "approved_model_revision": permit.body.model_revision,
    }
    values.update(updates)
    return ModelCallRecord(**values)


def _record(permit: EgressPermitV1) -> EgressPermitRecord:
    return EgressPermitRecord(
        model_call_id=permit.model_call_id,
        route_ref=permit.route_ref,
        provider=permit.provider,
        provider_model=permit.provider_model,
        secret_ref=permit.secret_ref,
        secret_version=permit.secret_version,
        model_revision=permit.body.model_revision,
        parameters_digest=permit.body.parameters_digest,
        schema_digest=permit.body.schema_digest,
        input_digest=permit.body.input_digest,
        authorization_version=permit.authorization_version,
        execution_generation=permit.execution_generation,
        cancellation_generation=permit.cancellation_generation,
        status=permit.status.value,
        expires_at=permit.expires_at,
        admitted_at=permit.admitted_at,
        secret_read_at=permit.secret_read_at,
        row_version=permit.row_version,
        created_at=permit.created_at,
        updated_at=permit.created_at,
    )


class EgressPermitContractTests(unittest.TestCase):
    def test_ready_contract_and_round_trip_preserve_only_refs_and_digests(self) -> None:
        permit = _permit()
        record = _record(permit)

        self.assertEqual(_v1_from_record(record), permit)
        self.assertEqual(_body_from_record(record), permit.body)
        self.assertTrue(_same_issued_facts(record, permit))
        self.assertNotIn("api_key", permit.model_dump_json())

    def test_lifecycle_timestamp_combinations_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "EGRESS_PERMIT_LIFECYCLE_INVALID"):
            _permit(status=EgressPermitStatus.ADMITTED)
        with self.assertRaisesRegex(ValidationError, "EGRESS_PERMIT_EXPIRY_INVALID"):
            now = datetime.now(UTC)
            _permit(created_at=now, expires_at=now)

    def test_model_call_snapshot_accepts_exact_current_generation(self) -> None:
        permit = _permit()
        call = _call(permit)

        self.assertIsNone(_current_call_rejection(call, _record(permit)))
        _require_call_snapshot(call, permit)

    def test_model_call_snapshot_rejects_stale_authorization_and_generation(self) -> None:
        permit = _permit()
        cases = (
            ({"authorization_version": 4}, "EGRESS_AUTHORIZATION_VERSION_CHANGED"),
            ({"execution_generation": 6}, "EGRESS_EXECUTION_GENERATION_CHANGED"),
            ({"cancellation_generation": 3}, "EGRESS_CANCELLATION_GENERATION_CHANGED"),
            ({"status": "CANCELLED"}, "EGRESS_MODEL_CALL_NOT_SENDABLE"),
            ({"approved_model_id": "other"}, "EGRESS_MODEL_BINDING_CHANGED"),
        )
        for updates, code in cases:
            with self.subTest(code=code):
                call = _call(permit, **updates)
                self.assertEqual(_current_call_rejection(call, _record(permit)), code)
                with self.assertRaisesRegex(EgressPermitContractError, code):
                    _require_call_snapshot(call, permit)

    def test_orm_columns_match_frozen_projection(self) -> None:
        self.assertEqual(
            list(EgressPermitRecord.__table__.c.keys()),
            [
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
                "status",
                "expires_at",
                "admitted_at",
                "secret_read_at",
                "row_version",
                "created_at",
                "updated_at",
            ],
        )


if __name__ == "__main__":
    unittest.main()
