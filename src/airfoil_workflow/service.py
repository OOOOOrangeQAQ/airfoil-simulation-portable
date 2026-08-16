"""Application service used by the CLI and fixed workers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .execution import compile_engine_config, execute_request
from .fsm import TERMINAL_STATES, StateError, append_event, atomic_json, create_state, directory_lock, read_checkpoint, read_json, transition, utc_now, verify_event_log
from .intent_parser import PROPOSED_DEFAULTS, parse_intent, questions_for, safe_merge_answers
from .jobspec import spec_digest, validate_job_spec
from .mesh_supervision import accept_mesh_candidate, evaluate_mesh_candidate, mesh_brief as read_mesh_brief


def workspace_path(value: str | Path | None = None) -> Path:
    configured = value or os.environ.get("AIRFOIL_WORKFLOW_STATE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[2] / ".airfoil-workflow").resolve()


def _personal_defaults(workspace: Path) -> dict[str, Any]:
    path = workspace / "preferences" / "personal_defaults.json"
    if not path.exists():
        return {}
    value = read_json(path)
    allowed = set(PROPOSED_DEFAULTS)
    return {key: item for key, item in value.items() if key in allowed}


def save_personal_defaults(workspace: Path, values: Mapping[str, Any]) -> None:
    allowed = set(PROPOSED_DEFAULTS)
    safe = {key: value for key, value in values.items() if key in allowed}
    if set(values) - allowed:
        raise ValueError("only non-path proposed defaults may be saved")
    current = _personal_defaults(workspace)
    current.update(safe)
    atomic_json(workspace / "preferences" / "personal_defaults.json", current)


def create_plan(text: str, workspace: Path) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    partial, used_defaults = parse_intent(text, _personal_defaults(workspace))
    plan_id = "plan-" + hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:20]
    root = workspace / "plans" / plan_id
    if (root / "plan.json").exists():
        existing = read_json(root / "plan.json")
        if existing.get("source_text") != text:
            raise StateError("deterministic plan ID collision")
        return {**existing, "state": read_json(root / "state.json"), "idempotent": True}
    questions = questions_for(partial)
    status = "NEEDS_INPUT" if questions else "PLANNED"
    state = create_state(root, state=status, identity={"plan_id": plan_id, "run_id": None})
    plan = {
        "plan_id": plan_id,
        "status": status,
        "source_text": text,
        "partial_spec": partial,
        "questions": questions,
        "used_personal_defaults": used_defaults,
        "confirmation_required": status == "PLANNED",
        "confirmed": False,
        "warnings": [],
    }
    if status == "PLANNED":
        plan["spec"] = validate_job_spec(partial)
        plan["spec_sha256"] = spec_digest(plan["spec"])
        plan["summary"] = render_plan_summary(plan["spec"], used_defaults)
    atomic_json(root / "plan.json", plan)
    return {**plan, "state": state}


def answer_plan(
    plan_id: str,
    answers: Mapping[str, Any],
    workspace: Path,
    *,
    use_proposed_defaults: bool = False,
    save_defaults: bool = False,
) -> dict[str, Any]:
    root = _plan_root(workspace, plan_id)
    plan = read_json(root / "plan.json")
    if plan["status"] not in {"NEEDS_INPUT", "PLANNED"} or plan.get("confirmed"):
        raise StateError("only an unconfirmed plan may be answered")
    partial = safe_merge_answers(plan["partial_spec"], answers, use_proposed=use_proposed_defaults)
    questions = questions_for(partial)
    status = "NEEDS_INPUT" if questions else "PLANNED"
    plan.update({"partial_spec": partial, "questions": questions, "status": status, "confirmation_required": status == "PLANNED"})
    if status == "PLANNED":
        plan["spec"] = validate_job_spec(partial)
        plan["spec_sha256"] = spec_digest(plan["spec"])
        plan["summary"] = render_plan_summary(plan["spec"], plan.get("used_personal_defaults", []))
    else:
        for key in ("spec", "spec_sha256", "summary"):
            plan.pop(key, None)
    transition(root, status, event="ANSWERS_APPLIED", details={"answered_fields": sorted(_flatten_names(answers))})
    atomic_json(root / "plan.json", plan)
    if save_defaults:
        flat = _flatten_values(answers)
        save_personal_defaults(workspace, {key: value for key, value in flat.items() if key in PROPOSED_DEFAULTS})
    return plan


def confirm_plan(plan_id: str, workspace: Path) -> dict[str, Any]:
    plan_root = _plan_root(workspace, plan_id)
    plan = read_json(plan_root / "plan.json")
    if plan.get("confirmed"):
        run_id = plan["run_id"]
        return {"plan": plan, "run": read_json(workspace / "runs" / run_id / "state.json"), "idempotent": True}
    if plan.get("status") != "PLANNED":
        raise StateError("plan is not complete; answer the listed questions first")
    spec = validate_job_spec(plan["spec"])
    if spec_digest(spec) != plan.get("spec_sha256"):
        raise StateError("plan spec hash changed after planning")
    run_id = "run-" + spec_digest(spec)[:20]
    run_root = workspace / "runs" / run_id
    create_state(run_root, state="CONFIRMED", identity={"plan_id": plan_id, "run_id": run_id})
    atomic_json(run_root / "request.json", spec)
    transition(
        plan_root,
        "CONFIRMED",
        event="USER_CONFIRMED",
        details={"run_id": run_id},
        updates={"run_id": run_id, "execution_status": "RUNNING"},
    )
    plan.update({"status": "CONFIRMED", "confirmed": True, "run_id": run_id})
    atomic_json(plan_root / "plan.json", plan)
    result = execute_request(spec, run_root)
    transition(
        plan_root,
        "CONFIRMED",
        event="LINKED_RUN_FINISHED",
        details={"run_id": run_id, "run_state": read_json(run_root / "state.json")["state"]},
        updates={"execution_status": result.get("execution_status", "FAILED")},
    )
    return {"plan": plan, "run": read_json(run_root / "state.json"), "result": result, "idempotent": False}


def resume_run(run_id: str, workspace: Path) -> dict[str, Any]:
    run_root = _run_root(workspace, run_id)
    state = read_json(run_root / "state.json")
    if state["state"] == "COMPLETED":
        return {"run": state, "result": read_json(run_root / "result.json"), "idempotent": True}
    if state["state"] == "REJECTED":
        raise StateError("a scientifically rejected run cannot be resumed; make a new plan")
    if state["state"] == "MESH" and read_checkpoint(run_root, "mesh_accepted") is None:
        waiting = {
            "execution_status": "WAITING_FOR_AI_MESH",
            "design_status": "NOT_EVALUATED",
            "evidence_status": "NOT_EVALUATED",
            "outcome": "WAITING_FOR_AI_MESH",
            "message": "No accepted mesh checkpoint exists; evaluate and accept a candidate before resume.",
            "remaining_candidates": read_mesh_brief(run_root)["remaining_candidates"],
        }
        return {"run": state, "result": waiting, "idempotent": True}
    request = read_json(run_root / "request.json")
    active_pid = state.get("active_pid")
    active_process = isinstance(active_pid, int) and _pid_is_alive(active_pid)
    if not active_process:
        # Preflight is deterministic and cheap; repeat it so resumed jobs validate
        # the current input hash and immutable policy before re-entering the engine.
        transition(
            run_root,
            "PREFLIGHT",
            event="RESUME_REQUESTED",
            details={"previous_state": state["state"], "previous_pid": active_pid, "process_alive": False},
            updates={"active_pid": None, "execution_status": "RUNNING"},
        )
        (run_root / "cancel.requested").unlink(missing_ok=True)
    execution_result = execute_request(request, run_root, resume=True)
    # execute_request may advance through several states before returning.  Read
    # the snapshot afterwards so the outer run object and nested result always
    # describe the same point in time.
    return {"run": read_json(run_root / "state.json"), "result": execution_result}


def mesh_brief(run_id: str, workspace: Path) -> dict[str, Any]:
    return read_mesh_brief(_run_root(workspace, run_id))


def mesh_evaluate(run_id: str, proposal: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    run_root = _run_root(workspace, run_id)
    request = read_json(run_root / "request.json")
    config = compile_engine_config(request, run_root)
    return evaluate_mesh_candidate(run_root, config, proposal)


def mesh_accept(run_id: str, attempt_id: str, decision: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    return accept_mesh_candidate(_run_root(workspace, run_id), attempt_id, decision)


def cancel_run(run_id: str, workspace: Path) -> dict[str, Any]:
    run_root = _run_root(workspace, run_id)
    state = read_json(run_root / "state.json")
    if state["state"] in TERMINAL_STATES:
        return {"run": state, "idempotent": True}
    active_pid = state.get("active_pid")
    active_process = isinstance(active_pid, int) and _pid_is_alive(active_pid)
    request = run_root / "cancel.requested"
    if not active_process:
        request.unlink(missing_ok=True)
        state = transition(
            run_root,
            "CANCELLED",
            event="CANCELLED_DEAD_PROCESS",
            details={"previous_pid": active_pid},
            updates={"execution_status": "CANCELLED", "active_pid": None},
        )
        return {"run": state, "idempotent": False}
    if request.exists():
        return {"run": state, "idempotent": True}
    with directory_lock(run_root):
        descriptor = __import__("os").open(request, __import__("os").O_CREAT | __import__("os").O_EXCL | __import__("os").O_WRONLY)
        with __import__("os").fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"cancel requested {utc_now()}\n")
        append_event(run_root, {"event": "CANCEL_REQUESTED", "from": state["state"], "to": state["state"], "details": {}})
    return {"run": state, "idempotent": False}


def status(identifier: str, workspace: Path) -> dict[str, Any]:
    if identifier.startswith("plan-"):
        root = _plan_root(workspace, identifier)
        plan = read_json(root / "plan.json")
        plan_state = read_json(root / "state.json")
        linked_run = None
        run_id = plan.get("run_id") or plan_state.get("run_id")
        if isinstance(run_id, str):
            run_state_path = workspace / "runs" / run_id / "state.json"
            if run_state_path.is_file():
                linked_run = read_json(run_state_path)
        return {
            "plan": plan,
            "state": plan_state,
            "plan_state": plan_state,
            "linked_run": linked_run,
            "run_id": run_id,
            "execution_status": (linked_run or plan_state).get("execution_status"),
            "event_chain_valid": bool(verify_event_log(root)),
        }
    root = _run_root(workspace, identifier)
    return {"state": read_json(root / "state.json"), "event_chain_valid": bool(verify_event_log(root))}


def result(run_id: str, workspace: Path) -> dict[str, Any]:
    root = _run_root(workspace, run_id)
    state = read_json(root / "state.json")
    path = root / "result.json"
    return {"state": state, "result": read_json(path) if path.exists() else None}


def render_plan_summary(spec: Mapping[str, Any], defaults: list[str]) -> dict[str, Any]:
    return {
        "input": {"path": spec["geometry"]["airfoil_path"], "sha256": spec["geometry"]["airfoil_sha256"]},
        "flow": spec["flow"],
        "constraints": spec["constraints"],
        "objective": spec["objective"],
        "mesh": {"policy_id": "ai-supervised-cgrid-v1", **spec["mesh"]},
        "execution": spec["execution"],
        "assumptions_from_personal_defaults": defaults,
        "expected_outputs": ["ranked feasible scheme set", "resolved config", "mesh/solver evidence", "reproduction manifest"],
        "warning": "Confirmation prepares the MESH brief. Baseline computation starts only after an AI-qualified C-grid is explicitly accepted.",
    }


def _plan_root(workspace: Path, plan_id: str) -> Path:
    if not plan_id.startswith("plan-") or "/" in plan_id or "\\" in plan_id:
        raise ValueError("unsafe plan ID")
    root = workspace / "plans" / plan_id
    if not (root / "plan.json").is_file():
        raise FileNotFoundError(f"unknown plan: {plan_id}")
    return root


def _run_root(workspace: Path, run_id: str) -> Path:
    if not run_id.startswith("run-") or "/" in run_id or "\\" in run_id:
        raise ValueError("unsafe run ID")
    root = workspace / "runs" / run_id
    if not (root / "state.json").is_file():
        raise FileNotFoundError(f"unknown run: {run_id}")
    return root


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True
    import ctypes

    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _flatten_names(value: Mapping[str, Any], prefix: str = "") -> list[str]:
    return list(_flatten_values(value, prefix))


def _flatten_values(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping):
            result.update(_flatten_values(item, dotted))
        else:
            result[dotted] = item
    return result
