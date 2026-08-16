from __future__ import annotations

import copy
import math
from typing import Any


DEFAULT_PROFILE_ID = "naca64-414-strict-lift-bound-v1"
NO_CANDIDATE_PRESET = "none"


RECOMMENDED_VALUES: dict[str, Any] = {
    "advanced_settings.optimizer.objective_strategy": "drag-with-lift-bound",
    "advanced_settings.optimizer.drag_step_percent": -0.15,
    "advanced_settings.optimizer.lift_runtime_bound_ratio": 0.999,
    "advanced_settings.optimizer.lift_bound_tolerance_percent": 0.02,
    "completion.minimum_lift_ratio": 0.998,
    "completion.require_stepwise_lift_to_drag_improvement": True,
    "completion.performance_targets_enabled": True,
    "optimization_run.max_accepted_design_steps": 12,
    "optimization_run.candidate_selection_policy": "first-pass",
    "optimization_run.accept_recovered_attempts": False,
    "optimization_run.strict_clean_morphing": True,
    "optimization_run.repair_on_profile_exhaustion": False,
    "advanced_settings.design_tool.x_control_points": 24,
    "advanced_settings.design_tool.y_control_points": 8,
    "advanced_settings.design_tool.morpher.method": "radial-basis-function",
    "advanced_settings.design_tool.morpher.rbf.max_iterations": 10,
    "advanced_settings.design_tool.morpher.rbf.max_subiteration": 100,
    "advanced_settings.design_tool.morpher.rbf.linear_solver_tolerance": 1.0e-5,
    "advanced_settings.design_tool.morpher.rbf.number_of_modes": 40,
    "advanced_settings.design_tool.shape_anchors.mode": "endpoints-only",
    "advanced_settings.design_tool.thickness_constraint.clearance_percent_of_baseline_max_thickness": 5.0,
}


def _variant(
    preset_id: str,
    label: str,
    evidence: str,
    risk: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": preset_id,
        "label": label,
        "evidence": evidence,
        "applicability": "NACA64-414, 32.5 m/s, altitude 0 m, AoA 2 deg, chord 1 m, y+=1",
        "risk": risk,
        "overrides": overrides,
    }


RESEARCH_CANDIDATE_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "id": "coupled-c4",
        "label": "双 step-size C4",
        "variants": (
            _variant(
                "coupled-c4",
                "Cd -0.05%, Cl +0.03%",
                "verified-research",
                "可复现微小 L/D 收益，但未达到累计性能目标；不得自动提升为默认。",
                {
                    "advanced_settings.optimizer.objective_strategy": "coupled-drag-lift-step",
                    "advanced_settings.optimizer.drag_step_percent": -0.05,
                    "advanced_settings.optimizer.lift_step_percent": 0.03,
                    "retry_profiles": [],
                },
            ),
        ),
    },
    {
        "id": "lift-bound-b3",
        "label": "升力下限 B3",
        "variants": (
            _variant(
                "lift-bound-b3",
                "Cd -0.10%, Cl bound 99.97%",
                "verified-research",
                "仅在当前离散筛选点中表现最好，不能解释为连续意义上的最优容忍值。",
                {
                    "advanced_settings.optimizer.objective_strategy": "drag-with-lift-bound",
                    "advanced_settings.optimizer.drag_step_percent": -0.10,
                    "advanced_settings.optimizer.lift_runtime_bound_ratio": 0.9997,
                    "advanced_settings.optimizer.lift_bound_tolerance_percent": 0.02,
                    "retry_profiles": [],
                },
            ),
        ),
    },
    {
        "id": "control-point-study",
        "label": "控制点辨识",
        "variants": tuple(
            _variant(
                f"control-points-{x}x{y}",
                f"控制点 {x}x{y}",
                "unverified-hypothesis",
                "改变设计自由度和灵敏度空间；必须重新进行严格真实验证。",
                {
                    "advanced_settings.design_tool.x_control_points": x,
                    "advanced_settings.design_tool.y_control_points": y,
                    "retry_profiles": [],
                },
            )
            for x, y in ((16, 8), (24, 6), (24, 12), (32, 8))
        ),
    },
    {
        "id": "relaxed-lift-study",
        "label": "放宽升力门槛",
        "variants": (
            _variant(
                "relaxed-lift-99.5",
                "最终 Cl 99.5%, 内部下限 99.6%",
                "unverified-hypothesis",
                "改变用户气动验收约束，可能提高降阻但允许更多升力损失。",
                {
                    "completion.minimum_lift_ratio": 0.995,
                    "advanced_settings.optimizer.lift_runtime_bound_ratio": 0.996,
                    "retry_profiles": [],
                },
            ),
            _variant(
                "relaxed-lift-99.0",
                "最终 Cl 99.0%, 内部下限 99.1%",
                "unverified-hypothesis",
                "显著改变用户气动验收约束，只能作为显式研究选择。",
                {
                    "completion.minimum_lift_ratio": 0.990,
                    "advanced_settings.optimizer.lift_runtime_bound_ratio": 0.991,
                    "retry_profiles": [],
                },
            ),
        ),
    },
    {
        "id": "relaxed-thickness-study",
        "label": "放宽全弦厚度余量",
        "variants": (
            _variant(
                "relaxed-thickness-7.5",
                "全弦厚度余量 7.5%",
                "unverified-hypothesis",
                "允许更多局部减薄；仍执行全弦几何门槛而非只检查最大厚度。",
                {"advanced_settings.design_tool.thickness_constraint.clearance_percent_of_baseline_max_thickness": 7.5, "retry_profiles": []},
            ),
            _variant(
                "relaxed-thickness-10",
                "全弦厚度余量 10%",
                "unverified-hypothesis",
                "允许更多局部减薄；必须重新检查结构/制造约束。",
                {"advanced_settings.design_tool.thickness_constraint.clearance_percent_of_baseline_max_thickness": 10.0, "retry_profiles": []},
            ),
        ),
    },
)


