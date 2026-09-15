"""真实SDK的离线接口探针；FunctionModel是测试替身，不是已接通的网关。"""

import unittest
from datetime import timedelta
from importlib.metadata import version

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.durable_exec.temporal import (
    AgentPlugin,
    PydanticAIPlugin,
    TemporalDurability,
)
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig


class ProbeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    recommendation: str


class SDKContractTests(unittest.IsolatedAsyncioTestCase):
    def test_installed_runtime_versions_match_approved_pins(self) -> None:
        expected = {
            "pydantic-ai-slim": "2.42.0",
            "temporalio": "1.32.0",
            "pydantic": "2.13.5",
            "httpx": "0.28.1",
        }
        for distribution, pin in expected.items():
            with self.subTest(distribution=distribution):
                self.assertEqual(version(distribution), pin)

    async def test_native_strict_output_does_not_advertise_tools(self) -> None:
        calls = 0

        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            self.assertTrue(messages)
            self.assertEqual(info.function_tools, [])
            self.assertEqual(info.output_tools, [])
            params = info.model_request_parameters
            self.assertEqual(params.native_tools, [])
            self.assertEqual(params.output_mode, "native")
            self.assertIsNotNone(params.output_object)
            assert params.output_object is not None
            self.assertIs(params.output_object.strict, True)
            self.assertIs(params.output_object.json_schema["additionalProperties"], False)
            return ModelResponse([TextPart('{"recommendation":"先列出本周工作目标"}')])

        agent = Agent(
            FunctionModel(respond),
            name="m0-sdk-contract-probe",
            output_type=NativeOutput(ProbeOutput, strict=True),
            tools=[],
            retries=0,
        )
        agent.instrument = False
        result = await agent.run("离线测试输入，不含业务数据")
        self.assertEqual(result.output.recommendation, "先列出本周工作目标")
        self.assertEqual(calls, 1)

    async def test_invalid_output_is_rejected_without_a_second_model_request(self) -> None:
        for body in (
            "not-json",
            '{"recommendation":"测试","unexpected":true}',
            '{"recommendation":123}',
        ):
            with self.subTest(body=body):
                calls = 0

                async def respond(
                    messages: list[ModelMessage], info: AgentInfo, response_body: str = body
                ) -> ModelResponse:
                    nonlocal calls
                    calls += 1
                    return ModelResponse([TextPart(response_body)])

                agent = Agent(
                    FunctionModel(respond),
                    name="m0-sdk-invalid-output-probe",
                    output_type=NativeOutput(ProbeOutput, strict=True),
                    tools=[],
                    retries=0,
                )
                agent.instrument = False
                with self.assertRaises(UnexpectedModelBehavior):
                    await agent.run("离线非法输出探针")
                self.assertEqual(calls, 1)

    def test_temporal_capability_binds_and_registers_without_connecting(self) -> None:
        def never_execute(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.fail("仅注册接口，不应执行模型")

        durability = TemporalDurability(
            model_activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        )
        agent = Agent(
            FunctionModel(never_execute),
            name="m0-sdk-temporal-registration-probe",
            output_type=NativeOutput(ProbeOutput, strict=True),
            tools=[],
            retries=0,
            capabilities=[durability],
        )
        agent.instrument = False
        bound = TemporalDurability.from_agent(agent)
        self.assertIsNotNone(bound)
        assert bound is not None
        self.assertTrue(bound.temporal_activities)
        self.assertTrue(all(callable(activity) for activity in bound.temporal_activities))
        self.assertIsInstance(PydanticAIPlugin(), PydanticAIPlugin)
        self.assertIsInstance(AgentPlugin(agent), AgentPlugin)


if __name__ == "__main__":
    unittest.main()
