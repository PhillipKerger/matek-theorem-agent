from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ascend_math_agent.config import (
    AppConfig,
    ConfigError,
    config_as_toml,
    consume_config_migration_notice,
    load_config,
    merge_config,
    resolve_backend_provider,
)


def test_config_precedence_defaults_project_environment_cli(tmp_path: Path) -> None:
    path = tmp_path / "ascend.toml"
    path.write_text(
        """
[research]
maximum_rounds = 2
maximum_concurrent_agents = 5
""",
        encoding="utf-8",
    )

    config = load_config(
        path,
        env={"ASCEND_RESEARCH__MAXIMUM_ROUNDS": "3"},
        cli_overrides={"max_rounds": 4},
    )

    assert config.research.minimum_initial_agents == 4  # default
    assert config.research.maximum_concurrent_agents == 5  # project
    assert config.research.maximum_rounds == 4  # CLI beats environment


def test_backend_precedence_is_cli_environment_toml_then_codex(tmp_path: Path) -> None:
    assert AppConfig().backend.provider == "codex"
    path = tmp_path / "ascend.toml"
    path.write_text(
        'config_version = 2\n[backend]\nprovider = "api"\n',
        encoding="utf-8",
    )

    assert load_config(path, env={}).backend.provider == "api"
    assert load_config(path, env={"ASCEND_BACKEND": "codex"}).backend.provider == "codex"
    assert (
        load_config(
            path,
            env={"ASCEND_BACKEND": "codex"},
            cli_overrides={"backend": "api"},
        ).backend.provider
        == "api"
    )


def test_backend_resolver_has_no_auto_or_implicit_api_fallback() -> None:
    config = AppConfig()
    assert resolve_backend_provider(config, environment={}) == "codex"
    assert resolve_backend_provider(config, environment={"ASCEND_BACKEND": "api"}) == "api"
    assert (
        resolve_backend_provider(
            config,
            environment={"ASCEND_BACKEND": "api"},
            cli_provider="codex",
        )
        == "codex"
    )
    with pytest.raises(ConfigError, match=r"codex.*api"):
        resolve_backend_provider(config, environment={"ASCEND_BACKEND": "auto"})


def test_config_environment_parses_strict_bool_and_list(tmp_path: Path) -> None:
    config = load_config(
        project_root=tmp_path,
        env={
            "ASCEND_LEAN__ENABLED": "false",
            "ASCEND_LEAN__APPROVED_AXIOMS": '["propext"]',
        },
    )
    assert not config.lean.enabled
    assert config.lean.approved_axioms == ["propext"]


def test_codex_environment_overrides_use_documented_names(tmp_path: Path) -> None:
    config = load_config(
        project_root=tmp_path,
        env={
            "ASCEND_CODEX_MODEL": "gpt-5.6",
            "ASCEND_CODEX_EXECUTABLE": "/opt/codex/bin/codex",
            "ASCEND_CODEX_LIMITS_MAX_AGENT_CALLS": "25",
        },
    )
    assert config.codex.model == "gpt-5.6"
    assert config.codex.executable == "/opt/codex/bin/codex"
    assert config.codex.limits.max_agent_calls == 25


def test_flat_environment_names_are_supported(tmp_path: Path) -> None:
    config = load_config(
        project_root=tmp_path,
        env={"ASCEND_MODELS_PROMPT_COMPILER_MAX_OUTPUT_TOKENS": "123"},
    )
    assert config.models.prompt_compiler.max_output_tokens == 123


def test_legacy_api_environment_names_remain_supported(tmp_path: Path) -> None:
    config = load_config(
        project_root=tmp_path,
        env={
            "ASCEND_MODELS__AUDIT__WEB_SEARCH": "false",
            "ASCEND_LIMITS__MAXIMUM_API_RETRIES": "7",
        },
    )
    assert not config.api.models.audit.web_search
    assert config.api.limits.maximum_api_retries == 7


def test_no_lean_convenience_is_inverted(tmp_path: Path) -> None:
    assert not load_config(project_root=tmp_path, env={"ASCEND_NO_LEAN": "true"}).lean.enabled
    assert not merge_config(AppConfig(), {"no_lean": True}).lean.enabled