def deep_get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def deep_set(data: dict[str, Any], dotted: str, value: Any) -> None:
    current: Any = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def candidate_variants() -> list[dict[str, Any]]:
    return [copy.deepcopy(variant) for group in RESEARCH_CANDIDATE_GROUPS for variant in group["variants"]]


def candidate_preset_ids() -> list[str]:
    return [variant["id"] for variant in candidate_variants()]


def candidate_catalog() -> list[dict[str, Any]]:
    return copy.deepcopy(list(RESEARCH_CANDIDATE_GROUPS))


def apply_candidate_preset(cfg: dict[str, Any], preset_id: str) -> list[dict[str, Any]]:
    variants = {variant["id"]: variant for variant in candidate_variants()}
    if preset_id not in variants:
        raise ValueError(f"未知研究候选 {preset_id!r}；可用值: {', '.join(sorted(variants))}")
    changes: list[dict[str, Any]] = []
    for path, value in variants[preset_id]["overrides"].items():
        before = copy.deepcopy(deep_get(cfg, path))
        deep_set(cfg, path, copy.deepcopy(value))
        if before != value:
            changes.append({"path": path, "before": before, "after": copy.deepcopy(value), "source": "candidate-preset"})
    return changes


def validate_aerodynamic_controls(cfg: dict[str, Any]) -> list[str]:
    drag_step = deep_get(cfg, "advanced_settings.optimizer.drag_step_percent")
    runtime_ratio = deep_get(cfg, "advanced_settings.optimizer.lift_runtime_bound_ratio")
    final_ratio = deep_get(cfg, "completion.minimum_lift_ratio")
    for name, value in (
        ("Cd target step", drag_step),
        ("Fluent lift bound", runtime_ratio),
        ("final lift gate", final_ratio),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError(f"{name} must be a finite number")
    if float(drag_step) >= 0.0:
        raise ValueError("Cd target step must be negative")
    if not 0.0 < float(runtime_ratio) <= 1.0:
        raise ValueError("Fluent lift bound must be in (0, 1]")
    if not 0.0 < float(final_ratio) <= 1.0:
        raise ValueError("final lift gate must be in (0, 1]")
    if float(runtime_ratio) < float(final_ratio):
        raise ValueError("Fluent lift bound must not be lower than the final lift gate")
    buffer_percentage_points = 100.0 * (float(runtime_ratio) - float(final_ratio))
    warnings: list[str] = []
    if buffer_percentage_points < 0.1 - 1.0e-12:
        warnings.append(
            "Fluent 内部 Cl 下限与最终门槛的复算缓冲小于推荐的 0.1 个百分点"
        )
    return warnings


def validate_control_points(cfg: dict[str, Any]) -> dict[str, int]:
    values = {
        "x": deep_get(cfg, "advanced_settings.design_tool.x_control_points", deep_get(cfg, "optimizer.x_control_points")),
        "y": deep_get(cfg, "advanced_settings.design_tool.y_control_points", deep_get(cfg, "optimizer.y_control_points")),
    }
    for axis, value in values.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"advanced design-tool {axis} control points must be a positive integer")
    return {"x": int(values["x"]), "y": int(values["y"])}


