"""PydanticAI `Model`适配器：把Agent的一次结构化生成编排为一次受控模型调用。

`GatewayModel`在构造时绑定一次受控调用的冻结事实——能力别名、（任务/预算账户/Context Pack）标识、
Token Accounting、五维预留上限、授权与版本快照；`request()`把PydanticAI的消息与结构化输出Schema
转译为`ControlledCallPlanV1`，交`ModelGatewayService.complete()`跑完整批准链，再把归一文本以
`TextPart`回传。适配器不持Secret、不选Provider、不做授权、不自行重试。

只支持结构化输出（`NativeOutput(strict=True)`）：`output_object`缺失即失败关闭；终态非`COMPLETED`
不回传半截文本，抛`ControlledCallFailure`让Agent把失败暴露给上层，而非把`FAILED/UNKNOWN`伪装成
结果。文本输入不支持多模态`UserContent`序列，非纯文本即失败关闭。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings

from knowledge_system.modules.model_gateway.application.policy_adapter import ModelGatewayService
from knowledge_system.modules.model_gateway.domain.budget_quantity import BudgetQuantityV1
from knowledge_system.modules.model_gateway.domain.call_state import ModelCallState
from knowledge_system.modules.model_gateway.domain.chat_request import (
    ChatJsonSchemaV1,
    ChatMessageV1,
)
from knowledge_system.modules.model_gateway.domain.controlled_call import (
    ControlledCallFailure,
    ControlledCallPlanV1,
)
from knowledge_system.modules.model_gateway.domain.token_accounting import TokenAccountingV1

DEFAULT_MAX_TOKENS = 4096


class GatewayModel(Model):
    """绑定一次受控调用冻结事实的PydanticAI `Model`适配器。"""

    def __init__(
        self,
        service: ModelGatewayService,
        *,
        capability_alias: str,
        task_id: UUID,
        account_id: UUID,
        context_pack_id: UUID,
        context_pack_hash: str,
        context_pack_input_digest: str,
        token_accounting: TokenAccountingV1,
        reserved: BudgetQuantityV1,
        prompt_version: str,
        context_policy_version: str,
        runtime_version: str,
        agent_manifest_version: str,
        authorization_version: int,
        execution_generation: int,
        cancellation_generation: int,
        expires_in_seconds: int,
        model_name: str | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._capability_alias = capability_alias
        self._task_id = task_id
        self._account_id = account_id
        self._context_pack_id = context_pack_id
        self._context_pack_hash = context_pack_hash
        self._context_pack_input_digest = context_pack_input_digest
        self._token_accounting = token_accounting
        self._reserved = reserved
        self._prompt_version = prompt_version
        self._context_policy_version = context_policy_version
        self._runtime_version = runtime_version
        self._agent_manifest_version = agent_manifest_version
        self._authorization_version = authorization_version
        self._execution_generation = execution_generation
        self._cancellation_generation = cancellation_generation
        self._expires_in_seconds = expires_in_seconds
        self._model_name = model_name or f"knowledge:{capability_alias}"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def system(self) -> str:
        return "alibaba_bailian"

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        output_object = model_request_parameters.output_object
        if output_object is None:
            raise ControlledCallFailure("MODEL_STRUCTURED_OUTPUT_REQUIRED")

        max_tokens = (
            model_settings.get("max_tokens") if model_settings is not None else None
        ) or DEFAULT_MAX_TOKENS
        response_schema = ChatJsonSchemaV1(
            name=output_object.name or "structured_output",
            strict=True,
            schema=output_object.json_schema,
        )
        plan = ControlledCallPlanV1(
            model_call_id=uuid4(),
            task_id=self._task_id,
            account_id=self._account_id,
            context_pack_id=self._context_pack_id,
            context_pack_hash=self._context_pack_hash,
            context_pack_input_digest=self._context_pack_input_digest,
            capability_alias=self._capability_alias,
            messages=self._extract_messages(messages),
            max_tokens=max_tokens,
            response_schema=response_schema,
            token_accounting=self._token_accounting,
            reserved=self._reserved,
            prompt_version=self._prompt_version,
            context_policy_version=self._context_policy_version,
            runtime_version=self._runtime_version,
            agent_manifest_version=self._agent_manifest_version,
            authorization_version=self._authorization_version,
            execution_generation=self._execution_generation,
            cancellation_generation=self._cancellation_generation,
            expires_in_seconds=self._expires_in_seconds,
        )
        result = await self._service.complete(plan)
        if result.status is not ModelCallState.COMPLETED:
            raise ControlledCallFailure(result.error_code or "MODEL_CALL_NOT_COMPLETED")
        return ModelResponse([TextPart(result.text)])

    def _extract_messages(self, messages: list[ModelMessage]) -> tuple[ChatMessageV1, ...]:
        chat: list[ChatMessageV1] = []
        for message in messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, SystemPromptPart):
                        chat.append(ChatMessageV1(role="system", content=part.content))
                    elif isinstance(part, UserPromptPart):
                        if not isinstance(part.content, str):
                            raise ControlledCallFailure("MODEL_INPUT_NON_TEXT_UNSUPPORTED")
                        chat.append(ChatMessageV1(role="user", content=part.content))
            else:
                for response_part in message.parts:
                    if isinstance(response_part, TextPart):
                        chat.append(
                            ChatMessageV1(role="assistant", content=response_part.content)
                        )
        if not chat:
            raise ControlledCallFailure("MODEL_INPUT_EMPTY")
        return tuple(chat)


__all__ = ["DEFAULT_MAX_TOKENS", "GatewayModel"]