def test_no_web_search_disables_every_model_stage_and_defaults_remain_enabled(
    tmp_path: Path,
) -> None:
    default = AppConfig()
    assert default.web_search_enabled
    assert all(
        settings.web_search
        for settings in (
            default.models.prompt_compiler,
            default.models.research,
            default.models.audit,
            default.models.manuscript,
        )
    )

    cli_disabled = merge_config(default, {"no_web_search": True})
    env_disabled = load_config(
        project_root=tmp_path,
        env={"ASCEND_NO_WEB_SEARCH": "true"},
    )
    for config in (cli_disabled, env_disabled):
        assert not config.web_search_enabled
        assert not any(
            settings.web_search
            for settings in (
                config.models.prompt_compiler,
                config.models.research,
                config.models.audit,
                config.models.manuscript,
            )
        )


def test_no_web_search_override_requires_a_boolean() -> None:
    with pytest.raises(ConfigError, match="no_web_search must be a boolean"):
        merge_config(AppConfig(), {"no_web_search": "true"})


def test_total_time_limit_updates_both_backends_and_environment(tmp_path: Path) -> None:
    default = AppConfig()
    configured = merge_config(AppConfig(), {"time_limit_minutes": 45})
    from_environment = load_config(
        project_root=tmp_path,
        env={"ASCEND_TIME_LIMIT_MINUTES": "30"},
    )

    assert default.codex.limits.max_wall_clock_minutes is None
    assert default.limits.maximum_wall_clock_hours is None
    assert configured.codex.limits.max_wall_clock_minutes == 45
    assert configured.limits.maximum_wall_clock_hours == 0.75
    assert from_environment.codex.limits.max_wall_clock_minutes == 30
    assert from_environment.limits.maximum_wall_clock_hours == 0.5


@pytest.mark.parametrize("value", [0, True, "30"])
def test_total_time_limit_rejects_invalid_cli_values(value: object) -> None:
    with pytest.raises(ConfigError, match="time_limit_minutes"):
        merge_config(AppConfig(), {"time_limit_minutes": value})


def test_nested_and_dotted_cli_overrides_are_supported() -> None:
    nested = merge_config(AppConfig(), {"research": {"maximum_rounds": 3}})
    dotted = merge_config(AppConfig(), {"models.audit.web_search": False})
    assert nested.research.maximum_rounds == 3
    assert not dotted.models.audit.web_search


def test_toml_values_are_not_silently_coerced(tmp_path: Path) -> None:
    path = tmp_path / "ascend.toml"
    path.write_text('[research]\nmaximum_rounds = "4"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="maximum_rounds"):
        load_config(path, env={})


def test_direct_settings_are_strict() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"lean": {"enabled": "false"}})


def test_automatic_backend_fallback_is_rejected() -> None:
    with pytest.raises(ValidationError, match="select the API backend explicitly"):
        AppConfig.model_validate({"backend": {"allow_automatic_fallback": True}})


@pytest.mark.parametrize(
    "argument",
    [
        "--sandbox",
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-schema=/tmp/schema.json",
        "--config",
        "--model",
        "--search",
        "problem text",
    ],
)
def test_codex_extra_args_reject_control_and_positional_arguments(argument: str) -> None:
    with pytest.raises(ValidationError, match="ASCEND-controlled"):
        AppConfig.model_validate({"codex": {"extra_args": [argument]}})


def test_codex_extra_args_allow_only_safe_color_presentation() -> None:
    config = AppConfig.model_validate({"codex": {"extra_args": ["--color", "never"]}})
    assert config.codex.extra_args == ["--color", "never"]


def test_project_edits_cannot_be_enabled_by_config_or_environment(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="explicit --allow-project-edits"):
        AppConfig.model_validate({"lean": {"allow_project_edits": True}})
    with pytest.raises(ConfigError, match="explicit --allow-project-edits"):
        load_config(
            project_root=tmp_path,
            env={"ASCEND_LEAN__ALLOW_PROJECT_EDITS": "true"},
        )