def validate_control_point_motion(cfg: dict[str, Any]) -> str:
    value = str(cfg.get("control_point_motion", "")).strip().lower()
    if value not in {"x-only", "y-only", "xy"}:
        raise ValueError("control_point_motion is required and must be x-only, y-only, or xy")
    return value


def validate_removed_envelope_config(cfg: dict[str, Any]) -> None:
    """Reject removed envelope settings instead of silently changing old jobs."""
    if "envelope" in cfg:
        raise ValueError(
            "配置迁移错误：顶层 envelope 已删除；请改用 "
            "advanced_settings.design_tool.thickness_constraint。"
        )
    thickness = deep_get(cfg, "advanced_settings.design_tool.thickness_constraint", {}) or {}
    if thickness.get("mode") == "fluent-envelope":
        raise ValueError(
            "配置迁移错误：fluent-envelope 已删除；请删除 mode，并配置 enabled、"
            "clearance_percent_of_baseline_max_thickness 和 samples。"
        )


def recommended_profile_differences(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for path, recommended in RECOMMENDED_VALUES.items():
        actual = deep_get(cfg, path)
        if actual != recommended:
            differences.append({"path": path, "recommended": copy.deepcopy(recommended), "actual": copy.deepcopy(actual)})
    expected_retry_steps = [-0.10, -0.05, -0.02, -0.01]
    actual_retry_steps = [item.get("drag_step_percent") for item in cfg.get("retry_profiles", []) if isinstance(item, dict)]
    if actual_retry_steps != expected_retry_steps:
        differences.append({"path": "retry_profiles.drag_step_percent", "recommended": expected_retry_steps, "actual": actual_retry_steps})
    return differences


def build_optimization_profile(cfg: dict[str, Any], interaction: dict[str, Any] | None = None) -> dict[str, Any]:
    interaction = interaction or {}
    differences = recommended_profile_differences(cfg)
    selected_candidate = interaction.get("candidate_preset") or NO_CANDIDATE_PRESET
    return {
        "id": DEFAULT_PROFILE_ID,
        "source_report": "NACA64-414_严格升力多目标降阻验证报告.md",
        "recommended_default": not differences and selected_candidate == NO_CANDIDATE_PRESET,
        "matches_recommended_default": not differences,
        "selected_candidate_preset": selected_candidate,
        "aerodynamic_controls": {
            "drag_step_percent": deep_get(cfg, "advanced_settings.optimizer.drag_step_percent"),
            "lift_runtime_bound_percent": 100.0 * float(deep_get(cfg, "advanced_settings.optimizer.lift_runtime_bound_ratio", 0.0)),
            "minimum_lift_percent": 100.0 * float(deep_get(cfg, "completion.minimum_lift_ratio", 0.0)),
        },
        "control_points": {
            "x": deep_get(cfg, "advanced_settings.design_tool.x_control_points", deep_get(cfg, "optimizer.x_control_points")),
            "y": deep_get(cfg, "advanced_settings.design_tool.y_control_points", deep_get(cfg, "optimizer.y_control_points")),
        },
        "retry_drag_steps_percent": [
            item.get("drag_step_percent") for item in cfg.get("retry_profiles", []) if isinstance(item, dict)
        ],
        "differences_from_recommended": differences,
        "validation_warnings": list(interaction.get("validation_warnings") or []),
    }
