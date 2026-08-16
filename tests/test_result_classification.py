from __future__ import annotations

import json
from pathlib import Path

from airfoil_workflow.cli import EXIT_OK, EXIT_REJECTED, _exit_for_result
from airfoil_workflow.execution import _classify_engine_summary


def _write_summary(root: Path, value: dict) -> None:
    path = root / "artifacts" / "engine_runs" / "solver" / "optimization_summary.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_id": "expected", **value}), encoding="utf-8")


def test_completed_engine_is_not_implicitly_a_success(tmp_path: Path) -> None:
    _write_summary(tmp_path, {
        "technical_status": "COMPLETED",
        "design_status": "NO_STATISTICAL_IMPROVEMENT",
        "acceptance_status": "UNVERIFIED",
        "adjoint_result": {"accepted_steps": [], "performance_target": {"achieved": False}},
    })
    result = _classify_engine_summary(tmp_path, {}, expected_run_id="expected")
    assert result["outcome"] == "CONVERGED_WITHOUT_TARGET"
    assert result["evidence_status"] == "PROVISIONAL"
    assert result["production_qualified"] is False
    assert _exit_for_result("COMPLETED", result) == EXIT_REJECTED


def test_only_full_v2_qualification_becomes_production(tmp_path: Path) -> None:
    _write_summary(tmp_path, {
        "technical_status": "COMPLETED",
        "design_status": "TARGET_ACHIEVED",
        "acceptance_status": "ACCEPTED",
        "adjoint_result": {
            "accepted_steps": [{"step": 1}],
            "performance_target": {"achieved": True},
            "numerical_qualification": {"qualification": "QUALIFIED"},
            "cfd_qualification": {"qualification": "QUALIFIED"},
        },
    })
    result = _classify_engine_summary(tmp_path, {}, expected_run_id="expected")
    assert result["outcome"] == "TARGET_ACHIEVED"
    assert result["evidence_status"] == "PRODUCTION_QUALIFIED"
    assert result["production_qualified"] is True
    assert _exit_for_result("COMPLETED", result) == EXIT_OK


def test_dry_run_is_a_successful_plan_but_not_scientific_evidence(tmp_path: Path) -> None:
    result = {"execution_status": "DRY_RUN_COMPLETED", "evidence_status": "NOT_EVALUATED"}
    assert result["evidence_status"] == "NOT_EVALUATED"
    assert _exit_for_result("COMPLETED", result) == EXIT_OK


def test_waiting_for_ai_mesh_is_a_successful_checkpoint_command() -> None:
    result = {"execution_status": "WAITING_FOR_AI_MESH", "evidence_status": "NOT_EVALUATED"}

    assert _exit_for_result("MESH", result) == EXIT_OK
