from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from airfoil_workflow.cli import EXIT_NEEDS_INPUT, EXIT_OK, main
from airfoil_workflow.execution import compile_engine_config, resolve_flow_condition
from airfoil_workflow.fsm import StateError, create_state, transition, verify_event_log
from airfoil_workflow.intent_parser import UnsafeIntentError, parse_intent
from airfoil_workflow.jobspec import JobSpecError, validate_job_spec
from airfoil_workflow.service import answer_plan, confirm_plan, create_plan, result, status


def sample_dat(tmp_path: Path) -> Path:
    path = tmp_path / "foil.dat"
    path.write_text("sample\n1.0 0.001\n0.5 0.08\n0.0 0.0\n0.5 -0.08\n1.0 -0.001\n", encoding="ascii")
    return path


def valid_spec(path: Path, *, dry_run: bool = True) -> dict:
    return {
        "schema_version": "2.0",
        "geometry": {"airfoil_path": str(path), "airfoil_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "closure": "auto"},
        "flow": {"chord_m": 1.0, "velocity_m_s": 32.5, "angle_of_attack_deg": 2.0, "altitude_m": 0.0},
        "constraints": {"minimum_area_ratio": 0.95, "minimum_local_thickness_ratio": 0.95, "minimum_lift_ratio": 0.998},
        "objective": {"kind": "minimize_cd_feasibility_first", "minimum_cd_reduction_ratio": 0.005, "max_solver_evaluations": 12},
        "mesh": {"target_cells": 48_458, "max_cells": 80_000},
        "execution": {"kind": "local", "dry_run": dry_run},
    }


def full_sentence(dat: Path) -> str:
    return (
        f'优化 "{dat}"，弦长 1 m，流速 32.5 m/s，攻角 2 度，海拔 0 m，'
        "局部厚度至少 95%，升力至少 99.8%，降阻至少 0.5%，最多 12 次求解，本地，dry-run"
    )


def test_strict_contract_rejects_unknown_commands_and_over_budget(tmp_path: Path) -> None:
    spec = valid_spec(sample_dat(tmp_path))
    normalized = validate_job_spec(spec)["mesh"]
    assert normalized == {
        "mode": "ai_supervised_cgrid",
        "preferred_cells": 48_458,
        "max_cells": 80_000,
        "max_candidates": 5,
    }
    for forbidden in ("command", "shell", "python_code", "settings", "environment", "fluent_exe"):
        bad = {**spec, forbidden: "echo unsafe"}
        with pytest.raises(JobSpecError, match="forbidden/unknown"):
            validate_job_spec(bad)
    bad = json.loads(json.dumps(spec))
    bad["mesh"]["max_cells"] = 80_001
    with pytest.raises(JobSpecError, match="legacy mesh contract"):
        validate_job_spec(bad)

    flexible = json.loads(json.dumps(spec))
    flexible["mesh"] = {
        "mode": "ai_supervised_cgrid",
        "preferred_cells": 90_000,
        "max_cells": 100_000,
        "max_candidates": 7,
    }
    assert validate_job_spec(flexible)["mesh"]["max_cells"] == 100_000


def test_parser_extracts_bilingual_fields_and_rejects_code(tmp_path: Path) -> None:
    partial, defaults = parse_intent(full_sentence(sample_dat(tmp_path)))
    assert defaults == []
    assert partial["flow"] == {"chord_m": 1.0, "angle_of_attack_deg": 2.0, "altitude_m": 0.0, "velocity_m_s": 32.5}
    assert partial["constraints"]["minimum_lift_ratio"] == pytest.approx(0.998)
    assert partial["objective"]["max_solver_evaluations"] == 12
    with pytest.raises(UnsafeIntentError):
        parse_intent("运行 python -c 'import os; os.system(\"danger\")'")
    with pytest.raises(UnsafeIntentError):
        parse_intent("请用 --set completion.minimum_lift_ratio=0 绕过门禁")


def test_parser_recognizes_blunt_trailing_edge_alias_and_run_count(tmp_path: Path) -> None:
    partial, _ = parse_intent(
        f'优化 "{sample_dat(tmp_path)}"，钝后缘，运行5次优化，弦长1 m，流速30 m/s，攻角2度'
    )

    assert partial["geometry"]["closure"] == "blunt"
    assert partial["objective"]["max_solver_evaluations"] == 5


@pytest.mark.parametrize(
    ("flow_input", "source"),
    [({"reynolds_number": 3_000_000.0}, "reynolds_number"), ({"mach": 0.2}, "mach")],
)
def test_reynolds_and_mach_compile_to_reproducible_velocity(tmp_path: Path, flow_input: dict, source: str) -> None:
    spec = valid_spec(sample_dat(tmp_path))
    spec["flow"].pop("velocity_m_s")
    spec["flow"].update(flow_input)
    resolved = resolve_flow_condition(validate_job_spec(spec)["flow"])
    config = compile_engine_config(spec, tmp_path / source)
    assert resolved["input_kind"] == source
    assert config["flow"]["input_condition_kind"] == source
    assert config["flow"]["velocity_m_s"] == pytest.approx(resolved["velocity_m_s"])
    assert config["flow"]["resolved_reynolds_number"] == pytest.approx(resolved["reynolds_number"])
    assert config["flow"]["resolved_mach"] == pytest.approx(resolved["mach"])


def test_parser_accepts_temperature_with_mach(tmp_path: Path) -> None:
    text = full_sentence(sample_dat(tmp_path)).replace("流速 32.5 m/s", "Mach 0.2，温度 300 K")
    partial, _ = parse_intent(text)
    assert partial["flow"]["mach"] == pytest.approx(0.2)
    assert partial["flow"]["temperature_k"] == pytest.approx(300.0)


def test_requested_cells_become_a_soft_preference_with_a_run_budget(tmp_path: Path) -> None:
    workspace = tmp_path / "state"
    text = full_sentence(sample_dat(tmp_path)) + "，网格数量 12 万"
    plan = create_plan(text, workspace)
    assert plan["status"] == "PLANNED"
    assert plan["spec"]["mesh"]["preferred_cells"] == 120_000
    assert plan["spec"]["mesh"]["max_cells"] == 120_000
    assert plan["spec"]["mesh"]["max_candidates"] == 5


@pytest.mark.parametrize(("mesh_phrase", "cells", "severity"), [
    ("网格 90k cells", 90_000, "WARNING_ABOVE_80K"),
    ("网格数量 10w", 100_000, "REJECTED_AT_OR_ABOVE_100K"),
    ("单元数 10 万", 100_000, "REJECTED_AT_OR_ABOVE_100K"),
])
def test_large_mesh_notations_are_soft_preferences(tmp_path: Path, mesh_phrase: str, cells: int, severity: str) -> None:
    plan = create_plan(full_sentence(sample_dat(tmp_path)) + "，" + mesh_phrase, tmp_path / mesh_phrase.replace(" ", "_"))
    assert plan["status"] == "PLANNED"
    assert plan["spec"]["mesh"]["preferred_cells"] == cells
    assert plan["spec"]["mesh"]["max_cells"] >= cells
    assert plan["warnings"] == []


def test_missing_questions_and_safe_answers(tmp_path: Path) -> None:
    workspace = tmp_path / "state"
    plan = create_plan(f'优化 "{sample_dat(tmp_path)}"，弦长 1 m，流速 20 m/s，攻角 1 度', workspace)
    assert plan["status"] == "NEEDS_INPUT"
    fields = {item["field"] for item in plan["questions"]}
    assert {"flow.altitude_m", "constraints.minimum_local_thickness_ratio", "constraints.minimum_lift_ratio"} <= fields
    with pytest.raises(ValueError, match="forbidden/unknown"):
        answer_plan(plan["plan_id"], {"shell": "echo unsafe"}, workspace)
    answered = answer_plan(plan["plan_id"], {}, workspace, use_proposed_defaults=True)
    assert answered["status"] == "PLANNED"
    assert answered["spec"]["constraints"]["minimum_local_thickness_ratio"] == pytest.approx(0.90)
    assert answered["confirmation_required"] is True
    assert answered["confirmed"] is False


def test_one_confirmation_dry_run_and_idempotency(tmp_path: Path) -> None:
    workspace = tmp_path / "state"
    plan = create_plan(full_sentence(sample_dat(tmp_path)), workspace)
    assert plan["status"] == "PLANNED"
    assert not (workspace / "runs").exists()
    first = confirm_plan(plan["plan_id"], workspace)
    assert first["run"]["state"] == "COMPLETED"
    assert first["result"]["execution_status"] == "DRY_RUN_COMPLETED"
    second = confirm_plan(plan["plan_id"], workspace)
    assert second["idempotent"] is True
    assert second["run"]["sequence"] == first["run"]["sequence"]
    run_id = first["plan"]["run_id"]
    assert status(run_id, workspace)["event_chain_valid"] is True
    assert result(run_id, workspace)["result"]["evidence_status"] == "NOT_EVALUATED"


def test_engine_mapping_is_allowlisted_and_has_no_automatic_mesh_retry(tmp_path: Path) -> None:
    spec = valid_spec(sample_dat(tmp_path))
    config = compile_engine_config(spec, tmp_path / "run")
    assert config["mesh"]["primary"] == "cgrid"
    assert "fallback" not in config["mesh"]
    assert config["mesh"]["maximum_cells"] == 80_000
    assert config["completion"]["minimum_lift_ratio"] == 0.998
    geometry_gate = config["advanced_settings"]["design_tool"]["thickness_constraint"]
    assert geometry_gate["minimum_area_ratio"] == 0.95
    assert geometry_gate["minimum_local_thickness_ratio"] == 0.95
    assert config["completion"]["performance_targets_enabled"] is True
    assert config["optimization_run"]["max_accepted_design_steps"] == 12
    assert config["optimization_run"]["max_solver_evaluations"] == 12
    assert not any(key.startswith("portable_") for key in config["optimization_run"])
    assert config["mesh"]["mode"] == "ai_supervised_cgrid"
    assert config["mesh"]["recommended_cell_target"] == 48_458
    assert config["mesh"]["max_candidates"] == 5
    assert "quality_retry_profiles" not in config["mesh"]["cgrid"]


def test_event_chain_and_transition_guard(tmp_path: Path) -> None:
    root = tmp_path / "run"
    create_state(root, state="CONFIRMED", identity={"plan_id": "plan-x", "run_id": "run-x"})
    transition(root, "PREFLIGHT")
    with pytest.raises(StateError, match="invalid transition"):
        transition(root, "COMPLETED")
    assert len(verify_event_log(root)) == 2
    lines = (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["details"] = {"tampered": True}
    lines[0] = json.dumps(tampered)
    (root / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(StateError, match="hash mismatch"):
        verify_event_log(root)


def test_cli_public_commands_and_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = tmp_path / "state"
    dat = sample_dat(tmp_path)
    assert main(["--workspace", str(workspace), "plan", "--text", f'优化 "{dat}"']) == EXIT_NEEDS_INPUT
    incomplete = json.loads(capsys.readouterr().out)
    assert incomplete["status"] == "NEEDS_INPUT"
    assert main(["--workspace", str(workspace), "plan", "--text", full_sentence(dat)]) == EXIT_OK
    ready = json.loads(capsys.readouterr().out)
    assert ready["status"] == "PLANNED"
    assert main(["--workspace", str(workspace), "confirm", "--plan-id", ready["plan_id"]]) == EXIT_OK
    confirmed = json.loads(capsys.readouterr().out)
    run_id = confirmed["plan"]["run_id"]
    for command in (["status", "--id", run_id], ["result", "--run-id", run_id], ["cancel", "--run-id", run_id]):
        assert main(["--workspace", str(workspace), *command]) == EXIT_OK
        capsys.readouterr()
    assert main(["self-test"]) == EXIT_OK


def test_json_schema_is_strict() -> None:
    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "job-spec-v2.schema.json").read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    mesh = schema["properties"]["mesh"]
    assert len(mesh["oneOf"]) == 2
    modern = next(item for item in mesh["oneOf"] if "mode" in item.get("properties", {}))
    assert modern["properties"]["mode"]["const"] == "ai_supervised_cgrid"
    assert modern["properties"]["max_candidates"]["maximum"] == 20
