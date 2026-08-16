from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from airfoil_workflow.execution import compile_engine_config, execute_request
from airfoil_workflow.fsm import atomic_json, create_state, read_checkpoint, read_json
from airfoil_workflow.jobspec import JobSpecError, validate_job_spec
from airfoil_workflow.mesh_supervision import (
    ENGINE_ROOT,
    MeshSupervisionError,
    accept_mesh_candidate,
    evaluate_fixed_mesh_fallback,
    evaluate_mesh_candidate,
    mesh_brief,
)
from airfoil_workflow.service import resume_run


def _dat(tmp_path: Path) -> Path:
    path = tmp_path / "foil.dat"
    path.write_text(
        "sample\n1.0 0.001\n0.75 0.05\n0.25 0.08\n0.0 0.0\n0.25 -0.08\n0.75 -0.05\n1.0 -0.001\n",
        encoding="ascii",
    )
    return path


def _spec(path: Path, *, max_candidates: int = 5, max_cells: int = 80_000) -> dict:
    return validate_job_spec(
        {
            "schema_version": "2.0",
            "geometry": {
                "airfoil_path": str(path),
                "airfoil_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "closure": "auto",
            },
            "flow": {"chord_m": 1.0, "velocity_m_s": 32.5, "angle_of_attack_deg": 2.0, "altitude_m": 0.0},
            "constraints": {
                "minimum_area_ratio": 0.95,
                "minimum_local_thickness_ratio": 0.95,
                "minimum_lift_ratio": 0.998,
            },
            "objective": {
                "kind": "minimize_cd_feasibility_first",
                "minimum_cd_reduction_ratio": 0.005,
                "max_solver_evaluations": 4,
            },
            "mesh": {
                "mode": "ai_supervised_cgrid",
                "preferred_cells": None,
                "max_cells": max_cells,
                "max_candidates": max_candidates,
            },
            "execution": {"kind": "local", "dry_run": False},
        }
    )


def _start(workspace: Path, *, max_candidates: int = 5) -> tuple[Path, dict]:
    spec = _spec(_dat(workspace), max_candidates=max_candidates)
    run_root = workspace / "runs" / "run-mesh-test"
    create_state(run_root, state="CONFIRMED", identity={"plan_id": "plan-test", "run_id": "run-mesh-test"})
    atomic_json(run_root / "request.json", spec)
    payload = execute_request(spec, run_root)
    assert payload["execution_status"] == "WAITING_FOR_AI_MESH"
    return run_root, spec


def _proposal(seed: dict, *, parent: str | None = None, n_delta: int = 0) -> dict:
    parameters = copy.deepcopy(seed)
    parameters["n_airfoil_side"] += n_delta
    return {
        "schema_version": 1,
        "parent_attempt": parent,
        "source_mode": "builtin",
        "rationale": {
            "observed_regions": ["leading_edge", "wake_inlet"],
            "hypothesis": "Curvature and wake cross-stream gradients control the local spacing need.",
            "expected_effect": "Improve local orthogonality and wake resolution without abrupt size growth.",
        },
        "parameters": parameters,
    }


