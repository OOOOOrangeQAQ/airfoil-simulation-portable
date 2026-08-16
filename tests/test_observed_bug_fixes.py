from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from airfoil_workflow.engine.airfoil_fluentmeshing.adjoint_audit import (
    Coefficients,
    optimizer_runtime_objective_audit,
    resolve_shape_anchor_ranges,
)
from airfoil_workflow.engine.airfoil_fluentmeshing.adjoint_runtime import FluentAdjointRunner
from airfoil_workflow.engine.airfoil_fluentmeshing.cfd_qualification import (
    collect_boundary_mass_fluxes,
    collect_physics_readback,
)
from airfoil_workflow.engine.airfoil_fluentmeshing.trust import build_v2_outcome
from airfoil_workflow.execution import _sync_engine_logs
from airfoil_workflow.fsm import (
    atomic_json,
    create_state,
    heartbeat,
    read_json,
    transition,
    verify_event_log,
)
from airfoil_workflow.service import cancel_run, resume_run, status


def _optimization_state(root: Path, *, pid: int) -> None:
    create_state(root, state="CONFIRMED", identity={"plan_id": "plan-x", "run_id": root.name})
    for target in ("PREFLIGHT", "MESH", "BASELINE", "OPTIMIZATION"):
        transition(root, target)
    heartbeat(root, pid=pid, details={"stage": "engine"})


def test_heartbeat_updates_snapshot_without_event_log_storm(tmp_path: Path) -> None:
    root = tmp_path / "run"
    create_state(root, state="CONFIRMED", identity={"plan_id": "plan-x", "run_id": "run-x"})
    heartbeat(root, pid=10)
    heartbeat(root, pid=11)

    assert len(verify_event_log(root)) == 1
    state = read_json(root / "state.json")
    assert state["sequence"] == 1
    assert state["active_pid"] == 11


def test_dead_optimization_process_can_be_cancelled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / "runs" / "run-dead"
    _optimization_state(root, pid=999_999_999)

    result = cancel_run("run-dead", workspace)

    assert result["run"]["state"] == "CANCELLED"
    assert result["run"]["active_pid"] is None


