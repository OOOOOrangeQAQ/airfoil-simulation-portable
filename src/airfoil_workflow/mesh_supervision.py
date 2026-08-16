"""Run-scoped, AI-supervised C-grid candidate lifecycle.

The workflow owns validation, evidence and isolation.  The calling AI owns the
engineering judgement expressed in proposals and acceptance decisions.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .fsm import atomic_json, directory_lock, read_checkpoint, read_json, transition, utc_now, write_checkpoint


PACKAGE_ROOT = Path(__file__).resolve().parent
ENGINE_ROOT = PACKAGE_ROOT / "engine"
SKILL_ROOT = PACKAGE_ROOT.parents[1] / "ai_contract" / "skills" / "optimize-airfoil-cgrid"
MESH_ARTIFACTS = "mesh_candidates"
WORKSPACE_NAME = "mesh_agent_workspace"
FIXED_FALLBACK_ATTEMPT_ID = "attempt_fixed_fallback"
_HASHED_SUFFIXES = {".py", ".json", ".toml", ".yaml", ".yml", ".ps1"}
_ALLOWED_PREFIXES = (
    "airfoil_fluentmeshing/cgrid/",
    "scripts/cgrid/",
)
_ALLOWED_FILES = {
    "airfoil_fluentmeshing/mesh_policy.py",
    "airfoil_fluentmeshing/boundary_layer.py",
}

_INTEGER_PARAMETERS: dict[str, tuple[int, int]] = {
    "n_airfoil_side": (8, 5000),
    "n_bridge": (2, 1000),
    "radial_layers": (4, 1000),
    "wake_columns": (2, 5000),
    "bl_layers": (1, 1000),
    "cst_order": (2, 30),
    "smoothing_iterations": (0, 1000),
}
_NUMBER_PARAMETERS: dict[str, tuple[float, float]] = {
    "farfield_distance": (1.0, 1000.0),
    "wake_length": (1.0, 5000.0),
    "growth_rate": (1.0, 2.0),
    "cst_regularization": (0.0, 1.0),
    "cst_cosine_weight": (0.0, 1.0),
    "cst_le_power": (0.1, 10.0),
    "wake_beta": (0.0, 100.0),
    "wake_outer_beta": (0.0, 100.0),
    "wake_center_width": (0.001, 10.0),
    "outlet_center_cluster": (0.0, 100.0),
    "outlet_match_blend": (0.0, 1.0),
    "outlet_match_power": (0.01, 10.0),
    "inlet_arc_match_blend": (0.0, 1.0),
    "smoothing_relaxation": (0.0, 1.0),
    "minimum_te_thickness": (1.0e-8, 0.1),
}
_CHOICE_PARAMETERS = {
    "outlet_distribution_mode": {"match-left", "matched-clustered", "quality-adaptive"},
}
_PARAMETER_NAMES = set(_INTEGER_PARAMETERS) | set(_NUMBER_PARAMETERS) | set(_CHOICE_PARAMETERS)
_DEFAULT_PARAMETERS: dict[str, Any] = {
    "n_airfoil_side": 214,
    "n_bridge": 10,
    "radial_layers": 58,
    "wake_columns": 190,
    "farfield_distance": 15.0,
    "wake_length": 20.0,
    "growth_rate": 1.12,
    "bl_layers": 42,
    "cst_order": 6,
    "cst_regularization": 1.0e-5,
    "cst_cosine_weight": 0.65,
    "cst_le_power": 1.45,
    "wake_beta": 5.0,
    "wake_outer_beta": 0.8,
    "wake_center_width": 0.30,
    "outlet_center_cluster": 1.35,
    "outlet_match_blend": 0.90,
    "outlet_match_power": 0.85,
    "outlet_distribution_mode": "quality-adaptive",
    "inlet_arc_match_blend": 0.35,
    "smoothing_iterations": 8,
    "smoothing_relaxation": 0.20,
    "minimum_te_thickness": 2.5e-4,
}


class MeshSupervisionError(RuntimeError):
    """The mesh candidate lifecycle contract was violated."""


def _engine_imports() -> tuple[Any, Any, Any, Any, Any]:
    """Load the bundled engine exactly as its standalone scripts do."""
    value = str(ENGINE_ROOT)
    if value not in sys.path:
        sys.path.insert(0, value)
    from airfoil_fluentmeshing.adjoint_mesh_pipeline import (  # type: ignore[import-not-found]
        _run_cgrid_mesh_once,
        computed_flow_context,
        resolve_effective_chord,
    )
    from airfoil_fluentmeshing.geometry import read_dat, trailing_edge_report  # type: ignore[import-not-found]

    return _run_cgrid_mesh_once, computed_flow_context, resolve_effective_chord, read_dat, trailing_edge_report


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix.lower() not in _HASHED_SUFFIXES:
            continue
        result[path.relative_to(root).as_posix()] = _sha256(path)
    return result


def _tree_digest(manifest: Mapping[str, str]) -> str:
    encoded = json.dumps(dict(manifest), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_allowed_mesh_source(relative: str) -> bool:
    clean = PurePosixPath(relative).as_posix()
    return clean in _ALLOWED_FILES or any(clean.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


def _candidate_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, (minimum, maximum) in _INTEGER_PARAMETERS.items():
        properties[name] = {"type": "integer", "minimum": minimum, "maximum": maximum}
    for name, (minimum, maximum) in _NUMBER_PARAMETERS.items():
        properties[name] = {"type": "number", "minimum": minimum, "maximum": maximum}
    for name, choices in _CHOICE_PARAMETERS.items():
        properties[name] = {"type": "string", "enum": sorted(choices)}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "mesh_candidate.schema.json",
        "title": "AI supervised two-dimensional airfoil C-grid candidate",
        "description": "Distances are in input-chord multiples unless a field name states otherwise. All numeric bounds are inclusive; max_cells remains the independent hard budget.",
        "x-predicted-cell-formula": "(2*n_airfoil_side-2)*radial_layers + (2*radial_layers+max(2,n_bridge)-1)*wake_columns",
        "x-boundary-layer-contract": "first_layer_m is derived from flow physics in mesh_brief.json and is not candidate-controlled; total height is derived from first layer, growth_rate, and bl_layers",
        "x-lineage-contract": "parent_attempt is the single provenance parent; rationale may compare any number of prior attempts",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "parent_attempt", "source_mode", "rationale", "parameters"],
        "properties": {
            "schema_version": {"const": 1},
            "parent_attempt": {"oneOf": [{"type": "null"}, {"type": "string", "pattern": "^attempt_[0-9]{3}$"}]},
            "source_mode": {"enum": ["builtin", "run_patch"]},
            "rationale": {
                "type": "object",
                "additionalProperties": False,
                "required": ["observed_regions", "hypothesis", "expected_effect"],
                "properties": {
                    "observed_regions": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                    "hypothesis": {"type": "string", "minLength": 12},
                    "expected_effect": {"type": "string", "minLength": 12},
                },
            },
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(_PARAMETER_NAMES),
                "properties": properties,
            },
        },
    }


def _geometry_brief(config: Mapping[str, Any]) -> dict[str, Any]:
    _, _, _, read_dat, trailing_edge_report = _engine_imports()
    points = read_dat(config["airfoil_dat"])
    xs = [float(point.x) for point in points]
    ys = [float(point.y) for point in points]
    chord = max(xs) - min(xs)
    le_index = min(range(len(points)), key=lambda index: (points[index].x, abs(points[index].y)))
    return {
        "input_dat": str(Path(config["airfoil_dat"]).resolve()),
        "input_sha256": _sha256(Path(config["airfoil_dat"]).resolve()),
        "point_count": len(points),
        "bounds": {"x_min": min(xs), "x_max": max(xs), "y_min": min(ys), "y_max": max(ys)},
        "coordinate_chord": chord,
        "leading_edge": {"data_index": le_index, "x": points[le_index].x, "y": points[le_index].y},
        "trailing_edge": trailing_edge_report(points),
        "requested_closure": config.get("closure", "auto"),
        "note": "Indices are descriptive evidence only; candidates must rediscover geometry features from coordinates.",
    }


def _copy_engine_workspace(destination: Path) -> None:
    if destination.exists():
        return
    shutil.copytree(
        ENGINE_ROOT,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "outputs", ".pytest_cache"),
    )


def prepare_mesh_supervision(spec: Mapping[str, Any], run_root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Create the immutable brief and an isolated editable mesh source tree."""
    run_root = Path(run_root).resolve()
    artifacts = run_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    brief_path = artifacts / "mesh_brief.json"
    schema_path = artifacts / "mesh_candidate.schema.json"
    workspace = artifacts / WORKSPACE_NAME / "engine"
    _copy_engine_workspace(workspace)
    baseline = _tree_manifest(workspace)
    canonical = _tree_manifest(ENGINE_ROOT)
    baseline_path = artifacts / WORKSPACE_NAME / "source_baseline.json"
    if not baseline_path.exists():
        atomic_json(
            baseline_path,
            {
                "created_utc": utc_now(),
                "workspace_tree_sha256": _tree_digest(baseline),
                "canonical_tree_sha256": _tree_digest(canonical),
                "files": baseline,
            },
        )
    else:
        original = read_json(baseline_path)
        if original.get("workspace_tree_sha256") != _tree_digest(baseline):
            # Existing edits are expected after preparation; retain the original baseline.
            baseline = dict(original.get("files", {}))
    atomic_json(schema_path, _candidate_schema())
    _, computed_flow_context, resolve_effective_chord, _, _ = _engine_imports()
    resolved_config = copy.deepcopy(dict(config))
    effective_chord = resolve_effective_chord(resolved_config)
    flow_context = computed_flow_context(resolved_config)
    seed = {
        **_DEFAULT_PARAMETERS,
        **{
        key: value
        for key, value in dict(resolved_config.get("mesh", {}).get("cgrid", {})).items()
        if key in _PARAMETER_NAMES
        },
    }
    fixed_fallback = {
        "mode": "legacy_fixed_48458_cgrid",
        "parameters": copy.deepcopy(_DEFAULT_PARAMETERS),
        "predicted_cells": _predicted_cells(_DEFAULT_PARAMETERS),
        "automatic": False,
        "available_after": "AI candidate budget exhausted and no eligible AI candidate exists",
        "generator_source": "canonical packaged engine; run-scoped mesh patches are ignored",
    }
    fixed_fallback["parameters_sha256"] = hashlib.sha256(
        json.dumps(_DEFAULT_PARAMETERS, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    brief = {
        "schema_version": 1,
        "run_id": run_root.name,
        "status": "WAITING_FOR_AI_MESH",
        "skill": {
            "name": "optimize-airfoil-cgrid",
            "path": str(SKILL_ROOT.resolve()),
            "entrypoint": str((SKILL_ROOT / "SKILL.md").resolve()),
        },
        "geometry": _geometry_brief(resolved_config),
        "flow": {
            **copy.deepcopy(resolved_config.get("flow", {})),
            "turbulence_model": resolved_config.get("advanced_settings", {}).get("solver", {}).get("viscous_model"),
            "first_layer_derivation": flow_context,
            "effective_chord": effective_chord,
        },
        "budget": {
            "preferred_cells": spec["mesh"].get("preferred_cells"),
            "max_cells": spec["mesh"]["max_cells"],
            "max_candidates": spec["mesh"]["max_candidates"],
            "pilot_iterations": 100,
            "pilot_record_interval": 10,
        },
        "generator": {
            "topology": "structured_2d_cgrid_pure_quadrilateral",
            "required_zones": ["airfoil", "velocity_inlet", "pressure_outlet"],
            "supported_parameters": sorted(_PARAMETER_NAMES),
            "baseline_seed_only_not_automatic": seed,
            "legacy_fixed_fallback": fixed_fallback,
            "isolated_engine_root": str(workspace),
            "allowed_source_paths": [*_ALLOWED_PREFIXES, *sorted(_ALLOWED_FILES)],
            "workspace_baseline_sha256": _tree_digest(baseline),
            "canonical_engine_sha256": _tree_digest(canonical),
        },
        "quality_policy": {
            "hard_failures": [
                "non_finite_coordinates", "folded_or_self_intersecting_cells", "non_positive_jacobian",
                "negative_or_zero_cell_measure", "invalid_connectivity_or_boundary_zones", "fluent_read_failure",
                "non_quadrilateral_cells", "cell_budget_exceeded",
            ],
            "minimum_fluent_orthogonal_quality_strictly_greater_than": 0.01,
            "soft_regional_metrics": [
                "orthogonal_quality", "skewness", "non_wall_aspect_ratio", "neighbor_area_ratio",
                "wall_normal_alignment", "regional_resolution", "pilot_stability", "cell_cost",
            ],
            "single_metric_auto_selection": False,
        },
        "history": [],
        "created_utc": utc_now(),
    }
    if brief_path.exists():
        existing = read_json(brief_path)
        brief["created_utc"] = existing.get("created_utc", brief["created_utc"])
        brief["history"] = existing.get("history", [])
    atomic_json(brief_path, brief)
    return mesh_brief(run_root)


def mesh_brief(run_root: Path) -> dict[str, Any]:
    run_root = Path(run_root).resolve()
    brief_path = run_root / "artifacts" / "mesh_brief.json"
    if not brief_path.is_file():
        raise MeshSupervisionError("mesh brief is unavailable; confirm the run first")
    brief = read_json(brief_path)
    attempts_root = run_root / "artifacts" / MESH_ARTIFACTS
    attempts: list[dict[str, Any]] = []
    if attempts_root.exists():
        for result_path in sorted(attempts_root.glob("attempt_*/result.json")):
            attempts.append(read_json(result_path))
    maximum = int(brief["budget"]["max_candidates"])
    ai_attempts = [item for item in attempts if item.get("attempt_kind", "ai_candidate") == "ai_candidate"]
    fallback_attempt = next((item for item in attempts if item.get("attempt_kind") == "fixed_fallback"), None)
    eligible_ai = [item for item in ai_attempts if item.get("acceptance_eligible") is True]
    remaining = max(0, maximum - len(ai_attempts))
    checkpoint = read_checkpoint(run_root, "mesh_accepted")
    if fallback_attempt is not None:
        fallback_status = "ELIGIBLE" if fallback_attempt.get("acceptance_eligible") is True else "FAILED"
    elif remaining > 0:
        fallback_status = "LOCKED_AI_BUDGET_REMAINING"
    elif eligible_ai:
        fallback_status = "LOCKED_ELIGIBLE_AI_CANDIDATE_EXISTS"
    elif int(brief["generator"]["legacy_fixed_fallback"]["predicted_cells"]) > int(brief["budget"]["max_cells"]):
        fallback_status = "BLOCKED_CELL_BUDGET"
    else:
        fallback_status = "AVAILABLE"
    overall_status = "MESH_ACCEPTED" if checkpoint else (
        "MESH_FIXED_FALLBACK_FAILED" if fallback_status == "FAILED" else "WAITING_FOR_AI_MESH"
    )
    return {
        "status": overall_status,
        "brief": brief,
        "attempts": attempts,
        "used_candidates": len(ai_attempts),
        "remaining_candidates": remaining,
        "fallback": {
            "status": fallback_status,
            "attempt_id": fallback_attempt.get("attempt_id") if fallback_attempt else None,
            "trigger": "AI candidate budget exhausted and no eligible AI candidate exists",
            "predicted_cells": brief["generator"]["legacy_fixed_fallback"]["predicted_cells"],
            "parameters_sha256": brief["generator"]["legacy_fixed_fallback"]["parameters_sha256"],
        },
        "accepted_checkpoint": checkpoint,
    }


def _validate_proposal(raw: Mapping[str, Any], run_root: Path) -> dict[str, Any]:
    proposal = copy.deepcopy(dict(raw))
    required = {"schema_version", "parent_attempt", "source_mode", "rationale", "parameters"}
    if set(proposal) != required or proposal.get("schema_version") != 1:
        raise MeshSupervisionError("candidate must exactly match mesh_candidate.schema.json version 1")
    parent = proposal.get("parent_attempt")
    if parent is not None:
        if not isinstance(parent, str) or not parent.startswith("attempt_") or len(parent) != 11:
            raise MeshSupervisionError("parent_attempt must be null or attempt_NNN")
        if not (run_root / "artifacts" / MESH_ARTIFACTS / parent / "result.json").is_file():
            raise MeshSupervisionError("parent_attempt does not identify an evaluated candidate")
    if proposal.get("source_mode") not in {"builtin", "run_patch"}:
        raise MeshSupervisionError("source_mode must be builtin or run_patch")
    rationale = proposal.get("rationale")
    if not isinstance(rationale, Mapping) or set(rationale) != {"observed_regions", "hypothesis", "expected_effect"}:
        raise MeshSupervisionError("candidate rationale is incomplete or has unknown fields")
    regions = rationale.get("observed_regions")
    if not isinstance(regions, list) or not regions or any(not isinstance(item, str) or not item.strip() for item in regions):
        raise MeshSupervisionError("rationale.observed_regions must be a non-empty string list")
    for name in ("hypothesis", "expected_effect"):
        if not isinstance(rationale.get(name), str) or len(rationale[name].strip()) < 12:
            raise MeshSupervisionError(f"rationale.{name} must explain the engineering judgement")
    parameters = proposal.get("parameters")
    if not isinstance(parameters, Mapping) or set(parameters) != _PARAMETER_NAMES:
        missing = sorted(_PARAMETER_NAMES - set(parameters or {}))
        unknown = sorted(set(parameters or {}) - _PARAMETER_NAMES)
        raise MeshSupervisionError(f"candidate parameters must be complete; missing={missing}, unknown={unknown}")
    normalized: dict[str, Any] = {}
    for name, (minimum, maximum) in _INTEGER_PARAMETERS.items():
        value = parameters[name]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise MeshSupervisionError(f"parameters.{name} must be an integer in [{minimum}, {maximum}]")
        normalized[name] = value
    for name, (minimum, maximum) in _NUMBER_PARAMETERS.items():
        value = parameters[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise MeshSupervisionError(f"parameters.{name} must be finite")
        numeric = float(value)
        if not minimum <= numeric <= maximum:
            raise MeshSupervisionError(f"parameters.{name} must be in [{minimum}, {maximum}]")
        normalized[name] = numeric
    for name, choices in _CHOICE_PARAMETERS.items():
        if parameters[name] not in choices:
            raise MeshSupervisionError(f"parameters.{name} must be one of {sorted(choices)}")
        normalized[name] = parameters[name]
    proposal["parameters"] = normalized
    proposal["rationale"] = {
        "observed_regions": [str(item).strip() for item in regions],
        "hypothesis": rationale["hypothesis"].strip(),
        "expected_effect": rationale["expected_effect"].strip(),
    }
    return proposal


def _source_changes(run_root: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    artifacts = run_root / "artifacts"
    workspace = artifacts / WORKSPACE_NAME / "engine"
    baseline_record = read_json(artifacts / WORKSPACE_NAME / "source_baseline.json")
    baseline = dict(baseline_record.get("files", {}))
    current = _tree_manifest(workspace)
    changes: list[dict[str, Any]] = []
    for relative in sorted(set(baseline) | set(current)):
        old_hash = baseline.get(relative)
        new_hash = current.get(relative)
        if old_hash == new_hash:
            continue
        changes.append(
            {
                "path": relative,
                "change": "added" if old_hash is None else "deleted" if new_hash is None else "modified",
                "old_sha256": old_hash,
                "new_sha256": new_hash,
                "allowed": _is_allowed_mesh_source(relative),
            }
        )
    forbidden = [item["path"] for item in changes if not item["allowed"]]
    if forbidden:
        raise MeshSupervisionError(f"isolated workspace contains non-mesh source changes: {forbidden}")
    return changes, current


def _source_patch(run_root: Path, changes: list[dict[str, Any]]) -> str:
    workspace = run_root / "artifacts" / WORKSPACE_NAME / "engine"
    chunks: list[str] = []
    for item in changes:
        relative = item["path"]
        original = ENGINE_ROOT / Path(*PurePosixPath(relative).parts)
        current = workspace / Path(*PurePosixPath(relative).parts)
        old = original.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if original.is_file() else []
        new = current.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if current.is_file() else []
        chunks.extend(difflib.unified_diff(old, new, fromfile=f"a/{relative}", tofile=f"b/{relative}"))
    return "".join(chunks)


def _predicted_cells(parameters: Mapping[str, Any]) -> int:
    n = int(parameters["n_airfoil_side"])
    bridge = int(parameters["n_bridge"])
    radial = int(parameters["radial_layers"])
    wake = int(parameters["wake_columns"])
    return (2 * n - 2) * radial + (2 * radial + max(2, bridge) - 1) * wake


def _boundary_layer_design(context: Mapping[str, Any], parameters: Mapping[str, Any]) -> dict[str, Any]:
    first_layer = float(context["first_layer_m"])
    growth = float(parameters["growth_rate"])
    layers = int(parameters["bl_layers"])
    total_height = first_layer * layers if math.isclose(growth, 1.0) else first_layer * (growth**layers - 1.0) / (growth - 1.0)
    return {
        "target_y_plus": context.get("target_y_plus"),
        "first_layer_m": first_layer,
        "first_layer_over_input_chord": context.get("first_layer_over_input_chord"),
        "growth_rate": growth,
        "layers": layers,
        "total_height_m": total_height,
        "total_height_over_input_chord": total_height / float(context["input_chord_m"]),
        "first_layer_source": "derived from mesh brief flow physics; candidate cannot override it",
    }


def _extract_metrics(summary: Mapping[str, Any]) -> dict[str, float | int | None]:
    quality = summary.get("python_quality") if isinstance(summary.get("python_quality"), Mapping) else {}
    gate = summary.get("quality_gate") if isinstance(summary.get("quality_gate"), Mapping) else {}
    regions = quality.get("regions") if isinstance(quality.get("regions"), Mapping) else {}
    non_wall_values = [
        float(report["max_edge_aspect_ratio"])
        for name, report in regions.items()
        if name != "near_wall" and isinstance(report, Mapping) and isinstance(report.get("max_edge_aspect_ratio"), (int, float))
    ]
    return {
        "minimum_fluent_orthogonal_quality": _finite_or_none(
            summary.get("minimum_orthogonal_quality", gate.get("minimum_fluent_orthogonal_quality_actual"))
        ),
        "maximum_skewness": _finite_or_none(gate.get("maximum_skewness_actual", quality.get("max_skewness_estimated"))),
        "maximum_non_wall_aspect_ratio": max(non_wall_values) if non_wall_values else _finite_or_none(gate.get("non_wall_max_aspect_ratio_actual")),
        "maximum_neighbor_area_ratio": _finite_or_none(quality.get("max_neighbor_area_ratio")),
        "cell_count": int(summary.get("cell_count") or summary.get("fluent_quadrilateral_cells") or 0),
    }


def _load_partial_mesh_summary(cgrid_dir: Path) -> dict[str, Any]:
    """Preserve generator/Python evidence when the Fluent hard check cannot start."""
    primary_path = cgrid_dir / "primary_mesh_summary.json"
    if primary_path.is_file():
        return read_json(primary_path)
    generated_path = cgrid_dir / "cgrid_summary.json"
    quality_path = cgrid_dir / "quality_report.json"
    generated = read_json(generated_path) if generated_path.is_file() else {}
    quality = read_json(quality_path) if quality_path.is_file() else {}
    if quality:
        generated["python_quality"] = quality
        generated["quality_gate"] = quality.get("status", {})
        generated.setdefault("cell_count", quality.get("quadrilateral_cells", 0))
    else:
        generated.setdefault("cell_count", generated.get("quadrilateral_cells", 0))
    return generated


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _warning_list(summary: Mapping[str, Any]) -> list[str]:
    gate = summary.get("quality_gate") if isinstance(summary.get("quality_gate"), Mapping) else {}
    warnings = [str(item) for item in gate.get("warnings", [])]
    warnings.extend(str(item) for item in summary.get("warnings", []))
    return sorted(set(warnings))


def _run_fluent_pilot(config: dict[str, Any], case_path: Path, attempt_dir: Path, context: dict[str, Any]) -> dict[str, Any]:
    """Run exactly 100 first-order iterations, sampling residuals and forces every 10."""
    value = str(ENGINE_ROOT)
    if value not in sys.path:
        sys.path.insert(0, value)
    from airfoil_fluentmeshing.adjoint_runtime import FluentAdjointRunner  # type: ignore[import-not-found]
    from airfoil_fluentmeshing.cfd_qualification import collect_final_residuals  # type: ignore[import-not-found]
    from airfoil_fluentmeshing.fluent_runner import prepare_fluent_env  # type: ignore[import-not-found]

    os.environ.update(prepare_fluent_env())
    try:
        import ansys.fluent.core as pyfluent
    except Exception as exc:  # pragma: no cover - exercised by integration tests
        raise MeshSupervisionError(f"PyFluent is unavailable for the required pilot: {type(exc).__name__}: {exc}") from exc
    pilot_dir = attempt_dir / "pilot"
    pilot_dir.mkdir(parents=True, exist_ok=True)
    pilot_cfg = copy.deepcopy(config)
    pilot_cfg.setdefault("advanced_settings", {}).setdefault("solution_controls", {}).setdefault("flow_ramp", {})[
        "first_order_iterations"
    ] = 100
    runner = FluentAdjointRunner(pilot_cfg, pilot_dir, context, dry_run=False)
    transcript = pilot_dir / "pilot_transcript.txt"
    solver = None
    samples: list[dict[str, Any]] = []
    error: str | None = None
    try:
        solver = runner._launch_solver(pyfluent, pilot_dir)
        solver.transcript.start(file_name=str(transcript))
        runner._current_solver = solver
        runner.transcript_path = transcript
        solver.settings.file.read_case(file_name=str(case_path))
        runner._settings_step("pilot mesh check", lambda: solver.tui.mesh.check(), required=True)
        runner._apply_physics_settings(solver)
        runner._apply_flow_settings(solver)
        runner._verify_full_physics_fingerprint(solver)
        runner._apply_solution_settings(solver)
        runner._initialize_and_iterate_flow(solver, 0)
        runner._apply_spatial_discretization(
            solver.settings.solution.methods,
            pilot_cfg["advanced_settings"]["solution_methods"],
            order="first-order",
        )
        for completed in range(10, 101, 10):
            runner._settings_step(
                f"pilot first-order iterations {completed - 9}-{completed}",
                lambda: solver.settings.solution.run_calculation.iterate(iter_count=10),
                required=True,
            )
            residuals = collect_final_residuals(solver)
            coefficients = runner._compute_coefficients(solver, f"pilot_{completed:03d}")
            numeric = [*residuals.values(), coefficients.cd, coefficients.cl]
            if not residuals or any(value is None or not math.isfinite(float(value)) for value in numeric):
                raise RuntimeError(f"non-finite or missing residual/force evidence after {completed} iterations")
            samples.append(
                {
                    "iteration": completed,
                    "residuals": residuals,
                    "cd": coefficients.cd,
                    "cl": coefficients.cl,
                }
            )
    except Exception as exc:  # pragma: no cover - Fluent integration path
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if solver is not None:
            try:
                solver.transcript.stop()
            except Exception:
                pass
            try:
                # The pilot owns this session. Wait for shutdown and allow the
                # PyFluent fallback to terminate a process that does not exit.
                solver.exit(timeout=30, timeout_force=True, wait=True)
            except Exception:
                pass
    text = transcript.read_text(encoding="utf-8", errors="ignore").lower() if transcript.exists() else ""
    fatal_markers = [
        marker
        for marker in (
            "negative cell volume", "negative volume cell", "floating point exception", "divergence detected",
            "error at host", "connection to fluent closed",
        )
        if marker in text
    ]
    stalled = False
    diverged = False
    maxima: list[float] = []
    for sample in samples:
        maxima.append(max(abs(float(value)) for value in sample["residuals"].values()))
    if len(maxima) >= 5:
        diverged = maxima[-1] > max(1.0e6, maxima[0] * 1.0e6)
        recent = maxima[-5:]
        stalled = recent[-1] > 1.0e-2 and min(recent) >= recent[0] * 0.98
    passed = error is None and len(samples) == 10 and not fatal_markers and not stalled and not diverged
    return {
        "status": "PASS" if passed else "FAIL",
        "iterations_completed": samples[-1]["iteration"] if samples else 0,
        "record_interval": 10,
        "first_order": True,
        "samples": samples,
        "fatal_transcript_markers": fatal_markers,
        "stalled": stalled,
        "diverged": diverged,
        "error": error,
        "transcript": str(transcript),
        "commands": runner.commands,
    }


def _evaluate_prepared_candidate(
    run_root: Path,
    config: Mapping[str, Any],
    proposal: Mapping[str, Any],
    *,
    brief_status: Mapping[str, Any],
    maximum: int,
    changes: list[dict[str, Any]],
    source_manifest: Mapping[str, str],
    engine_root: Path,
    attempt_id: str,
    attempt_kind: str,
    mesh_runner: Callable[..., tuple[Path, dict[str, Any]]] | None = None,
    pilot_runner: Callable[[dict[str, Any], Path, Path, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate, check and record one already-authorized candidate."""
    predicted = _predicted_cells(proposal["parameters"])
    attempts_root = run_root / "artifacts" / MESH_ARTIFACTS
    with directory_lock(run_root):
        attempts_root.mkdir(parents=True, exist_ok=True)
        attempt_dir = attempts_root / attempt_id
        if attempt_dir.exists():
            raise MeshSupervisionError(f"mesh attempt already exists: {attempt_id}")
        attempt_dir.mkdir()
        atomic_json(
            attempt_dir / "proposal.json",
            {
                **proposal,
                "attempt_id": attempt_id,
                "predicted_cells": predicted,
                "submitted_utc": utc_now(),
                "generator_tree_sha256": _tree_digest(source_manifest),
                "source_changes": changes,
                "attempt_kind": attempt_kind,
            },
        )
    (attempt_dir / "source.patch").write_text(_source_patch(run_root, changes), encoding="utf-8")
    atomic_json(attempt_dir / "source_manifest.json", {"files": source_manifest, "tree_sha256": _tree_digest(source_manifest)})
    attempt_config = copy.deepcopy(dict(config))
    attempt_config.setdefault("mesh", {})["candidate_parameters"] = copy.deepcopy(proposal["parameters"])
    attempt_config["mesh"]["maximum_cells"] = maximum
    attempt_config["mesh"].setdefault("cgrid", {}).update(copy.deepcopy(proposal["parameters"]))
    attempt_config["mesh"]["cgrid"].pop("quality_retry_profiles", None)
    atomic_json(attempt_dir / "resolved_candidate_config.json", attempt_config)
    if mesh_runner is None:
        mesh_runner, computed_flow_context, resolve_effective_chord, _, _ = _engine_imports()
    else:
        _, computed_flow_context, resolve_effective_chord, _, _ = _engine_imports()
    resolve_effective_chord(attempt_config)
    context = computed_flow_context(attempt_config)
    hard_failures: list[str] = []
    summary: dict[str, Any] = {}
    case_path: Path | None = None
    try:
        case_path, summary = mesh_runner(
            attempt_config,
            attempt_dir,
            context,
            dry_run=False,
            cgrid_dir=attempt_dir / "cgrid",
            engine_root=engine_root,
        )
    except Exception as exc:
        hard_failures.append(f"mesh_generation_or_fluent_check:{type(exc).__name__}: {exc}")
        summary = _load_partial_mesh_summary(attempt_dir / "cgrid")
    actual_cells = int(summary.get("cell_count") or summary.get("fluent_quadrilateral_cells") or 0)
    if actual_cells > maximum:
        hard_failures.append("cell_budget_exceeded")
    pilot = {
        "status": "NOT_RUN",
        "iterations_completed": 0,
        "reason": "mesh hard failure",
    }
    if not hard_failures and case_path is not None:
        pilot = (pilot_runner or _run_fluent_pilot)(attempt_config, case_path, attempt_dir, context)
        if pilot.get("status") != "PASS":
            hard_failures.append("fluent_100_iteration_pilot_failed")
    warnings = _warning_list(summary)
    result = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "attempt_kind": attempt_kind,
        "parent_attempt": proposal["parent_attempt"],
        "status": "FAIL" if hard_failures else "ELIGIBLE",
        "acceptance_eligible": not hard_failures and pilot.get("status") == "PASS",
        "hard_failures": hard_failures,
        "warnings": warnings,
        "predicted_cells": predicted,
        "actual_cells": actual_cells,
        "parameters": proposal["parameters"],
        "boundary_layer_design": _boundary_layer_design(context, proposal["parameters"]),
        "rationale": proposal["rationale"],
        "source_mode": proposal["source_mode"],
        "generator_tree_sha256": _tree_digest(source_manifest),
        "generator_patch": str(attempt_dir / "source.patch"),
        "case_path": str(case_path.resolve()) if case_path and case_path.is_file() else None,
        "summary_path": str((attempt_dir / "cgrid" / "primary_mesh_summary.json").resolve()),
        "metrics": _extract_metrics(summary),
        "regional_hotspots": summary.get("python_quality", {}).get("regions", {}) if isinstance(summary.get("python_quality"), Mapping) else {},
        "visualizations": summary.get("visualizations", {}),
        "pilot": pilot,
        "completed_utc": utc_now(),
    }
    atomic_json(attempt_dir / "pilot_summary.json", pilot)
    atomic_json(attempt_dir / "result.json", result)
    brief_path = run_root / "artifacts" / "mesh_brief.json"
    brief = read_json(brief_path)
    history = [item for item in brief.get("history", []) if item.get("attempt_id") != attempt_id]
    history.append(
        {
            "attempt_id": attempt_id,
            "attempt_kind": attempt_kind,
            "parent_attempt": proposal["parent_attempt"],
            "status": result["status"],
            "targeted_regions": proposal["rationale"]["observed_regions"],
            "predicted_cells": predicted,
            "actual_cells": actual_cells,
            "result": str(attempt_dir / "result.json"),
        }
    )
    brief["history"] = history
    brief["updated_utc"] = utc_now()
    atomic_json(brief_path, brief)
    transition(
        run_root,
        "MESH",
        event="MESH_FIXED_FALLBACK_EVALUATED" if attempt_kind == "fixed_fallback" else "MESH_CANDIDATE_EVALUATED",
        details={
            "attempt_id": attempt_id,
            "attempt_kind": attempt_kind,
            "status": result["status"],
            "remaining": brief_status["remaining_candidates"] if attempt_kind == "fixed_fallback" else max(0, brief_status["remaining_candidates"] - 1),
        },
        updates={
            "execution_status": "MESH_FIXED_FALLBACK_FAILED"
            if attempt_kind == "fixed_fallback" and result["status"] == "FAIL"
            else "WAITING_FOR_AI_MESH"
        },
    )
    return result


def evaluate_mesh_candidate(
    run_root: Path,
    config: Mapping[str, Any],
    proposal_raw: Mapping[str, Any],
    *,
    mesh_runner: Callable[..., tuple[Path, dict[str, Any]]] | None = None,
    pilot_runner: Callable[[dict[str, Any], Path, Path, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Consume one AI candidate budget and record generation/Fluent evidence."""
    run_root = Path(run_root).resolve()
    state = read_json(run_root / "state.json")
    if state.get("state") != "MESH":
        raise MeshSupervisionError("mesh candidates may be evaluated only while the run is in MESH")
    if read_checkpoint(run_root, "mesh_accepted"):
        raise MeshSupervisionError("the run already has an accepted mesh checkpoint")
    brief_status = mesh_brief(run_root)
    if brief_status["remaining_candidates"] <= 0:
        raise MeshSupervisionError("mesh candidate budget is exhausted")
    proposal = _validate_proposal(proposal_raw, run_root)
    maximum = int(brief_status["brief"]["budget"]["max_cells"])
    predicted = _predicted_cells(proposal["parameters"])
    if predicted > maximum:
        raise MeshSupervisionError(f"candidate predicts {predicted} cells, above this run's {maximum}-cell limit")
    changes, source_manifest = _source_changes(run_root)
    if proposal["source_mode"] == "builtin" and changes:
        raise MeshSupervisionError("source_mode=builtin requires an unchanged isolated mesh workspace")
    if proposal["source_mode"] == "run_patch" and not changes:
        raise MeshSupervisionError("source_mode=run_patch requires at least one allowed mesh-source change")
    attempts_root = run_root / "artifacts" / MESH_ARTIFACTS
    attempt_number = len([path for path in attempts_root.glob("attempt_[0-9][0-9][0-9]") if path.is_dir()]) + 1
    return _evaluate_prepared_candidate(
        run_root,
        config,
        proposal,
        brief_status=brief_status,
        maximum=maximum,
        changes=changes,
        source_manifest=source_manifest,
        engine_root=run_root / "artifacts" / WORKSPACE_NAME / "engine",
        attempt_id=f"attempt_{attempt_number:03d}",
        attempt_kind="ai_candidate",
        mesh_runner=mesh_runner,
        pilot_runner=pilot_runner,
    )


def evaluate_fixed_mesh_fallback(
    run_root: Path,
    config: Mapping[str, Any],
    *,
    mesh_runner: Callable[..., tuple[Path, dict[str, Any]]] | None = None,
    pilot_runner: Callable[[dict[str, Any], Path, Path, dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate the unchanged legacy 48,458-cell C-grid after AI exhaustion."""
    run_root = Path(run_root).resolve()
    state = read_json(run_root / "state.json")
    if state.get("state") != "MESH":
        raise MeshSupervisionError("fixed fallback may be evaluated only while the run is in MESH")
    if read_checkpoint(run_root, "mesh_accepted"):
        raise MeshSupervisionError("the run already has an accepted mesh checkpoint")
    brief_status = mesh_brief(run_root)
    fallback_status = brief_status["fallback"]["status"]
    if fallback_status != "AVAILABLE":
        raise MeshSupervisionError(f"fixed fallback is not available: {fallback_status}")
    fallback = brief_status["brief"]["generator"]["legacy_fixed_fallback"]
    parameters = copy.deepcopy(fallback["parameters"])
    maximum = int(brief_status["brief"]["budget"]["max_cells"])
    predicted = _predicted_cells(parameters)
    if predicted > maximum:
        raise MeshSupervisionError(f"fixed fallback predicts {predicted} cells, above this run's {maximum}-cell limit")
    proposal = {
        "schema_version": 1,
        "parent_attempt": None,
        "source_mode": "legacy_fixed_fallback",
        "rationale": {
            "observed_regions": ["all_ai_candidates_failed"],
            "hypothesis": "The unchanged production C-grid provides a bounded recovery path after AI exhaustion.",
            "expected_effect": "Recover a Fluent-readable baseline without weakening any mesh or pilot gate.",
        },
        "parameters": parameters,
    }
    source_manifest = _tree_manifest(ENGINE_ROOT)
    return _evaluate_prepared_candidate(
        run_root,
        config,
        proposal,
        brief_status=brief_status,
        maximum=maximum,
        changes=[],
        source_manifest=source_manifest,
        engine_root=ENGINE_ROOT,
        attempt_id=FIXED_FALLBACK_ATTEMPT_ID,
        attempt_kind="fixed_fallback",
        mesh_runner=mesh_runner,
        pilot_runner=pilot_runner,
    )


def _dominates(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    objectives = {
        "minimum_fluent_orthogonal_quality": "max",
        "maximum_skewness": "min",
        "maximum_non_wall_aspect_ratio": "min",
        "maximum_neighbor_area_ratio": "min",
        "cell_count": "min",
    }
    strict = False
    comparable = False
    for key, direction in objectives.items():
        a = left.get(key)
        b = right.get(key)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        comparable = True
        if direction == "max":
            if float(a) < float(b):
                return False
            strict = strict or float(a) > float(b)
        else:
            if float(a) > float(b):
                return False
            strict = strict or float(a) < float(b)
    return comparable and strict


def accept_mesh_candidate(run_root: Path, attempt_id: str, decision_raw: Mapping[str, Any]) -> dict[str, Any]:
    run_root = Path(run_root).resolve()
    state = read_json(run_root / "state.json")
    if state.get("state") != "MESH":
        raise MeshSupervisionError("a mesh may be accepted only while the run is in MESH")
    if read_checkpoint(run_root, "mesh_accepted"):
        raise MeshSupervisionError("the run already has an accepted mesh checkpoint")
    if not isinstance(attempt_id, str) or not attempt_id.startswith("attempt_"):
        raise MeshSupervisionError("attempt_id must be attempt_NNN")
    attempt_dir = run_root / "artifacts" / MESH_ARTIFACTS / attempt_id
    result_path = attempt_dir / "result.json"
    if not result_path.is_file():
        raise MeshSupervisionError("attempt result does not exist")
    result = read_json(result_path)
    if result.get("acceptance_eligible") is not True:
        raise MeshSupervisionError("candidate has a hard failure or did not pass the 100-iteration Fluent pilot")
    decision = dict(decision_raw)
    required = {"rationale", "pareto_comparison", "accepted_with_warning"}
    if set(decision) != required:
        raise MeshSupervisionError("decision must contain only rationale, pareto_comparison, and accepted_with_warning")
    for name in ("rationale", "pareto_comparison"):
        if not isinstance(decision.get(name), str) or len(decision[name].strip()) < 20:
            raise MeshSupervisionError(f"decision.{name} must contain a substantive AI engineering explanation")
    if type(decision.get("accepted_with_warning")) is not bool:
        raise MeshSupervisionError("decision.accepted_with_warning must be boolean")
    has_warnings = bool(result.get("warnings"))
    if decision["accepted_with_warning"] != has_warnings:
        raise MeshSupervisionError("accepted_with_warning must exactly acknowledge the candidate's recorded warnings")
    eligible: list[dict[str, Any]] = []
    for path in sorted((run_root / "artifacts" / MESH_ARTIFACTS).glob("attempt_*/result.json")):
        candidate = read_json(path)
        if candidate.get("acceptance_eligible") is True:
            eligible.append(candidate)
    dominators = [
        candidate["attempt_id"]
        for candidate in eligible
        if candidate["attempt_id"] != attempt_id and _dominates(candidate.get("metrics", {}), result.get("metrics", {}))
    ]
    if dominators:
        raise MeshSupervisionError(f"candidate is Pareto-dominated by {dominators}; automatic single-metric selection is forbidden")
    case_path = Path(str(result.get("case_path", ""))).resolve()
    summary_path = Path(str(result.get("summary_path", ""))).resolve()
    if not case_path.is_file() or not summary_path.is_file():
        raise MeshSupervisionError("accepted candidate case or summary evidence is missing")
    decision = {
        "rationale": decision["rationale"].strip(),
        "pareto_comparison": decision["pareto_comparison"].strip(),
        "accepted_with_warning": decision["accepted_with_warning"],
        "accepted_utc": utc_now(),
        "attempt_id": attempt_id,
        "eligible_candidates_compared": [item["attempt_id"] for item in eligible],
        "dominators": dominators,
    }
    decision_path = attempt_dir / "acceptance_decision.json"
    atomic_json(decision_path, decision)
    checkpoint = {
        "status": "COMPLETE",
        "attempt_id": attempt_id,
        "attempt_kind": result.get("attempt_kind", "ai_candidate"),
        "case_path": str(case_path),
        "case_sha256": _sha256(case_path),
        "summary_path": str(summary_path),
        "summary_sha256": _sha256(summary_path),
        "decision_path": str(decision_path.resolve()),
        "decision_sha256": _sha256(decision_path),
        "generator_tree_sha256": result.get("generator_tree_sha256"),
        "generator_patch": result.get("generator_patch"),
        "accepted_with_warning": decision["accepted_with_warning"],
    }
    write_checkpoint(run_root, "mesh_accepted", checkpoint)
    transition(
        run_root,
        "MESH",
        event="AI_MESH_ACCEPTED",
        details={"attempt_id": attempt_id, "accepted_with_warning": decision["accepted_with_warning"]},
        updates={"execution_status": "MESH_ACCEPTED", "accepted_mesh_attempt": attempt_id},
    )
    return checkpoint