def _mesh_runner(oq: float, skew: float, non_wall_ar: float, neighbor: float, cells: int):
    def run(config: dict, output: Path, context: dict, **kwargs: object):
        directory = Path(kwargs["cgrid_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        case = directory / "candidate.cas.h5"
        case.write_bytes(b"case")
        summary = {
            "status": "PASS",
            "cell_count": cells,
            "fluent_quadrilateral_cells": cells,
            "fluent_triangular_cells": 0,
            "minimum_orthogonal_quality": oq,
            "quality_gate": {"warnings": [], "maximum_skewness_actual": skew},
            "python_quality": {
                "max_neighbor_area_ratio": neighbor,
                "regions": {
                    "near_wall": {"max_edge_aspect_ratio": 100_000.0},
                    "wake": {"max_edge_aspect_ratio": non_wall_ar},
                },
            },
            "visualizations": {},
        }
        atomic_json(directory / "primary_mesh_summary.json", summary)
        return case, summary

    return run


def _pilot(*args: object) -> dict:
    return {
        "status": "PASS",
        "iterations_completed": 100,
        "record_interval": 10,
        "samples": [{"iteration": value} for value in range(10, 101, 10)],
    }


def test_new_and_legacy_mesh_contracts_and_limits(tmp_path: Path) -> None:
    modern = _spec(_dat(tmp_path), max_candidates=20, max_cells=120_000)
    assert modern["mesh"]["max_cells"] == 120_000
    assert modern["mesh"]["max_candidates"] == 20
    invalid = copy.deepcopy(modern)
    invalid["mesh"]["max_candidates"] = 21
    with pytest.raises(JobSpecError, match=r"\[1, 20\]"):
        validate_job_spec(invalid)
    legacy = copy.deepcopy(modern)
    legacy["mesh"] = {"target_cells": 48_458, "max_cells": 80_000}
    assert validate_job_spec(legacy)["mesh"]["preferred_cells"] == 48_458


def test_confirmation_pauses_and_resume_requires_accepted_checkpoint(tmp_path: Path) -> None:
    run_root, _spec_value = _start(tmp_path)
    state = read_json(run_root / "state.json")
    assert state["state"] == "MESH"
    assert state["execution_status"] == "WAITING_FOR_AI_MESH"
    status = mesh_brief(run_root)
    assert status["remaining_candidates"] == 5
    assert (run_root / "artifacts" / "mesh_candidate.schema.json").is_file()
    assert (run_root / "artifacts" / "mesh_agent_workspace" / "engine").is_dir()
    resumed = resume_run("run-mesh-test", tmp_path)
    assert resumed["idempotent"] is True
    assert resumed["result"]["execution_status"] == "WAITING_FOR_AI_MESH"
    assert read_checkpoint(run_root, "mesh_accepted") is None


def test_isolated_source_allowlist_rejects_non_mesh_edits(tmp_path: Path) -> None:
    run_root, spec = _start(tmp_path)
    status = mesh_brief(run_root)
    seed = status["brief"]["generator"]["baseline_seed_only_not_automatic"]
    workspace = run_root / "artifacts" / "mesh_agent_workspace" / "engine"
    solver_file = workspace / "airfoil_fluentmeshing" / "adjoint_runtime.py"
    solver_file.write_text(solver_file.read_text(encoding="utf-8") + "\n# forbidden run edit\n", encoding="utf-8")
    with pytest.raises(MeshSupervisionError, match="non-mesh source changes"):
        evaluate_mesh_candidate(
            run_root,
            compile_engine_config(spec, run_root),
            _proposal(seed),
            mesh_runner=_mesh_runner(0.3, 0.2, 20.0, 1.5, 40_000),
            pilot_runner=_pilot,
        )
    assert not (run_root / "artifacts" / "mesh_candidates").exists()


def test_allowed_patch_is_recorded_without_touching_canonical_source(tmp_path: Path) -> None:
    run_root, spec = _start(tmp_path)
    status = mesh_brief(run_root)
    seed = status["brief"]["generator"]["baseline_seed_only_not_automatic"]
    canonical = Path(__file__).resolve().parents[1] / "src" / "airfoil_workflow" / "engine" / "airfoil_fluentmeshing" / "cgrid" / "quality.py"
    canonical_hash = hashlib.sha256(canonical.read_bytes()).hexdigest()
    isolated = run_root / "artifacts" / "mesh_agent_workspace" / "engine" / "airfoil_fluentmeshing" / "cgrid" / "quality.py"
    isolated.write_text(isolated.read_text(encoding="utf-8") + "\n# run-only mesh experiment\n", encoding="utf-8")
    proposal = _proposal(seed)
    proposal["source_mode"] = "run_patch"
    result = evaluate_mesh_candidate(
        run_root,
        compile_engine_config(spec, run_root),
        proposal,
        mesh_runner=_mesh_runner(0.3, 0.2, 20.0, 1.5, 40_000),
        pilot_runner=_pilot,
    )
    assert result["acceptance_eligible"] is True
    assert Path(result["generator_patch"]).read_text(encoding="utf-8")
    assert result["boundary_layer_design"]["first_layer_m"] > 0.0
    assert result["boundary_layer_design"]["total_height_m"] > result["boundary_layer_design"]["first_layer_m"]
    assert hashlib.sha256(canonical.read_bytes()).hexdigest() == canonical_hash


def test_pareto_acceptance_is_explicit_and_not_single_metric(tmp_path: Path) -> None:
    run_root, spec = _start(tmp_path)
    seed = mesh_brief(run_root)["brief"]["generator"]["baseline_seed_only_not_automatic"]
    config = compile_engine_config(spec, run_root)
    first = evaluate_mesh_candidate(
        run_root,
        config,
        _proposal(seed),
        mesh_runner=_mesh_runner(0.32, 0.20, 20.0, 1.4, 40_000),
        pilot_runner=_pilot,
    )
    second = evaluate_mesh_candidate(
        run_root,
        config,
        _proposal(seed, parent="attempt_001", n_delta=1),
        mesh_runner=_mesh_runner(0.20, 0.30, 30.0, 1.8, 50_000),
        pilot_runner=_pilot,
    )
    assert first["acceptance_eligible"] and second["acceptance_eligible"]
    decision = {
        "rationale": "The regional anisotropy is aligned with the wall and wake while all hard gates pass.",
        "pareto_comparison": "All eligible candidates were compared across quality, smoothness, anisotropy, and cost.",
        "accepted_with_warning": False,
    }
    with pytest.raises(MeshSupervisionError, match="Pareto-dominated"):
        accept_mesh_candidate(run_root, "attempt_002", decision)
    checkpoint = accept_mesh_candidate(run_root, "attempt_001", decision)
    assert checkpoint["attempt_id"] == "attempt_001"
    assert read_json(run_root / "state.json")["execution_status"] == "MESH_ACCEPTED"


def test_failed_candidate_consumes_budget_and_cannot_be_accepted(tmp_path: Path) -> None:
    run_root, spec = _start(tmp_path, max_candidates=1)
    seed = mesh_brief(run_root)["brief"]["generator"]["baseline_seed_only_not_automatic"]

    def failed_mesh(*args: object, **kwargs: object):
        raise RuntimeError("folded cell")

    failed = evaluate_mesh_candidate(
        run_root,
        compile_engine_config(spec, run_root),
        _proposal(seed),
        mesh_runner=failed_mesh,
        pilot_runner=_pilot,
    )
    assert failed["status"] == "FAIL"
    assert mesh_brief(run_root)["remaining_candidates"] == 0
    with pytest.raises(MeshSupervisionError, match="hard failure"):
        accept_mesh_candidate(
            run_root,
            "attempt_001",
            {
                "rationale": "This should never be accepted because the structural gate failed.",
                "pareto_comparison": "No comparison can override a structural hard failure in this workflow.",
                "accepted_with_warning": False,
            },
        )


def test_fixed_fallback_is_locked_until_ai_budget_is_exhausted(tmp_path: Path) -> None:
    run_root, spec = _start(tmp_path, max_candidates=1)
    with pytest.raises(MeshSupervisionError, match="LOCKED_AI_BUDGET_REMAINING"):
        evaluate_fixed_mesh_fallback(
            run_root,
            compile_engine_config(spec, run_root),
            mesh_runner=_mesh_runner(0.3, 0.2, 20.0, 1.5, 48_458),
            pilot_runner=_pilot,
        )


def test_fixed_fallback_never_overrides_run_cell_budget(tmp_path: Path) -> None:
    spec = _spec(_dat(tmp_path), max_candidates=1, max_cells=40_000)
    run_root = tmp_path / "runs" / "run-mesh-test"
    create_state(run_root, state="CONFIRMED", identity={"plan_id": "plan-test", "run_id": "run-mesh-test"})
    atomic_json(run_root / "request.json", spec)
    execute_request(spec, run_root)
    seed = mesh_brief(run_root)["brief"]["generator"]["baseline_seed_only_not_automatic"]
    candidate = _proposal(seed)
    candidate["parameters"]["n_airfoil_side"] = 150
    candidate["parameters"]["radial_layers"] = 45
    candidate["parameters"]["wake_columns"] = 120

    def failed_mesh(*args: object, **kwargs: object):
        raise RuntimeError("AI candidate failed")

    config = compile_engine_config(spec, run_root)
    evaluate_mesh_candidate(run_root, config, candidate, mesh_runner=failed_mesh, pilot_runner=_pilot)
    assert mesh_brief(run_root)["fallback"]["status"] == "BLOCKED_CELL_BUDGET"
    with pytest.raises(MeshSupervisionError, match="BLOCKED_CELL_BUDGET"):
        evaluate_fixed_mesh_fallback(run_root, config, mesh_runner=failed_mesh, pilot_runner=_pilot)


def test_unchanged_fixed_48458_fallback_requires_explicit_acceptance(tmp_path: Path) -> None:
    run_root, spec = _start(tmp_path, max_candidates=1)
    brief = mesh_brief(run_root)
    seed = brief["brief"]["generator"]["baseline_seed_only_not_automatic"]
    fallback_parameters = brief["brief"]["generator"]["legacy_fixed_fallback"]["parameters"]
    assert brief["brief"]["generator"]["legacy_fixed_fallback"]["predicted_cells"] == 48_458

    def failed_mesh(*args: object, **kwargs: object):
        raise RuntimeError("all AI-specific candidates failed")

    evaluate_mesh_candidate(
        run_root,
        compile_engine_config(spec, run_root),
        _proposal(seed),
        mesh_runner=failed_mesh,
        pilot_runner=_pilot,
    )
    assert mesh_brief(run_root)["fallback"]["status"] == "AVAILABLE"
    seen: dict[str, object] = {}

    def fixed_runner(config: dict, output: Path, context: dict, **kwargs: object):
        seen["engine_root"] = Path(kwargs["engine_root"]).resolve()
        seen["parameters"] = copy.deepcopy(config["mesh"]["candidate_parameters"])
        return _mesh_runner(0.24, 0.69, 80.0, 2.0, 48_458)(config, output, context, **kwargs)

    result = evaluate_fixed_mesh_fallback(
        run_root,
        compile_engine_config(spec, run_root),
        mesh_runner=fixed_runner,
        pilot_runner=_pilot,
    )
    assert result["attempt_id"] == "attempt_fixed_fallback"
    assert result["attempt_kind"] == "fixed_fallback"
    assert result["acceptance_eligible"] is True
    assert result["parameters"] == fallback_parameters
    assert seen["parameters"] == fallback_parameters
    assert seen["engine_root"] == ENGINE_ROOT.resolve()
    assert mesh_brief(run_root)["used_candidates"] == 1
    assert mesh_brief(run_root)["fallback"]["status"] == "ELIGIBLE"
    assert read_checkpoint(run_root, "mesh_accepted") is None

    checkpoint = accept_mesh_candidate(
        run_root,
        "attempt_fixed_fallback",
        {
            "rationale": "The fixed fallback passed every hard gate and the full 100-iteration pilot without instability.",
            "pareto_comparison": "All AI candidates failed, so the eligible fixed fallback is the only feasible Pareto member.",
            "accepted_with_warning": False,
        },
    )
    assert checkpoint["attempt_id"] == "attempt_fixed_fallback"
    with pytest.raises(MeshSupervisionError, match="accepted mesh checkpoint"):
        evaluate_fixed_mesh_fallback(run_root, compile_engine_config(spec, run_root), pilot_runner=_pilot)


def test_fixed_fallback_hard_failure_stays_in_mesh(tmp_path: Path) -> None:
    run_root, spec = _start(tmp_path, max_candidates=1)
    seed = mesh_brief(run_root)["brief"]["generator"]["baseline_seed_only_not_automatic"]

    def failed_mesh(*args: object, **kwargs: object):
        raise RuntimeError("negative cell volume")

    config = compile_engine_config(spec, run_root)
    evaluate_mesh_candidate(run_root, config, _proposal(seed), mesh_runner=failed_mesh, pilot_runner=_pilot)
    result = evaluate_fixed_mesh_fallback(run_root, config, mesh_runner=failed_mesh, pilot_runner=_pilot)
    assert result["status"] == "FAIL"
    assert mesh_brief(run_root)["status"] == "MESH_FIXED_FALLBACK_FAILED"
    assert read_json(run_root / "state.json")["state"] == "MESH"
    assert read_checkpoint(run_root, "mesh_accepted") is None
    resumed = resume_run("run-mesh-test", tmp_path)
    assert resumed["result"]["execution_status"] == "MESH_FIXED_FALLBACK_FAILED"
    with pytest.raises(MeshSupervisionError, match="FAILED"):
        evaluate_fixed_mesh_fallback(run_root, config, mesh_runner=failed_mesh, pilot_runner=_pilot)


def test_fluent_start_failure_preserves_python_mesh_evidence(tmp_path: Path) -> None:
    run_root, spec = _start(tmp_path)
    seed = mesh_brief(run_root)["brief"]["generator"]["baseline_seed_only_not_automatic"]

    def fluent_start_failure(config: dict, output: Path, context: dict, **kwargs: object):
        directory = Path(kwargs["cgrid_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        quality = {
            "quadrilateral_cells": 41_234,
            "max_skewness_estimated": 0.71,
            "max_neighbor_area_ratio": 2.4,
            "regions": {
                "near_wall": {"max_edge_aspect_ratio": 900.0},
                "wake_inlet": {"max_edge_aspect_ratio": 44.0},
            },
            "status": {"warnings": ["orthogonal_quality_not_verified_by_fluent"]},
        }
        atomic_json(directory / "quality_report.json", quality)
        atomic_json(directory / "cgrid_summary.json", {**quality, "visualizations": {"wake": "wake.svg"}})
        raise RuntimeError("Fluent launch timeout")

    result = evaluate_mesh_candidate(
        run_root,
        compile_engine_config(spec, run_root),
        _proposal(seed),
        mesh_runner=fluent_start_failure,
        pilot_runner=_pilot,
    )
    assert result["status"] == "FAIL"
    assert result["actual_cells"] == 41_234
    assert result["metrics"]["maximum_skewness"] == pytest.approx(0.71)
    assert result["metrics"]["maximum_non_wall_aspect_ratio"] == pytest.approx(44.0)
    assert result["metrics"]["minimum_fluent_orthogonal_quality"] is None