def test_config_validates_numeric_ranges(tmp_path: Path) -> None:
    path = tmp_path / "ascend.toml"
    path.write_text(
        "[research]\nmaximum_concurrent_agents = 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="maximum_concurrent_agents"):
        load_config(path, env={})


def test_default_pricing_is_explicit_and_configurable() -> None:
    config = AppConfig()
    sol = config.pricing.models["gpt-5.6-sol"]
    assert sol.input_per_million == 5.0
    assert sol.output_per_million == 30.0
    assert sol.web_search_per_call == 0.01
    updated = merge_config(
        config,
        {"pricing": {"models": {"gpt-5.6-sol": {"output_per_million": 31.0}}}},
    )
    assert updated.pricing.models["gpt-5.6-sol"].output_per_million == 31.0


def test_selected_model_requires_explicit_budget_pricing() -> None:
    with pytest.raises(ValidationError, match=r"pricing\.models"):
        AppConfig.model_validate({"models": {"research": {"model": "custom-research-model"}}})


def test_codex_backend_is_not_blocked_by_unused_api_pricing() -> None:
    config = AppConfig.model_validate(
        {
            "backend": {"provider": "codex"},
            "api": {"models": {"research": {"model": "custom-research-model"}}},
        }
    )
    assert config.backend.provider == "codex"


def test_legacy_api_config_is_migrated_without_discarding_values(tmp_path: Path) -> None:
    path = tmp_path / "ascend.toml"
    path.write_text(
        """
[models.research]
model = "gpt-5.6-terra"
reasoning_effort = "high"

[limits]
maximum_cost_usd = 42.5
maximum_api_retries = 9
""",
        encoding="utf-8",
    )

    config = load_config(path, env={})

    assert config.config_version == 2
    assert config.backend.provider == "api"
    assert config.api.models.research.model == "gpt-5.6-terra"
    assert config.api.models.research.reasoning_effort == "high"
    assert config.api.limits.maximum_cost_usd == 42.5
    assert config.api.limits.maximum_api_retries == 9
    assert config.migration_notice is not None

    rendered = config_as_toml(config)
    assert "[api.models.research]" in rendered
    assert '[backend]\nprovider = "api"' in rendered
    assert "migration_notice" not in rendered


def test_legacy_migration_notice_is_consumed_once_per_project(tmp_path: Path) -> None:
    path = tmp_path / "ascend.toml"
    path.write_text('[models.research]\nmodel = "gpt-5.6-sol"\n', encoding="utf-8")
    config = load_config(path, env={})

    first = consume_config_migration_notice(config)
    second = consume_config_migration_notice(config)

    assert first is not None
    assert "preserved the OpenAI API backend" in first
    assert second is None
    assert (tmp_path / ".ascend" / "config-migrations" / "backend-provider-v2").is_file()


def test_explicit_backend_beats_legacy_inference(tmp_path: Path) -> None:
    path = tmp_path / "ascend.toml"
    path.write_text(
        '[backend]\nprovider = "codex"\n[models.research]\nmodel = "gpt-5.6-sol"\n',
        encoding="utf-8",
    )
    config = load_config(path, env={})
    assert config.backend.provider == "codex"
    assert config.migration_notice is None


def test_future_config_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ascend.toml"
    path.write_text("config_version = 999\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="config_version"):
        load_config(path, env={})


def test_checked_in_example_config_loads() -> None:
    example = Path(__file__).parents[1] / "ascend.example.toml"
    config = load_config(example, env={})

    assert config.config_version == 2
    assert config.backend.provider == "codex"
    assert config.codex.max_parallel_agents == 8
    assert config.models.research.model == "gpt-5.6-sol"
    assert config.lean.docker_image == "ascend-math-agent:latest"
    assert set(config.pricing.models) == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    }


def test_resolved_toml_omits_runtime_root_and_none(tmp_path: Path) -> None:
    config = load_config(project_root=tmp_path, env={})
    rendered = config_as_toml(config)
    assert "project_root" not in rendered
    assert "maximum_total_tokens" not in rendered
    round_trip_path = tmp_path / "resolved.toml"
    round_trip_path.write_text(rendered, encoding="utf-8")
    round_trip = load_config(round_trip_path, env={})
    assert round_trip.model_dump(exclude={"project_root"}) == config.model_dump(
        exclude={"project_root"}
    )


def test_unknown_nested_environment_key_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown configuration key"):
        load_config(project_root=tmp_path, env={"ASCEND_LIMITS__TYPO": "1"})
