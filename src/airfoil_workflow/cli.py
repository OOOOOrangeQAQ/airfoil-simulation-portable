"""Console entry point for humans, weak AIs, and fixed workers."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .execution import execute_request
from .fsm import StateError, create_state, read_json
from .jobspec import JobSpecError, spec_digest, validate_job_spec
from .service import (
    answer_plan,
    cancel_run,
    confirm_plan,
    create_plan,
    mesh_accept,
    mesh_brief,
    mesh_evaluate,
    result,
    resume_run,
    status,
    workspace_path,
)
from .experience_cli import add_experience_subparser, run_experience_command


EXIT_OK = 0
EXIT_NEEDS_INPUT = 10
EXIT_WAITING_FOR_AI = 11
EXIT_REJECTED = 20
EXIT_USAGE = 2
EXIT_FAILED = 30


class CliError(RuntimeError):
    pass


def _exit_for_result(state: str, payload: Mapping[str, Any] | None) -> int:
    """Map orthogonal run/evidence state to a process exit code.

    A technically completed dry-run remains a successful planning operation.
    Any computed result that is provisional or rejected must be non-zero so a
    weak AI cannot mistake SCREENING/PROVISIONAL evidence for production truth.
    """
    if state == "FAILED":
        return EXIT_FAILED
    if state == "REJECTED":
        return EXIT_REJECTED
    if payload and payload.get("execution_status") == "WAITING_FOR_AI_MESH":
        # Reaching the explicit AI-mesh checkpoint is a successful command,
        # not a compute failure.  The machine-readable payload carries the
        # waiting state without causing shells to collapse it into exit code 1.
        return EXIT_OK
    if payload and payload.get("evidence_status") in {"PROVISIONAL", "REJECTED"}:
        return EXIT_REJECTED
    return EXIT_OK


def _json_object(value: str, name: str) -> dict[str, Any]:
    path = Path(value).expanduser()
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = json.loads(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"{name} must be a JSON object or readable JSON file") from exc
    if not isinstance(payload, dict):
        raise CliError(f"{name} must be a JSON object")
    return payload


def _emit(value: Mapping[str, Any], *, pretty: bool = True) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2 if pretty else None, sort_keys=True, allow_nan=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airfoil-workflow",
        description="Strict one-sentence airfoil simulation workflow. It never accepts shell/code or arbitrary settings.",
    )
    parser.add_argument("--workspace", help="State directory (default: .airfoil-workflow).", default=None)
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON.")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Parse one Chinese/English sentence into a reviewable plan.")
    plan.add_argument("--text", required=True)

    answer = sub.add_parser("answer", help="Answer only the fields requested by an incomplete plan.")
    answer.add_argument("--plan-id", required=True)
    answer.add_argument("--values", required=True, help="JSON object or JSON file; unknown fields are rejected.")
    answer.add_argument("--use-proposed-defaults", action="store_true")
    answer.add_argument("--save-defaults", action="store_true", help="Save only code-external non-path proposed defaults.")

    confirm = sub.add_parser("confirm", help="Give the plan's one explicit compute confirmation.")
    confirm.add_argument("--plan-id", required=True)

    show_status = sub.add_parser("status", help="Read plan/run state and verify its event hash chain.")
    status_ids = show_status.add_mutually_exclusive_group(required=True)
    status_ids.add_argument("--id", dest="identifier")
    status_ids.add_argument("--run-id", dest="run_id")
    status_ids.add_argument("--plan-id", dest="plan_id")

    resume = sub.add_parser("resume", help="Resume a failed/cancelled run from a safe checkpoint.")
    resume.add_argument("--run-id", required=True)

    brief = sub.add_parser("mesh-brief", help="Read the AI C-grid task and remaining candidate budget.")
    brief.add_argument("--run-id", required=True)

    evaluate = sub.add_parser("mesh-evaluate", help="Generate, check and pilot one AI C-grid candidate.")
    evaluate.add_argument("--run-id", required=True)
    evaluate.add_argument("--proposal", required=True, help="Candidate JSON object or JSON file.")

    accept = sub.add_parser("mesh-accept", help="Explicitly accept one eligible Pareto C-grid candidate.")
    accept.add_argument("--run-id", required=True)
    accept.add_argument("--attempt-id", required=True)
    accept.add_argument("--decision", required=True, help="AI acceptance JSON object or JSON file.")

    cancel = sub.add_parser("cancel", help="Request a safe cancellation; idempotent.")
    cancel.add_argument("--run-id", required=True)

    show_result = sub.add_parser("result", help="Return orthogonal execution/design/evidence statuses.")
    show_result.add_argument("--run-id", required=True)

    worker = sub.add_parser("worker-run", help=argparse.SUPPRESS)
    worker.add_argument("--request", required=True, help="Strict administrator-packaged JobSpec JSON.")
    worker.add_argument("--run-root", required=True)

    add_experience_subparser(sub)

    sub.add_parser("self-test", help="Run a dependency-free contract/state smoke test (no Fluent).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = workspace_path(args.workspace)
    try:
        if args.command == "plan":
            payload = create_plan(args.text, workspace)
            _emit(payload, pretty=not args.compact)
            return EXIT_NEEDS_INPUT if payload["status"] == "NEEDS_INPUT" else EXIT_OK
        if args.command == "answer":
            payload = answer_plan(
                args.plan_id,
                _json_object(args.values, "--values"),
                workspace,
                use_proposed_defaults=args.use_proposed_defaults,
                save_defaults=args.save_defaults,
            )
            _emit(payload, pretty=not args.compact)
            return EXIT_NEEDS_INPUT if payload["status"] == "NEEDS_INPUT" else EXIT_OK
        if args.command == "confirm":
            payload = confirm_plan(args.plan_id, workspace)
            _emit(payload, pretty=not args.compact)
            state = payload["run"]["state"]
            return _exit_for_result(state, payload.get("result"))
        if args.command == "status":
            _emit(status(args.identifier or args.run_id or args.plan_id, workspace), pretty=not args.compact)
            return EXIT_OK
        if args.command == "resume":
            payload = resume_run(args.run_id, workspace)
            _emit(payload, pretty=not args.compact)
            state = payload["run"]["state"]
            return _exit_for_result(state, payload.get("result"))
        if args.command == "mesh-brief":
            _emit(mesh_brief(args.run_id, workspace), pretty=not args.compact)
            return EXIT_OK
        if args.command == "mesh-evaluate":
            _emit(mesh_evaluate(args.run_id, _json_object(args.proposal, "--proposal"), workspace), pretty=not args.compact)
            return EXIT_OK
        if args.command == "mesh-accept":
            _emit(
                mesh_accept(args.run_id, args.attempt_id, _json_object(args.decision, "--decision"), workspace),
                pretty=not args.compact,
            )
            return EXIT_OK
        if args.command == "cancel":
            _emit(cancel_run(args.run_id, workspace), pretty=not args.compact)
            return EXIT_OK
        if args.command == "result":
            payload = result(args.run_id, workspace)
            _emit(payload, pretty=not args.compact)
            state = payload["state"]["state"]
            return _exit_for_result(state, payload.get("result"))
        if args.command == "worker-run":
            return _worker_run(args.request, args.run_root, pretty=not args.compact)
        if args.command == "experience":
            return run_experience_command(args)
        if args.command == "self-test":
            payload = self_test()
            _emit(payload, pretty=not args.compact)
            return EXIT_OK if payload["status"] == "PASS" else EXIT_FAILED
        raise CliError(f"unsupported command: {args.command}")
    except (CliError, JobSpecError, StateError, ValueError, FileNotFoundError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "ERROR", "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return EXIT_USAGE if isinstance(exc, (CliError, JobSpecError, ValueError, FileNotFoundError)) else EXIT_FAILED


def _worker_run(request_path: str, run_root_value: str, *, pretty: bool) -> int:
    request_file = Path(request_path).expanduser().resolve(strict=True)
    run_root = Path(run_root_value).expanduser().resolve()
    raw = _json_object(str(request_file), "--request")
    spec = validate_job_spec(raw)
    if spec["execution"]["kind"] != "local":
        raise CliError("worker-run request must be normalized to execution.kind=local by the signed SSH bundle")
    identity = {"plan_id": None, "run_id": "worker-" + spec_digest(spec)[:20]}
    if not (run_root / "state.json").exists():
        create_state(run_root, state="CONFIRMED", identity=identity, details={"worker": True})
        (run_root / "request.json").write_text(json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    payload = execute_request(spec, run_root, resume=True)
    _emit(payload, pretty=pretty)
    state = read_json(run_root / "state.json")["state"]
    return _exit_for_result(state, payload)


def self_test() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="airfoil-workflow-selftest-") as temporary:
        root = Path(temporary)
        dat = root / "sample.dat"
        dat.write_text("sample\n1 0\n0 0\n1 0\n", encoding="ascii")
        sha = __import__("hashlib").sha256(dat.read_bytes()).hexdigest()
        spec = {
            "schema_version": "2.0",
            "geometry": {"airfoil_path": str(dat), "airfoil_sha256": sha, "closure": "auto"},
            "flow": {"chord_m": 1.0, "velocity_m_s": 30.0, "angle_of_attack_deg": 2.0, "altitude_m": 0.0},
            "constraints": {"minimum_area_ratio": 0.95, "minimum_local_thickness_ratio": 0.90, "minimum_lift_ratio": 0.998},
            "objective": {"kind": "minimize_cd_feasibility_first", "minimum_cd_reduction_ratio": 0.005, "max_solver_evaluations": 4},
            "mesh": {"mode": "ai_supervised_cgrid", "preferred_cells": None, "max_cells": 80_000, "max_candidates": 5},
            "execution": {"kind": "local", "dry_run": True},
        }
        validate_job_spec(spec)
        checks.append({"name": "strict_jobspec", "status": "PASS"})
        bad = dict(spec)
        bad["command"] = "echo unsafe"
        try:
            validate_job_spec(bad)
        except JobSpecError:
            checks.append({"name": "unknown_command_rejected", "status": "PASS"})
        else:
            checks.append({"name": "unknown_command_rejected", "status": "FAIL"})
        state_root = root / "state"
        create_state(state_root, state="NEEDS_INPUT", identity={"plan_id": "plan-test", "run_id": None})
        from .fsm import transition, verify_event_log

        transition(state_root, "PLANNED")
        checks.append({"name": "append_only_state_chain", "status": "PASS" if len(verify_event_log(state_root)) == 2 else "FAIL"})
    return {"status": "PASS" if all(check["status"] == "PASS" for check in checks) else "FAIL", "checks": checks}


if __name__ == "__main__":
    raise SystemExit(main())
