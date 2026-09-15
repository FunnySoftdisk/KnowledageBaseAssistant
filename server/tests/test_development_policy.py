import unittest

from knowledge_system.modules.model_gateway.domain.development_policy import (
    DevelopmentPolicyViolation,
    validate_estimated_budget,
    validate_trial_input,
)


class DevelopmentPolicyTests(unittest.TestCase):
    def test_input_size_uses_utf8_bytes(self) -> None:
        validate_trial_input(
            app_env="development", data_scope="PUBLIC_DEVELOPMENT", text="a" * 8192
        )
        for text in ("a" * 8193, "中" * 2731):
            with (
                self.subTest(length=len(text)),
                self.assertRaises(DevelopmentPolicyViolation) as ctx,
            ):
                validate_trial_input(
                    app_env="development", data_scope="PUBLIC_DEVELOPMENT", text=text
                )
            self.assertEqual(ctx.exception.code, "DEV_TRIAL_INPUT_TOO_LARGE")

    def test_invalid_input_and_environment(self) -> None:
        for env, scope, text in (
            ("production", "PUBLIC_DEVELOPMENT", "目标"),
            ("test", "PUBLIC_DEVELOPMENT", "目标"),
            ("development", "INTERNAL_PRODUCTION", "目标"),
            ("development", "APPROVED_EXTERNAL_TRIAL", "目标"),
            ("development", "PUBLIC_DEVELOPMENT", " \n"),
            ("development", "PUBLIC_DEVELOPMENT", "\ud800"),
        ):
            with self.subTest(env=env, scope=scope), self.assertRaises(DevelopmentPolicyViolation):
                validate_trial_input(app_env=env, data_scope=scope, text=text)

    def test_exact_80_percent_boundary_and_stage_limit(self) -> None:
        common = dict(
            app_env="development",
            capability_alias="planning_strong",
            development_manifest_authorized=True,
            requires_exact_tokens=False,
            estimated_window_tokens=100,
            stage_input_limit=80,
            max_output_tokens=20,
        )
        validate_estimated_budget(**common, estimated_input_tokens=80)
        for count in (81, -1, True, 1.5):
            with self.subTest(count=count), self.assertRaises(DevelopmentPolicyViolation):
                validate_estimated_budget(**common, estimated_input_tokens=count)
        with self.assertRaises(DevelopmentPolicyViolation):
            validate_estimated_budget(
                **{**common, "stage_input_limit": 79}, estimated_input_tokens=80
            )
        with self.assertRaises(DevelopmentPolicyViolation):
            validate_estimated_budget(
                **{**common, "max_output_tokens": 21}, estimated_input_tokens=80
            )

    def test_no_production_or_special_profile_estimation(self) -> None:
        common = dict(
            app_env="development",
            capability_alias="synthesis_strong",
            development_manifest_authorized=True,
            requires_exact_tokens=False,
            estimated_input_tokens=1,
            estimated_window_tokens=100,
            stage_input_limit=80,
            max_output_tokens=20,
        )
        for patch in (
            {"app_env": "production"},
            {"capability_alias": "external_research"},
            {"capability_alias": "knowledge_research"},
            {"requires_exact_tokens": True},
            {"development_manifest_authorized": False},
            {"estimated_window_tokens": 0},
            {"development_manifest_authorized": "true"},
            {"requires_exact_tokens": 0},
            {"max_output_tokens": False},
        ):
            with self.subTest(patch=patch), self.assertRaises(DevelopmentPolicyViolation):
                validate_estimated_budget(**{**common, **patch})


if __name__ == "__main__":
    unittest.main()
