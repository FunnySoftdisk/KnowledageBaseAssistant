"""TokenAccountingV1单一Pydantic源类型；SDK协议适配在M0另行验证。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, field_validator

NonNegativeInt = Annotated[int, Field(ge=0)]
Revision = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S+$")]
Sha256 = Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]


class ExactTokenAccountingV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["EXACT"]
    estimated: Literal[False]
    exact_input_tokens: NonNegativeInt
    tokenizer_revision: Revision
    chat_template_hash: Sha256

    @field_validator("estimated", mode="before")
    @classmethod
    def require_false_boolean(cls, value: object) -> object:
        if value is not False:
            raise ValueError("EXACT requires estimated=false boolean")
        return value


class EstimatedDevTokenAccountingV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal["ESTIMATED_DEV"]
    estimated: Literal[True]
    estimated_input_tokens: NonNegativeInt
    estimator_revision: Revision
    serialized_request_bytes: NonNegativeInt
    protocol_overhead_tokens: NonNegativeInt

    @field_validator("estimated", mode="before")
    @classmethod
    def require_true_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("ESTIMATED_DEV requires estimated=true boolean")
        return value


TokenAccountingV1 = Annotated[
    ExactTokenAccountingV1 | EstimatedDevTokenAccountingV1, Field(discriminator="mode")
]
TOKEN_ACCOUNTING_ADAPTER: TypeAdapter[TokenAccountingV1] = TypeAdapter(TokenAccountingV1)