def test_dead_optimization_process_can_resume_via_preflight(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / "runs" / "run-dead"
    _optimization_state(root, pid=999_999_999)
    atomic_json(root / "request.json", {"request": "checkpoint-owned-by-engine"})

    with patch("airfoil_workflow.service.execute_request", return_value={"execution_status": "COMPLETED"}) as execute:
        result = resume_run("run-dead", workspace)

    assert result["run"]["state"] == "PREFLIGHT"
    execute.assert_called_once()
    assert verify_event_log(root)[-1]["event"] == "RESUME_REQUESTED"


def test_resume_returns_post_execution_snapshot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / "runs" / "run-dead"
    _optimization_state(root, pid=999_999_999)
    atomic_json(root / "request.json", {"request": "checkpoint-owned-by-engine"})

    def finish(_request, run_root, *, resume):
        assert resume is True
        for target in ("MESH", "BASELINE", "OPTIMIZATION", "VALIDATION", "GRID_QUALIFICATION", "REPORTING", "COMPLETED"):
            transition(run_root, target)
        return {"execution_status": "COMPLETED"}

    with patch("airfoil_workflow.service.execute_request", side_effect=finish):
        resumed = resume_run("run-dead", workspace)

    assert resumed["run"]["state"] == "COMPLETED"
    assert resumed["result"]["execution_status"] == "COMPLETED"


def test_plan_status_exposes_linked_run_as_execution_source_of_truth(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    plan_root = workspace / "plans" / "plan-linked"
    run_root = workspace / "runs" / "run-linked"
    create_state(plan_root, state="CONFIRMED", identity={"plan_id": "plan-linked", "run_id": "run-linked"})
    atomic_json(
        plan_root / "plan.json",
        {"plan_id": "plan-linked", "run_id": "run-linked", "status": "CONFIRMED", "confirmed": True},
    )
    _optimization_state(run_root, pid=999_999_999)
    transition(run_root, "OPTIMIZATION", updates={"execution_status": "RUNNING"})

    result = status("plan-linked", workspace)

    assert result["run_id"] == "run-linked"
    assert result["execution_status"] == "RUNNING"
    assert result["linked_run"]["state"] == "OPTIMIZATION"


def test_shape_anchor_clip_ranges_are_nonzero_width() -> None:
    geometry = {
        "metrics": {"xmin": 0.0, "xmax": 1.0, "chord": 1.0},
        "points": [[1.0, 0.01], [0.0, 0.0], [1.0, -0.01]],
    }

    result = resolve_shape_anchor_ranges(
        geometry,
        {"enabled": True, "mode": "endpoints-only", "max_anchor_displacement_over_chord": 0.0},
    )

    assert result["leading_edge_range"][0] < result["leading_edge_range"][1]
    assert result["trailing_edge_range"][0] < result["trailing_edge_range"][1]


def test_optimizer_audit_rejects_stalled_infeasible_design_step(tmp_path: Path) -> None:
    transcript = tmp_path / "optimizer.txt"
    transcript.write_text(
        "\n".join(
            [
                "0 | 1 | 0 | cl | Y | Y | 213.6 | undef | - | Y",
                "0 | 2 | 0 | cd | Y | Y | 7.307 | -0.010 | - | -",
                "Linear solver exits due to divergence or stalling!",
                "1 | 1 | 0 | cl | Y | N | 213.4 | undef | -0.044 | N",
                "1 | 2 | 0 | cd | Y | Y | 7.295 | undef | - | -",
            ]
        ),
        encoding="utf-8",
    )

    result = optimizer_runtime_objective_audit(transcript, lift_lower_bound=213.5)

    assert result["verified"] is False
    assert result["binding_verified"] is True
    assert result["design_step_valid"] is False
    assert result["final_cl_feasible"] is False
    assert result["linear_solver_stalled"] is True


def test_solver_close_waits_and_owns_timeout_cleanup() -> None:
    solver = Mock()

    FluentAdjointRunner._close_solver(solver)

    solver.transcript.stop.assert_called_once_with()
    solver.exit.assert_called_once_with(timeout=30, timeout_force=False, wait=False)


def test_mass_flux_query_uses_quiet_signed_tui_path() -> None:
    scheme = SimpleNamespace(exec=Mock(side_effect=["velocity_inlet 1193.6932", "pressure_outlet -1193.6932"]))
    report = SimpleNamespace(surface_integrals=Mock())
    solver = SimpleNamespace(
        scheme=scheme,
        settings=SimpleNamespace(results=SimpleNamespace(report=report)),
    )

    values = collect_boundary_mass_fluxes(solver, ["velocity_inlet", "pressure_outlet"])

    assert values == {"velocity_inlet": 1193.6932, "pressure_outlet": -1193.6932}
    assert scheme.exec.call_count == 2
    assert not hasattr(report, "fluxes")


def test_physics_readback_uses_fluent_251_solver_time_path() -> None:
    settings = SimpleNamespace(
        setup=SimpleNamespace(
            general=SimpleNamespace(solver=SimpleNamespace(type="pressure-based", time="steady")),
            models=SimpleNamespace(
                energy=SimpleNamespace(enabled=True),
                viscous=SimpleNamespace(k_omega_model="sst"),
            ),
            materials=SimpleNamespace(
                fluid={"air": SimpleNamespace(density=SimpleNamespace(option="ideal-gas"), viscosity=SimpleNamespace(option="sutherland"))}
            ),
            boundary_conditions=SimpleNamespace(
                velocity_inlet={
                    "velocity_inlet": SimpleNamespace(
                        turbulence=SimpleNamespace(turbulent_intensity=SimpleNamespace(value=0.01))
                    )
                }
            ),
            reference_values=SimpleNamespace(velocity=32.5, density=1.225, area=1.0, length=1.0),
        )
    )

    result = collect_physics_readback(SimpleNamespace(settings=settings))

    assert result["time"] == "steady"


def test_solver_evaluation_budget_caps_profiles_and_reports_exhaustion(tmp_path: Path) -> None:
    runner = FluentAdjointRunner(
        {"optimization_run": {"max_accepted_design_steps": 5, "max_solver_evaluations": 1}},
        tmp_path,
        {},
    )
    observed: list[int] = []

    def fail_cycle(_pyfluent, _cycle, profiles, *_args, **_kwargs):
        observed.append(len(profiles))
        return {
            "attempts": [{"status": "FAIL", "candidate_gate": {"accepted": False}}],
            "accepted": None,
            "commands": [],
            "failures": [],
        }

    runner._run_cycle_attempts = fail_cycle  # type: ignore[method-assign]
    result = runner._run_design_cycles(
        object(),
        Coefficients(cd=0.01, cl=0.4, source="baseline"),
        tmp_path / "baseline.cas.h5",
        tmp_path / "baseline.dat.h5",
    )

    assert observed == [1]
    assert result["completion_reason"] == "maximum_solver_evaluations"


def test_infeasible_profile_exhaustion_is_not_reported_as_converged() -> None:
    outcome = build_v2_outcome(
        "INCOMPLETE_REPAIR_EXHAUSTED",
        {"attempts": [{"candidate_gate": {"reasons": ["optimizer_design_step_invalid"]}}], "completion_reason": "finite_standard_profiles_exhausted"},
    )

    assert outcome["design_status"] == "INFEASIBLE"
    assert outcome["termination_reason"] == "BUDGET_EXHAUSTED"


def test_unified_engine_log_mirrors_stdout_and_transcript(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    transcript = artifacts / "engine_runs" / "solver" / "attempt_transcript.txt"
    transcript.parent.mkdir(parents=True)
    (artifacts / "engine.stdout.log").write_text("python progress\n", encoding="utf-8")
    transcript.write_text("fluent progress\n", encoding="utf-8")
    offsets: dict[str, int] = {}
    output = StringIO()

    assert _sync_engine_logs(tmp_path, output, offsets) is True

    rendered = output.getvalue()
    assert "python progress" in rendered
    assert "fluent progress" in rendered
    assert "source=" in rendered
