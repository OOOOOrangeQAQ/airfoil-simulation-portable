from __future__ import annotations

import json
import copy
import hashlib
import io
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any

from airfoil_fluentmeshing.boundary_layer import first_layer_from_velocity
from airfoil_fluentmeshing.fluent_runner import close_fluent_session, fluent_path_arg, fluent_product_version_arg, prepare_fluent_env
from airfoil_fluentmeshing.geometry import read_dat, split_normalized, write_dat_sections
from airfoil_fluentmeshing.shape_guard import build_geometry_snapshot, compare_geometry, resolve_shape_guard
from airfoil_fluentmeshing.optimization_profiles import (
    build_optimization_profile,
    validate_aerodynamic_controls,
    validate_control_points,
)

from airfoil_fluentmeshing.adjoint_support import *
from airfoil_fluentmeshing.adjoint_audit import *
from airfoil_fluentmeshing.adjoint_mesh_pipeline import *
from airfoil_fluentmeshing.trust import (
    aa_noise_floor,
    coefficient_representative,
    geometry_change_gate,
    improvement_confidence,
)
from airfoil_fluentmeshing.cfd_qualification import (
    build_cfd_qualification,
    collect_boundary_mass_fluxes,
    collect_final_residuals,
    collect_physics_readback,
    collect_wall_y_plus,
    collect_wall_y_plus_distribution,
    physics_readback_qualification,
    residual_qualification,
)

# Stateful Fluent sessions, exports, validation, and design cycles.

INTERPOLATION_REQUIRED_FIELDS = (
    "pressure",
    "x-velocity",
    "y-velocity",
    "temperature",
    "k",
    "omega",
)
DEFAULT_PRESSURE_VELOCITY_COUPLING = "coupled"


def select_solution_interpolation_fields(
    allowed_fields: list[str] | tuple[str, ...],
    requested_fields: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Resolve a complete, deterministic field list for mesh-to-mesh transfer."""
    allowed = [str(value).strip() for value in allowed_fields if str(value).strip()]
    requested = list(requested_fields or INTERPOLATION_REQUIRED_FIELDS)
    aliases = {value.lower().replace("_", "-"): value for value in allowed}
    resolved: list[str] = []
    missing: list[str] = []
    for value in requested:
        normalized = str(value).strip().lower().replace("_", "-")
        actual = aliases.get(normalized)
        if actual is None:
            missing.append(str(value))
        elif actual not in resolved:
            resolved.append(actual)
    if missing:
        raise RuntimeError(
            "Fluent interpolation source is missing required solution fields: "
            + ", ".join(missing)
            + f"; allowed={allowed}"
        )
    return resolved


def normalize_pressure_velocity_coupling(value: Any) -> str:
    """Return the exact Fluent spelling for supported pressure-velocity schemes."""
    normalized = str(value).strip().lower()
    mapping = {
        "simple": "SIMPLE",
        "simplec": "SIMPLEC",
        "piso": "PISO",
        "coupled": "Coupled",
    }
    if normalized not in mapping:
        raise ValueError(f"Unsupported pressure-velocity coupling: {value!r}")
    return mapping[normalized]


def normalize_under_relaxation_updates(updates: dict[str, Any], available: dict[str, Any]) -> dict[str, float]:
    """Map user-facing equation names to Fluent's active under-relaxation keys."""
    aliases = {
        "body_force": "body-force",
        "body-force": "body-force",
        "momentum": "mom",
        "mom": "mom",
        "turbulent_kinetic_energy": "k",
        "specific_dissipation_rate": "omega",
        "turbulent_viscosity": "turb-viscosity",
        "turb-viscosity": "turb-viscosity",
    }
    resolved: dict[str, float] = {}
    for name, raw_value in updates.items():
        key = aliases.get(str(name).strip().lower(), str(name).strip().lower().replace("_", "-"))
        if key not in available:
            raise ValueError(f"Under-relaxation field {name!r} is not active; available={sorted(available)}")
        value = float(raw_value)
        if not 0.0 < value <= 1.0:
            raise ValueError(f"Under-relaxation factor for {name!r} must be in (0, 1]")
        resolved[key] = value
    return resolved

class FluentAdjointRunner:
    def __init__(self, cfg: dict[str, Any], output_dir: Path, context: dict[str, Any], *, dry_run: bool = False):
        self.cfg = cfg
        self.output_dir = output_dir
        self.context = context
        self.dry_run = dry_run
        self.commands: list[dict[str, Any]] = []
        self.failures: list[dict[str, str]] = []
        self.exports: list[dict[str, Any]] = []
        self.transcript_path: Path | None = None
        self._current_solver: Any | None = None
        self.shape_guard = resolve_shape_guard(deep_get(cfg, "shape_guard", {}))
        self.baseline_geometry: dict[str, Any] | None = None
        self.baseline_force_stability: dict[str, Any] | None = None
        self.cfd_qualification: dict[str, Any] = {
            "qualification": "UNQUALIFIED",
            "production_qualified": False,
            "errors": ["cfd_qualification_not_run"],
        }
        self.numerical_qualification: dict[str, Any] = aa_noise_floor(
            deep_get(cfg, "optimization_run.numerical_uncertainty.aa_cd_values", []) or [],
            engineering_floor=float(
                deep_get(cfg, "optimization_run.numerical_uncertainty.engineering_floor", 5.0e-4)
            ),
            required_repeats=int(
                deep_get(cfg, "optimization_run.numerical_uncertainty.required_repeats", 5)
            ),
        )
        if deep_get(cfg, "optimization_run.numerical_uncertainty.aa_cd_values", []):
            # Configuration values are useful for dry-run calculations but are
            # never accepted as proof of independent Fluent reloads.
            self.numerical_qualification["qualification"] = "UNQUALIFIED"
            self.numerical_qualification["source"] = "configured_values_not_independent_reload_evidence"
            self.numerical_qualification.setdefault("errors", []).append("aa_values_not_collected_by_runtime")
        run_cfg = deep_get(cfg, "optimization_run", {})
        self.runtime_resolution: dict[str, Any] = {
            "versions": {"fluent": None, "pyfluent": None},
            "objective_mapping": None,
            "morpher": None,
            "shape_anchors": None,
            "thickness_constraint": None,
            "minimum_orthogonal_quality": None,
            "control_points": None,
            "acceptance_policy": {
                "accept_recovered_attempts": bool(run_cfg.get("accept_recovered_attempts", False)),
                "strict_clean_morphing": bool(run_cfg.get("strict_clean_morphing", False)),
                "transient_negative_volume_history_allowed": bool(run_cfg.get("accept_recovered_attempts", False)),
                "zero_step_allowed": False,
                "fresh_fluent_revalidation_required": True,
                "minimum_lift_ratio": float(deep_get(cfg, "completion.minimum_lift_ratio", 0.998)),
                "stepwise_lift_to_drag_improvement_required": bool(
                    deep_get(cfg, "completion.require_stepwise_lift_to_drag_improvement", False)
                ),
                "performance_targets_enabled": bool(deep_get(cfg, "completion.performance_targets_enabled", False)),
                "minimum_cumulative_cd_reduction": float(
                    deep_get(cfg, "completion.minimum_cumulative_cd_reduction", 0.005)
                ),
                "minimum_cumulative_lift_to_drag_improvement": float(
                    deep_get(cfg, "completion.minimum_cumulative_ld_improvement", 0.002)
                ),
                "lift_runtime_bound_ratio": float(
                    deep_get(cfg, "advanced_settings.optimizer.lift_runtime_bound_ratio", 0.999)
                ),
                "lift_bound_tolerance_percent": float(
                    deep_get(cfg, "advanced_settings.optimizer.lift_bound_tolerance_percent", 0.02)
                ),
                "candidate_selection_policy": str(
                    deep_get(cfg, "optimization_run.candidate_selection_policy", "first-pass")
                ),
                "force_stability": copy.deepcopy(
                    deep_get(cfg, "optimization_run.force_stability", {})
                ),
            },
            "repair_profiles_enabled": bool(run_cfg.get("repair_on_profile_exhaustion", False)),
            "attempts": [],
        }
        self._active_attempt_runtime: dict[str, Any] | None = None

    def run(self, case_path: Path) -> dict[str, Any]:
        self.context.update(
            {
                "case_path": str(case_path),
                "output_dir": str(self.output_dir),
                "optimized_case_path": str(self.output_dir / "fluent" / "optimized_airfoil.cas.h5"),
            }
        )
        self._seed_optimizer_context()
        if self.dry_run:
            plan = self._render_plan()
            write_json(self.output_dir / "adjoint_execution_plan.json", plan)
            return {
                "status": "DRY_RUN",
                "plan_path": str(self.output_dir / "adjoint_execution_plan.json"),
                "runtime_resolution": self.runtime_resolution,
            }

        os.environ.update(prepare_fluent_env())
        try:
            import ansys.fluent.core as pyfluent
        except Exception as exc:
            raise RuntimeError(f"PyFluent is unavailable: {type(exc).__name__}: {exc}") from exc
        self.runtime_resolution["versions"]["pyfluent"] = str(getattr(pyfluent, "__version__", "unknown"))

        fluent_dir = self.output_dir / "fluent"
        fluent_dir.mkdir(parents=True, exist_ok=True)
        transcript = fluent_dir / "baseline_transcript.txt"
        solver = None
        try:
            solver = self._launch_solver(pyfluent, fluent_dir)
            self.runtime_resolution["versions"]["fluent"] = str(solver.get_fluent_version())
            solver.transcript.start(file_name=str(transcript))
            self._current_solver = solver
            self.transcript_path = transcript
            solver.settings.file.read_case(file_name=str(case_path))
            self._apply_physics_settings(solver)
            self._apply_flow_settings(solver)
            self._verify_full_physics_fingerprint(solver)
            self._apply_solution_settings(solver)
            self._initialize_and_iterate_flow(solver, int(deep_get(self.cfg, "iterations.flow", 1000)))
            self.baseline_force_stability, baseline = self._stabilize_force_coefficients(solver, "baseline")
            write_json(
                self.output_dir / "exports" / "baseline" / "baseline_force_stability.json",
                self.baseline_force_stability,
            )
            self.cfd_qualification = self._collect_cfd_qualification(solver, self.baseline_force_stability)
            write_json(self.output_dir / "cfd_qualification.json", self.cfd_qualification)
            self.context["baseline_cd"] = baseline.cd if baseline.cd is not None else 0.0
            self.context["baseline_cl"] = baseline.cl if baseline.cl is not None else 0.0
            self.context["minimum_allowed_cl"] = self._minimum_allowed_cl(baseline.cl)
            self.context["minimum_allowed_lift_force"] = self._minimum_allowed_lift_force(baseline.cl)
            self.baseline_geometry = self._surface_geometry_snapshot(solver)
            baseline_geometry_path = self.output_dir / "exports" / "baseline" / "baseline_airfoil_geometry.json"
            write_json(baseline_geometry_path, self.baseline_geometry)
            if self.baseline_geometry.get("status") != "PASS":
                raise RuntimeError(f"Baseline airfoil geometry is invalid: {self.baseline_geometry.get('errors', [])}")
            try:
                self._save_solution_exports(solver, "baseline", include_sensitivity=False)
            except Exception as exc:
                transcript_audit = transcript_morphing_audit(transcript)
                return {
                    "status": "FAIL_BASELINE_EXPORTS",
                    "baseline": baseline.__dict__,
                    "error": f"{type(exc).__name__}: {exc}",
                    "transcript_path": str(transcript),
                    "transcript_audit": transcript_audit,
                    "exports": self.exports,
                    "commands": self.commands,
                    "failures": self.failures,
                    "runtime_resolution": self.runtime_resolution,
                }
            baseline_exports = list(self.exports)
        except Exception as exc:
            transcript_audit = transcript_morphing_audit(transcript)
            return {
                "status": "FAIL_RUNTIME_EXCEPTION",
                "error": f"{type(exc).__name__}: {exc}",
                "transcript_path": str(transcript),
                "transcript_audit": transcript_audit,
                "exports": self.exports,
                "commands": self.commands,
                "failures": self.failures,
                "runtime_resolution": self.runtime_resolution,
            }
        finally:
            self._close_solver(solver)

        baseline_case = self.output_dir / "exports" / "baseline" / "baseline.cas.h5"
        baseline_data = self.output_dir / "exports" / "baseline" / "baseline.dat.h5"
        numerical_cfg = deep_get(self.cfg, "optimization_run.numerical_uncertainty", {}) or {}
        if bool(numerical_cfg.get("run_independent_reloads", True)):
            self.numerical_qualification = self._run_checkpoint_aa_calibration(
                pyfluent,
                baseline_case,
                baseline_data,
                repeat_count=int(numerical_cfg.get("required_repeats", 5)),
            )
        baseline_commands = list(self.commands)
        baseline_failures = list(self.failures)
        result = self._run_design_cycles(pyfluent, baseline, baseline_case, baseline_data)
        attempt_exports = list(result.pop("attempt_exports", []))
        attempt_commands = list(result.pop("attempt_commands", []))
        attempt_failures = list(result.pop("attempt_failures", []))
        return {
            **result,
            "baseline_force_stability": self.baseline_force_stability,
            "numerical_qualification": self.numerical_qualification,
            "cfd_qualification": self.cfd_qualification,
            "runtime_resolution": self.runtime_resolution,
            "shape_guard": self.shape_guard,
            "baseline_geometry_path": str(self.output_dir / "exports" / "baseline" / "baseline_airfoil_geometry.json"),
            "baseline_transcript_path": str(transcript),
            "baseline_transcript_audit": transcript_morphing_audit(transcript),
            "baseline_checkpoint": {"case": str(baseline_case), "data": str(baseline_data)},
            "optimized_case_path": str(self.context["optimized_case_path"]) if result.get("accepted_steps") else None,
            "optimized_geometry_path": (
                result.get("accepted_steps", [{}])[-1].get("geometry_path")
                if result.get("accepted_steps") else None
            ),
            "exports": baseline_exports + attempt_exports,
            "commands": baseline_commands + attempt_commands,
            "failures": baseline_failures + attempt_failures,
        }

    def _launch_solver(self, pyfluent: Any, cwd: Path) -> Any:
        cwd.mkdir(parents=True, exist_ok=True)
        return pyfluent.launch_fluent(
            dimension=2,
            precision="double",
            processor_count=int(self.cfg.get("processor_count", 1)),
            mode="solver",
            ui_mode="no_gui",
            additional_arguments="-g",
            start_timeout=int(self.cfg.get("start_timeout_seconds", 180)),
            # Shutdown is owned explicitly below. Avoid PyFluent's automatic
            # winkill fallback racing a successful graceful exit.
            cleanup_on_exit=False,
            cwd=str(cwd),
            **fluent_product_version_arg(self.cfg.get("product_version")),
            **fluent_path_arg(self.cfg.get("fluent_exe")),
        )

    def write_solution_interpolation(
        self,
        pyfluent: Any,
        case_path: Path,
        data_path: Path,
        output_path: Path,
        *,
        label: str,
        requested_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Export a converged checkpoint for deterministic mesh-to-mesh transfer."""
        case_path = Path(case_path).resolve()
        data_path = Path(data_path).resolve()
        output_path = Path(output_path).resolve()
        if not case_path.is_file() or not data_path.is_file():
            raise FileNotFoundError(f"Interpolation checkpoint is incomplete: {case_path}, {data_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        transcript = output_path.parent / f"{label}_write_interpolation.trn"
        solver = None
        try:
            self._check_hard_deadline(f"before interpolation export {label}")
            solver = self._launch_solver(pyfluent, output_path.parent)
            solver.transcript.start(file_name=str(transcript))
            solver.settings.file.read_case(file_name=str(case_path))
            solver.settings.file.read_data(file_name=str(data_path))
            command = solver.settings.file.interpolate.write_data
            allowed_fields = list(command.fields.allowed_values())
            fields = select_solution_interpolation_fields(allowed_fields, requested_fields)
            allowed_zones = [str(value) for value in command.cell_zones.allowed_values()]
            cell_zone = "fluid" if "fluid" in allowed_zones else (allowed_zones[0] if len(allowed_zones) == 1 else None)
            if cell_zone is None:
                raise RuntimeError(f"Unable to select an unambiguous interpolation cell zone: {allowed_zones}")
            self._settings_step(
                f"write {label} solution interpolation",
                lambda: command(
                    filename=str(output_path),
                    cell_zones=[cell_zone],
                    fields=fields,
                    binary_format=True,
                ),
                required=True,
            )
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise RuntimeError(f"Fluent did not create interpolation evidence: {output_path}")
            evidence = {
                "status": "PASS",
                "source_checkpoint": {
                    "case": str(case_path),
                    "case_sha256": hashlib.sha256(case_path.read_bytes()).hexdigest().upper(),
                    "data": str(data_path),
                    "data_sha256": hashlib.sha256(data_path.read_bytes()).hexdigest().upper(),
                },
                "interpolation_file": str(output_path),
                "interpolation_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest().upper(),
                "interpolation_bytes": output_path.stat().st_size,
                "cell_zones": [cell_zone],
                "fields": fields,
                "binary_format": True,
                "transcript": str(transcript),
            }
            write_json(output_path.with_suffix(output_path.suffix + ".evidence.json"), evidence)
            return evidence
        finally:
            self._close_solver(solver)

    def run_primal_qualification_case(
        self,
        pyfluent: Any,
        case_path: Path,
        *,
        label: str,
        interpolation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Solve and qualify one mesh without entering the adjoint/design workflow."""
        case_path = Path(case_path).resolve()
        solution_dir = self.output_dir / "solution"
        solution_dir.mkdir(parents=True, exist_ok=True)
        transcript = solution_dir / f"{label}_transcript.trn"
        solver = None
        try:
            self._check_hard_deadline(f"before primal grid solve {label}")
            solver = self._launch_solver(pyfluent, solution_dir)
            solver.transcript.start(file_name=str(transcript))
            self._current_solver = solver
            self.transcript_path = transcript
            solver.settings.file.read_case(file_name=str(case_path))
            self._apply_physics_settings(solver)
            self._apply_flow_settings(solver)
            self._verify_full_physics_fingerprint(solver)
            self._apply_solution_settings(solver)
            flow_iterations = int(deep_get(self.cfg, "iterations.flow", 1200))
            if interpolation:
                interpolation_path = Path(str(interpolation.get("interpolation_file", ""))).resolve()
                cell_zones = list(interpolation.get("cell_zones") or ["fluid"])
                expected_hash = str(interpolation.get("interpolation_sha256", "")).upper()
                if not interpolation_path.is_file():
                    raise FileNotFoundError(f"Missing solution interpolation file: {interpolation_path}")
                actual_hash = hashlib.sha256(interpolation_path.read_bytes()).hexdigest().upper()
                if not expected_hash or actual_hash != expected_hash:
                    raise RuntimeError("Solution interpolation checksum does not match its recorded evidence")
                self._settings_step(
                    f"read {label} solution interpolation",
                    lambda: solver.settings.file.interpolate.read_data(
                        filename=str(interpolation_path),
                        cell_zones=cell_zones,
                    ),
                    required=True,
                )
                self._iterate_interpolated_flow(solver, flow_iterations)
            else:
                self._initialize_and_iterate_flow(solver, flow_iterations)
            stability, coefficients = self._stabilize_force_coefficients(solver, label)
            qualification = self._collect_cfd_qualification(solver, stability)
            geometry = self._surface_geometry_snapshot(solver)
            checkpoint_case = solution_dir / f"{label}.cas.h5"
            checkpoint_data = solution_dir / f"{label}.dat.h5"
            self._settings_step(
                f"write {label} qualification case",
                lambda: solver.settings.file.write_case(file_name=str(checkpoint_case)),
                required=True,
            )
            self._settings_step(
                f"write {label} qualification data",
                lambda: solver.settings.file.write_data(file_name=str(checkpoint_data)),
                required=True,
            )
            return {
                "status": "PASS" if stability.get("status") == "PASS" else "FAIL",
                "label": label,
                "case_path": str(case_path),
                "checkpoint": {"case": str(checkpoint_case), "data": str(checkpoint_data)},
                "transcript": str(transcript),
                "force_stability": stability,
                "representative": {
                    "cd": coefficients.cd,
                    "cl": coefficients.cl,
                    "source": f"{label}_force_stability_tail_mean",
                },
                "cfd_qualification": qualification,
                "geometry": geometry,
                "initialization": (
                    {"method": "mesh_to_mesh_interpolation", "evidence": interpolation}
                    if interpolation
                    else {"method": str(deep_get(self.cfg, "advanced_settings.solution_controls.initialization", "standard"))}
                ),
            }
        except Exception as exc:
            return {
                "status": "FAIL",
                "label": label,
                "case_path": str(case_path),
                "transcript": str(transcript),
                "error": f"{type(exc).__name__}: {exc}",
                "commands": list(self.commands),
                "failures": list(self.failures),
            }
        finally:
            self._close_solver(solver)
            self._current_solver = None

    def _surface_geometry_snapshot(self, solver: Any) -> dict[str, Any]:
        from ansys.fluent.core.services.field_data import SurfaceDataType

        wall_zone = str(deep_get(self.cfg, "advanced_settings.solver.wall_zone", "airfoil"))
        field_data = solver.fields.field_data.get_surface_data(
            data_types=[SurfaceDataType.Vertices, SurfaceDataType.FacesConnectivity],
            surfaces=[wall_zone],
        )
        surface_data = field_data.get(wall_zone) or next(iter(field_data.values()))
        vertices = surface_data[SurfaceDataType.Vertices]
        edges = surface_data[SurfaceDataType.FacesConnectivity]
        return build_geometry_snapshot(vertices, edges, surface=wall_zone)

    def _baseline_geometry(self) -> dict[str, Any]:
        if self.baseline_geometry is not None:
            return self.baseline_geometry
        path = self.output_dir / "exports" / "baseline" / "baseline_airfoil_geometry.json"
        if not path.exists():
            raise RuntimeError(f"Missing baseline airfoil geometry evidence: {path}")
        self.baseline_geometry = load_json(path)
        return self.baseline_geometry

    @staticmethod
    def _close_solver(solver: Any | None) -> None:
        close_fluent_session(solver)

    def _activate_profile(self, profile: dict[str, Any], baseline: Coefficients) -> None:
        self.context["design_iterations"] = int(profile.get("design_iterations", deep_get(self.cfg, "iterations.design", 1)))
        self.context["flow_iterations"] = int(profile.get("flow_iterations", deep_get(self.cfg, "iterations.flow", 1200)))
        self.context["adjoint_iterations"] = int(profile.get("adjoint_iterations", deep_get(self.cfg, "iterations.adjoint", 350)))
        self.context["drag_step_percent"] = float(profile.get("drag_step_percent", deep_get(self.cfg, "advanced_settings.optimizer.drag_step_percent", -0.0001)))
        self.context["lift_step_percent"] = float(profile.get("lift_step_percent", deep_get(self.cfg, "advanced_settings.optimizer.lift_step_percent", 0.0001)))
        objective_strategy = str(
            profile.get("objective_strategy", deep_get(self.cfg, "advanced_settings.optimizer.objective_strategy", "drag-with-lift-bound"))
        ).strip().lower()
        if objective_strategy not in {"coupled-drag-lift-step", "drag-with-lift-bound"}:
            raise ValueError(f"Unsupported optimizer objective strategy: {objective_strategy!r}")
        self.context["objective_strategy"] = objective_strategy
        self.context["lift_runtime_bound_ratio"] = float(
            profile.get("lift_runtime_bound_ratio", deep_get(self.cfg, "advanced_settings.optimizer.lift_runtime_bound_ratio", 0.999))
        )
        self.context["lift_bound_tolerance_percent"] = float(
            profile.get("lift_bound_tolerance_percent", deep_get(self.cfg, "advanced_settings.optimizer.lift_bound_tolerance_percent", 0.02))
        )
        if not 0.0 < self.context["lift_runtime_bound_ratio"] <= 1.1:
            raise ValueError("lift_runtime_bound_ratio must be in (0, 1.1]")
        if not 0.0 <= self.context["lift_bound_tolerance_percent"] <= 100.0:
            raise ValueError("lift_bound_tolerance_percent must be in [0, 100]")
        self.context["post_design_flow_iterations"] = int(profile.get("post_design_flow_iterations", deep_get(self.cfg, "advanced_settings.optimizer.post_design_flow_iterations", 10)))
        self.context["lift_force_report_to_observable_factor"] = float(
            profile.get("lift_force_report_to_observable_factor", deep_get(self.cfg, "advanced_settings.optimizer.lift_force_report_to_observable_factor", 1.0))
        )
        self.context["minimum_allowed_lift_force"] = self._minimum_allowed_lift_force(baseline.cl)
        self.context["morpher_method"] = str(
            profile.get("morpher_method", deep_get(self.cfg, "advanced_settings.design_tool.morpher.method", "radial-basis-function"))
        )
        if "x_control_points" in profile:
            self.context["x_control_points"] = int(profile["x_control_points"])
        if "y_control_points" in profile:
            self.context["y_control_points"] = int(profile["y_control_points"])

    def _run_design_cycles(
        self,
        pyfluent: Any,
        original: Coefficients,
        baseline_case: Path,
        baseline_data: Path,
        *,
        start_cycle: int = 1,
        current: Coefficients | None = None,
        seed_attempts: list[dict[str, Any]] | None = None,
        seed_accepted_steps: list[dict[str, Any]] | None = None,
        seed_improvements: list[float] | None = None,
        recovered_any: bool = False,
    ) -> dict[str, Any]:
        run_cfg = deep_get(self.cfg, "optimization_run", {})
        max_steps = int(run_cfg.get("max_accepted_design_steps", 1))
        configured_evaluations = run_cfg.get("max_solver_evaluations")
        max_evaluations = int(configured_evaluations) if configured_evaluations is not None else None
        convergence_threshold = float(run_cfg.get("relative_cd_convergence", 5.0e-4))
        consecutive_steps = int(run_cfg.get("consecutive_converged_steps", 2))
        performance_targets_enabled = bool(deep_get(self.cfg, "completion.performance_targets_enabled", False))
        minimum_cd_reduction = float(deep_get(self.cfg, "completion.minimum_cumulative_cd_reduction", 0.005))
        minimum_ld_improvement = float(deep_get(self.cfg, "completion.minimum_cumulative_ld_improvement", 0.002))
        minimum_lift_ratio = float(deep_get(self.cfg, "completion.minimum_lift_ratio", 0.998))
        profiles = optimization_attempt_profiles(self.cfg)
        attempts: list[dict[str, Any]] = list(seed_attempts or [])
        accepted_steps: list[dict[str, Any]] = list(seed_accepted_steps or [])
        all_commands: list[dict[str, Any]] = []
        all_failures: list[dict[str, Any]] = []
        accepted_exports: list[dict[str, Any]] = []
        improvements: list[float] = list(seed_improvements or [])
        current = current or original
        checkpoint_case = baseline_case
        checkpoint_data = baseline_data
        completion_reason = "maximum_accepted_design_steps"
        final_status = "PASS"
        performance = performance_target_state(
            original,
            current,
            minimum_cumulative_cd_reduction=minimum_cd_reduction,
            minimum_cumulative_ld_improvement=minimum_ld_improvement,
            minimum_lift_ratio=minimum_lift_ratio,
        )
        for cycle in range(start_cycle, max_steps + 1):
            remaining_evaluations = (
                max_evaluations - len(attempts) if max_evaluations is not None else len(profiles)
            )
            if remaining_evaluations <= 0:
                final_status = "INCOMPLETE_PERFORMANCE_TARGET" if accepted_steps else "INCOMPLETE_REPAIR_EXHAUSTED"
                completion_reason = "maximum_solver_evaluations"
                break
            cycle_result = self._run_cycle_attempts(
                pyfluent,
                cycle,
                profiles[:remaining_evaluations],
                original,
                current,
                checkpoint_case,
                checkpoint_data,
            )
            attempts.extend(cycle_result["attempts"])
            all_commands.extend(cycle_result["commands"])
            all_failures.extend(cycle_result["failures"])
            if not cycle_result.get("accepted"):
                objective_binding_failed = any(
                    attempt.get("status") == "FAIL_OBJECTIVE_RUNTIME_BINDING" for attempt in cycle_result.get("attempts", [])
                )
                anchor_failed = any(
                    attempt.get("status") == "FAIL_SHAPE_ANCHOR_SETUP" for attempt in cycle_result.get("attempts", [])
                )
                shape_exhausted = any(
                    attempt.get("status") == "FAIL_SHAPE_GATE" for attempt in cycle_result.get("attempts", [])
                )
                budget_exhausted = max_evaluations is not None and len(attempts) >= max_evaluations
                if objective_binding_failed:
                    completion_reason = "objective_runtime_binding_verification_failed"
                    incomplete_status = "FAIL_OBJECTIVE_RUNTIME_BINDING"
                elif anchor_failed:
                    completion_reason = "shape_anchor_setup_or_displacement_failed"
                    incomplete_status = "FAIL_SHAPE_ANCHOR_SETUP"
                elif shape_exhausted:
                    completion_reason = "shape_guard_rejected_all_remaining_profiles"
                    incomplete_status = "INCOMPLETE_SHAPE_GUARD_EXHAUSTED"
                elif budget_exhausted:
                    completion_reason = "maximum_solver_evaluations"
                    incomplete_status = "INCOMPLETE_PERFORMANCE_TARGET" if accepted_steps else "INCOMPLETE_REPAIR_EXHAUSTED"
                elif performance_targets_enabled and accepted_steps and not performance.get("achieved"):
                    completion_reason = "finite_profiles_exhausted_before_cumulative_performance_targets"
                    incomplete_status = "INCOMPLETE_PERFORMANCE_TARGET"
                else:
                    completion_reason = (
                        "finite_standard_and_repair_profiles_exhausted"
                        if bool(run_cfg.get("repair_on_profile_exhaustion", False))
                        else "finite_standard_profiles_exhausted"
                    )
                    incomplete_status = "INCOMPLETE_REPAIR_EXHAUSTED"
                return {
                    "status": incomplete_status,
                    "completion_reason": completion_reason,
                    "baseline": original.__dict__,
                    "final": current.__dict__,
                    "attempts": attempts,
                    "accepted_steps": accepted_steps,
                    "accepted_attempt": accepted_steps[-1]["attempt_index"] if accepted_steps else None,
                    "recovered_from_failed_attempts": bool(accepted_steps) and any(
                        not attempt.get("candidate_gate", {}).get("accepted") for attempt in attempts
                    ),
                    "accepted_recovered_attempts": recovered_any,
                    "final_checkpoint": {"case": str(checkpoint_case), "data": str(checkpoint_data)},
                    "final_shape_guard": accepted_steps[-1].get("shape_guard_validation") if accepted_steps else None,
                    "optimized_export_dir": str(self.output_dir / "exports" / "optimized") if accepted_steps else None,
                    "convergence": design_convergence_state(improvements, convergence_threshold, consecutive_steps),
                    "performance_target": performance,
                    "attempt_exports": accepted_exports,
                    "attempt_commands": all_commands,
                    "attempt_failures": all_failures,
                }
            step = cycle_result["accepted"]
            accepted_steps.append(step)
            current = Coefficients(**step["final"])
            checkpoint_case = Path(step["checkpoint"]["case"])
            checkpoint_data = Path(step["checkpoint"]["data"])
            accepted_exports = list(cycle_result.get("accepted_exports", accepted_exports))
            recovered_any = recovered_any or bool(step["candidate_gate"].get("recovered"))
            improvements.append(float(step["candidate_gate"]["relative_drag_improvement_from_previous"]))
            convergence = design_convergence_state(improvements, convergence_threshold, consecutive_steps)
            performance = performance_target_state(
                original,
                current,
                minimum_cumulative_cd_reduction=minimum_cd_reduction,
                minimum_cumulative_ld_improvement=minimum_ld_improvement,
                minimum_lift_ratio=minimum_lift_ratio,
            )
            step["performance_target"] = copy.deepcopy(performance)
            completion = design_completion_state(
                performance_targets_enabled=performance_targets_enabled,
                performance=performance,
                convergence=convergence,
                accepted_step_limit_reached=cycle >= max_steps,
            )
            if completion["action"] == "STOP":
                final_status = str(completion["status"])
                completion_reason = str(completion["completion_reason"])
                break
            if max_evaluations is not None and len(attempts) >= max_evaluations:
                final_status = "INCOMPLETE_PERFORMANCE_TARGET"
                completion_reason = "maximum_solver_evaluations"
                break
        return {
            "status": final_status,
            "completion_reason": completion_reason,
            "baseline": original.__dict__,
            "final": current.__dict__,
            "attempts": attempts,
            "accepted_steps": accepted_steps,
            "accepted_attempt": accepted_steps[-1]["attempt_index"] if accepted_steps else None,
            "recovered_from_failed_attempts": bool(accepted_steps) and any(
                not attempt.get("candidate_gate", {}).get("accepted") for attempt in attempts
            ),
            "accepted_recovered_attempts": recovered_any,
            "transcript_path": accepted_steps[-1]["transcript_path"] if accepted_steps else None,
            "transcript_audit": accepted_steps[-1]["transcript_audit"] if accepted_steps else None,
            "final_checkpoint": {"case": str(checkpoint_case), "data": str(checkpoint_data)},
            "final_shape_guard": accepted_steps[-1].get("shape_guard_validation") if accepted_steps else None,
            "optimized_export_dir": str(self.output_dir / "exports" / "optimized") if accepted_steps else None,
            "convergence": design_convergence_state(improvements, convergence_threshold, consecutive_steps),
            "performance_target": performance,
            "attempt_exports": accepted_exports,
            "attempt_commands": all_commands,
            "attempt_failures": all_failures,
        }

    def _run_cycle_attempts(
        self,
        pyfluent: Any,
        cycle: int,
        profiles: list[dict[str, Any]],
        original: Coefficients,
        previous: Coefficients,
        checkpoint_case: Path,
        checkpoint_data: Path,
    ) -> dict[str, Any]:
        if not (self.runtime_resolution.get("versions") or {}).get("pyfluent"):
            self.runtime_resolution.setdefault("versions", {})["pyfluent"] = str(
                getattr(pyfluent, "__version__", "unknown")
            )
        attempts: list[dict[str, Any]] = []
        qualified_candidates: list[dict[str, Any]] = []
        all_commands: list[dict[str, Any]] = []
        all_failures: list[dict[str, Any]] = []
        selection_policy = str(
            deep_get(self.cfg, "optimization_run.candidate_selection_policy", "first-pass")
        ).strip().lower()
        if selection_policy not in {"first-pass", "best-of-cycle"}:
            raise ValueError("candidate_selection_policy must be first-pass or best-of-cycle")
        for index, profile in enumerate(profiles):
            stop_profiles = False
            global_index = sum(1 for _ in (self.output_dir / "attempts").glob("cycle_*_attempt_*"))
            attempt_dir = self.output_dir / "attempts" / f"cycle_{cycle:02d}_attempt_{index:02d}_{profile.get('name', 'profile')}"
            if attempt_dir.exists():
                attempt_dir = attempt_dir.with_name(f"{attempt_dir.name}_resume_{global_index:02d}")
            transcript = attempt_dir / "adjoint_optimization_transcript.txt"
            solver = None
            self.commands = []
            self.failures = []
            self.exports = []
            self._seed_optimizer_context()
            self._activate_profile(profile, original)
            attempt: dict[str, Any] = {"index": global_index, "cycle": cycle, "profile_index": index, "profile": dict(profile), "run_dir": str(attempt_dir)}
            self._active_attempt_runtime = {
                "attempt_index": global_index,
                "cycle": cycle,
                "profile_index": index,
                "profile_name": profile.get("name", "profile"),
            }
            self.runtime_resolution["attempts"].append(self._active_attempt_runtime)
            try:
                solver = self._launch_solver(pyfluent, attempt_dir)
                self.runtime_resolution["versions"]["fluent"] = str(solver.get_fluent_version())
                solver.transcript.start(file_name=str(transcript))
                self._current_solver = solver
                self.transcript_path = transcript
                self._settings_step("restore cycle checkpoint case", lambda: solver.settings.file.read_case(file_name=str(checkpoint_case)), required=True)
                self._settings_step("restore cycle checkpoint data", lambda: solver.settings.file.read_data(file_name=str(checkpoint_data)), required=True)
                previous_geometry = self._surface_geometry_snapshot(solver)
                self._setup_adjoint_observables(solver)
                self._setup_design_tool(solver)
                self._configure_and_run_optimizer(solver)
                # Fluent 25.1 can report a successful TUI sensitivity export without creating the
                # file when the Windows path approaches MAX_PATH.  Export to a deliberately short
                # native path, then copy to the descriptive staged name after validation.
                preserved_sensitivity = attempt_dir / "preflow.sens"
                self._export_with_tui_candidates(
                    "candidate_pre_flow_update",
                    "shape_sensitivity",
                    [preserved_sensitivity],
                    [
                        f'/adjoint/design-tool/objectives/manage/export-sensitivities "{preserved_sensitivity}"',
                    ],
                    required=bool(deep_get(self.cfg, "exports.required", True)),
                )
                candidate_geometry = self._surface_geometry_snapshot(solver)
                anchor_quick = anchor_displacement_audit(
                    self._baseline_geometry(),
                    candidate_geometry,
                    (self._active_attempt_runtime or {}).get("shape_anchors"),
                )
                quick_shape_report = compare_geometry(
                    self._baseline_geometry(), previous_geometry, candidate_geometry, self.shape_guard
                )
                thickness_runtime = (self._active_attempt_runtime or {}).get("thickness_constraint") or {}
                thickness_quick = thickness_geometry_audit(
                    self._baseline_geometry(),
                    candidate_geometry,
                    margin_percent=float(thickness_runtime.get("clearance_percent_of_baseline_max_thickness", 5.0)),
                    enabled=bool(thickness_runtime.get("enabled", True)),
                    samples=int(thickness_runtime.get("samples", 401)),
                    minimum_local_thickness_ratio=float(thickness_runtime.get("minimum_local_thickness_ratio", 0.90)),
                    minimum_area_ratio=float(thickness_runtime.get("minimum_area_ratio", 0.95)),
                )
                write_json(attempt_dir / "shape_anchor_quick.json", anchor_quick)
                write_json(attempt_dir / "shape_guard_quick.json", quick_shape_report)
                write_json(attempt_dir / "thickness_constraint_quick.json", thickness_quick)
                write_json(attempt_dir / "candidate_airfoil_geometry.json", candidate_geometry)
                if quick_shape_report.get("status") == "WARN":
                    print(f"[翼型形变预警] 周期 {cycle} / 档位 {profile.get('name', index)}: {', '.join(quick_shape_report.get('warnings', []))}")
                candidate_case = attempt_dir / "candidate.cas.h5"
                candidate_data = attempt_dir / "candidate.dat.h5"
                if anchor_quick.get("status") == "FAIL":
                    self._settings_step("write anchor-rejected candidate case", lambda: solver.settings.file.write_case(file_name=str(candidate_case)), required=True)
                    self._settings_step("write anchor-rejected candidate data", lambda: solver.settings.file.write_data(file_name=str(candidate_data)), required=True)
                    audit = transcript_morphing_audit(transcript)
                    attempt.update(
                        {
                            "status": "FAIL_SHAPE_ANCHOR_SETUP",
                            "transcript_path": str(transcript),
                            "transcript_audit": audit,
                            "shape_anchor_quick": anchor_quick,
                            "shape_guard_quick": quick_shape_report,
                            "candidate_gate": {
                                "status": "FAIL_SHAPE_ANCHOR_SETUP",
                                "accepted": False,
                                "recovered": False,
                                "reasons": list(anchor_quick.get("errors", [])),
                            },
                            "passed_completion_gate": False,
                        }
                    )
                    continue
                if thickness_quick.get("status") == "FAIL":
                    print(
                        f"[厚度约束回退] 周期 {cycle} / 档位 {profile.get('name', index)}: "
                        f"{', '.join(thickness_quick.get('errors', []))}"
                    )
                    self._settings_step("write thickness-rejected candidate case", lambda: solver.settings.file.write_case(file_name=str(candidate_case)), required=True)
                    self._settings_step("write thickness-rejected candidate data", lambda: solver.settings.file.write_data(file_name=str(candidate_data)), required=True)
                    audit = transcript_morphing_audit(transcript)
                    attempt.update(
                        {
                            "status": "FAIL_THICKNESS_GATE",
                            "transcript_path": str(transcript),
                            "transcript_audit": audit,
                            "shape_anchor_quick": anchor_quick,
                            "shape_guard_quick": quick_shape_report,
                            "thickness_constraint_quick": thickness_quick,
                            "candidate_gate": {
                                "status": "FAIL_THICKNESS_GATE",
                                "accepted": False,
                                "recovered": False,
                                "reasons": list(thickness_quick.get("errors", [])),
                            },
                            "passed_completion_gate": False,
                        }
                    )
                    continue
                if quick_shape_report.get("status") == "FAIL":
                    print(f"[翼型形变回退] 周期 {cycle} / 档位 {profile.get('name', index)}: {', '.join(quick_shape_report.get('hard_failures', []))}")
                    self._settings_step("write shape-rejected candidate case", lambda: solver.settings.file.write_case(file_name=str(candidate_case)), required=True)
                    self._settings_step("write shape-rejected candidate data", lambda: solver.settings.file.write_data(file_name=str(candidate_data)), required=True)
                    audit = transcript_morphing_audit(transcript)
                    attempt.update(
                        {
                            "status": "FAIL_SHAPE_GATE",
                            "transcript_path": str(transcript),
                            "transcript_audit": audit,
                            "shape_guard_quick": quick_shape_report,
                            "candidate_gate": {
                                "status": "FAIL_SHAPE_GATE",
                                "accepted": False,
                                "recovered": False,
                                "reasons": list(quick_shape_report.get("hard_failures", [])),
                                "shape_warnings": list(quick_shape_report.get("warnings", [])),
                            },
                            "passed_completion_gate": False,
                        }
                    )
                    continue
                self._post_design_flow_update(solver, profile)
                final = self._compute_coefficients(solver, f"cycle_{cycle}_attempt_{index}_candidate")
                self._settings_step("write staged candidate case", lambda: solver.settings.file.write_case(file_name=str(candidate_case)), required=True)
                self._settings_step("write staged candidate data", lambda: solver.settings.file.write_data(file_name=str(candidate_data)), required=True)
                audit = transcript_morphing_audit(transcript)
                required_oq = float(self.context.get("resolved_optimizer_min_orthogonal_quality", 0.10))
                validation_result = self._validate_candidate_checkpoint(
                    pyfluent,
                    cycle,
                    index,
                    original,
                    previous,
                    candidate_case,
                    candidate_data,
                    audit,
                    required_oq,
                    previous_geometry,
                    preserved_sensitivity=preserved_sensitivity,
                    stage_only=selection_policy == "best-of-cycle",
                )
                gate = validation_result["candidate_gate"]
                validated_final = validation_result.get("final", final.__dict__)
                attempt.update(
                    {
                        "status": gate["status"] if gate.get("accepted") else "FAIL",
                        "final": validated_final,
                        "transcript_path": str(transcript),
                        "transcript_audit": audit,
                        "candidate_validation": validation_result["validation"],
                        "geometry_path": validation_result.get("geometry_path"),
                        "shape_guard_quick": quick_shape_report,
                        "shape_anchor_quick": anchor_quick,
                        "shape_guard_validation": validation_result["validation"].get("shape_guard"),
                        "shape_anchor_validation": validation_result["validation"].get("shape_anchor"),
                        "candidate_gate": gate,
                        "passed_completion_gate": bool(gate.get("accepted")),
                    }
                )
                attempt["runtime_resolution"] = copy.deepcopy(self._active_attempt_runtime)
                if gate.get("accepted"):
                    attempt["checkpoint"] = validation_result["checkpoint"]
                    attempt["staged_export_dir"] = validation_result.get("staged_export_dir")
                    attempt["selected_for_cycle"] = selection_policy == "first-pass"
                    attempts.append(attempt)
                    accepted_candidate = {
                        "attempt": attempt,
                        "cycle": cycle,
                        "attempt_index": global_index,
                        "profile": dict(profile),
                        "final": validated_final,
                        "transcript_path": str(transcript),
                        "transcript_audit": audit,
                        "candidate_validation": validation_result["validation"],
                        "geometry_path": validation_result.get("geometry_path"),
                        "shape_guard_quick": quick_shape_report,
                        "shape_anchor_quick": anchor_quick,
                        "shape_guard_validation": validation_result["validation"].get("shape_guard"),
                        "shape_anchor_validation": validation_result["validation"].get("shape_anchor"),
                        "candidate_gate": gate,
                        "checkpoint": validation_result["checkpoint"],
                        "staged_export_dir": validation_result.get("staged_export_dir"),
                        "exports": validation_result.get("exports", []),
                    }
                    if selection_policy == "best-of-cycle":
                        qualified_candidates.append(accepted_candidate)
                        continue
                    return {
                        "attempts": attempts,
                        "accepted": {
                            "cycle": cycle,
                            "attempt_index": global_index,
                            "profile": dict(profile),
                            "final": validated_final,
                            "transcript_path": str(transcript),
                            "transcript_audit": audit,
                            "candidate_validation": validation_result["validation"],
                            "geometry_path": validation_result.get("geometry_path"),
                            "shape_guard_quick": quick_shape_report,
                            "shape_anchor_quick": anchor_quick,
                            "shape_guard_validation": validation_result["validation"].get("shape_guard"),
                            "shape_anchor_validation": validation_result["validation"].get("shape_anchor"),
                            "candidate_gate": gate,
                            "checkpoint": validation_result["checkpoint"],
                        },
                        "accepted_exports": validation_result.get("exports", []),
                        "commands": all_commands + self.commands,
                        "failures": all_failures + self.failures,
                    }
            except Exception as exc:
                audit = transcript_morphing_audit(transcript)
                if isinstance(exc, ObjectiveRuntimeBindingError):
                    failure_status = "FAIL_OBJECTIVE_RUNTIME_BINDING"
                    failure_reason = "objective_runtime_binding"
                    stop_profiles = True
                elif isinstance(exc, OptimizerDesignStepError):
                    failure_status = "FAIL_OPTIMIZER_DESIGN_STEP"
                    failure_reason = "optimizer_design_step_invalid_retry_with_smaller_step"
                elif isinstance(exc, ShapeAnchorSetupError):
                    failure_status = "FAIL_SHAPE_ANCHOR_SETUP"
                    failure_reason = "shape_anchor_setup"
                    stop_profiles = True
                else:
                    failure_status = "FAIL"
                    failure_reason = "runtime_exception"
                attempt.update(
                    {
                        "status": failure_status,
                        "error": f"{type(exc).__name__}: {exc}",
                        "transcript_path": str(transcript),
                        "transcript_audit": audit,
                        "candidate_gate": {"status": failure_status, "accepted": False, "reasons": [failure_reason]},
                    }
                )
                attempt["runtime_resolution"] = copy.deepcopy(self._active_attempt_runtime)
            finally:
                attempt.setdefault("runtime_resolution", copy.deepcopy(self._active_attempt_runtime))
                attempts.append(attempt) if not attempts or attempts[-1] is not attempt else None
                all_commands.extend(self.commands)
                all_failures.extend(self.failures)
                self._close_solver(solver)
                self._active_attempt_runtime = None
            if stop_profiles:
                break
        if qualified_candidates:
            selected_attempt = select_best_candidate([item["attempt"] for item in qualified_candidates])
            selected = next(item for item in qualified_candidates if item["attempt"] is selected_attempt)
            promoted = self._promote_best_of_cycle_candidate(selected, cycle)
            selected["checkpoint"] = promoted["checkpoint"]
            selected["attempt"]["checkpoint"] = promoted["checkpoint"]
            selected["attempt"]["selected_for_cycle"] = True
            selected["attempt"]["selection_policy"] = "best-of-cycle"
            for item in qualified_candidates:
                if item is not selected:
                    item["attempt"]["selected_for_cycle"] = False
                    item["attempt"]["selection_policy"] = "best-of-cycle"
            accepted = {key: value for key, value in selected.items() if key not in {"attempt", "exports", "staged_export_dir"}}
            return {
                "attempts": attempts,
                "accepted": accepted,
                "accepted_exports": selected.get("exports", []),
                "commands": all_commands,
                "failures": all_failures,
                "selection": {
                    "policy": "best-of-cycle",
                    "qualified_attempt_indices": [item["attempt_index"] for item in qualified_candidates],
                    "selected_attempt_index": selected["attempt_index"],
                    "cd_tie_percentage_points": 0.01,
                },
            }
        return {
            "attempts": attempts,
            "accepted": None,
            "commands": all_commands,
            "failures": all_failures,
        }

    def _validate_candidate_checkpoint(
        self,
        pyfluent: Any,
        cycle: int,
        attempt_index: int,
        original: Coefficients,
        previous: Coefficients,
        candidate_case: Path,
        candidate_data: Path,
        morph_audit: dict[str, Any],
        required_oq: float,
        previous_geometry: dict[str, Any] | None = None,
        *,
        preserved_sensitivity: Path | None = None,
        stage_only: bool = False,
    ) -> dict[str, Any]:
        validation_dir = candidate_case.parent / "validation"
        transcript = validation_dir / "candidate_validation_transcript.txt"
        solver = None
        validation: dict[str, Any] = {"status": "FAIL", "transcript_path": str(transcript)}
        try:
            solver = self._launch_solver(pyfluent, validation_dir)
            solver.transcript.start(file_name=str(transcript))
            self._current_solver = solver
            self.transcript_path = transcript
            solver.settings.file.read_case(file_name=str(candidate_case))
            solver.settings.file.read_data(file_name=str(candidate_data))
            candidate_geometry = self._surface_geometry_snapshot(solver)
            shape_report = compare_geometry(
                self._baseline_geometry(), previous_geometry or self._baseline_geometry(), candidate_geometry, self.shape_guard
            )
            anchor_report = anchor_displacement_audit(
                self._baseline_geometry(),
                candidate_geometry,
                (self._active_attempt_runtime or {}).get("shape_anchors"),
            )
            thickness_runtime = (self._active_attempt_runtime or {}).get("thickness_constraint") or {}
            thickness_report = thickness_geometry_audit(
                self._baseline_geometry(),
                candidate_geometry,
                margin_percent=float(thickness_runtime.get("clearance_percent_of_baseline_max_thickness", 5.0)),
                enabled=bool(thickness_runtime.get("enabled", True)),
                samples=int(thickness_runtime.get("samples", 401)),
                minimum_local_thickness_ratio=float(thickness_runtime.get("minimum_local_thickness_ratio", 0.90)),
                minimum_area_ratio=float(thickness_runtime.get("minimum_area_ratio", 0.95)),
            )
            write_json(validation_dir / "candidate_airfoil_geometry.json", candidate_geometry)
            write_json(validation_dir / "shape_guard_validation.json", shape_report)
            write_json(validation_dir / "shape_anchor_validation.json", anchor_report)
            write_json(validation_dir / "thickness_constraint_validation.json", thickness_report)
            validation["shape_guard"] = shape_report
            validation["shape_anchor"] = anchor_report
            validation["thickness_constraint"] = thickness_report
            console = io.StringIO()
            with redirect_stdout(console):
                solver.tui.mesh.check()
                solver.tui.mesh.quality()
            text = (transcript.read_text(encoding="utf-8", errors="ignore") if transcript.exists() else "") + "\n" + console.getvalue()
            oq_match = re.findall(r"Minimum Orthogonal Quality\s*=\s*([0-9.eE+-]+)", text, re.IGNORECASE)
            ar_match = re.findall(r"Maximum Aspect Ratio\s*=\s*([0-9.eE+-]+)", text, re.IGNORECASE)
            quad_match = re.findall(r"([0-9]+)\s+quadrilateral cells", text, re.IGNORECASE)
            tri_match = re.findall(r"([0-9]+)\s+triangular cells", text, re.IGNORECASE)
            unreferenced_matches = re.findall(r"([0-9]+)\s+2D unreferenced faces", text, re.IGNORECASE)
            wall_match = re.findall(r"([0-9]+)\s+2D wall faces", text, re.IGNORECASE)
            inlet_match = re.findall(r"([0-9]+)\s+2D velocity-inlet faces", text, re.IGNORECASE)
            outlet_match = re.findall(r"([0-9]+)\s+2D pressure-outlet faces", text, re.IGNORECASE)
            minimum_oq = float(oq_match[-1]) if oq_match else None
            validation.update(
                {
                    "minimum_orthogonal_quality": minimum_oq,
                    "maximum_aspect_ratio": float(ar_match[-1]) if ar_match else None,
                    "quadrilateral_cells": int(quad_match[-1]) if quad_match else None,
                    "triangular_cells": int(tri_match[-1]) if tri_match else 0,
                    "unreferenced_faces": sum(int(value) for value in unreferenced_matches),
                    "wall_faces": int(wall_match[-1]) if wall_match else None,
                    "velocity_inlet_faces": int(inlet_match[-1]) if inlet_match else None,
                    "pressure_outlet_faces": int(outlet_match[-1]) if outlet_match else None,
                    "last_reported_cell_volume": morph_audit.get("last_reported_cell_volume"),
                    "required_orthogonal_quality": required_oq,
                    "has_wall": "wall faces" in text.lower(),
                    "has_velocity_inlet": "velocity-inlet faces" in text.lower(),
                    "has_pressure_outlet": "pressure-outlet faces" in text.lower(),
                    "negative_volume_in_validation": "negative volume" in text.lower(),
                }
            )
            sensitivity_phase = "before_post_design_flow_update"
            if preserved_sensitivity is None or not preserved_sensitivity.is_file() or preserved_sensitivity.stat().st_size <= 0:
                sensitivity_phase = "before_force_stability_iterations_fallback"
                preserved_sensitivity = validation_dir / "prestable.sens"
                self._export_with_tui_candidates(
                    "candidate_pre_stability",
                    "shape_sensitivity",
                    [preserved_sensitivity],
                    [
                        f'/adjoint/design-tool/objectives/manage/export-sensitivities "{preserved_sensitivity}"',
                    ],
                    required=bool(deep_get(self.cfg, "exports.required", True)),
                )
            validation["preserved_shape_sensitivity"] = {
                "status": "PASS" if preserved_sensitivity.is_file() and preserved_sensitivity.stat().st_size > 0 else "FAIL",
                "path": str(preserved_sensitivity),
                "phase": sensitivity_phase,
                "reason": "Fluent writes to a short native path to avoid the Windows MAX_PATH boundary; the staged descriptive name is created by a verified copy.",
            }
            stability, final = self._stabilize_force_coefficients(
                solver,
                f"cycle_{cycle}_attempt_{attempt_index}_validated",
            )
            validation["force_stability"] = stability
            validation["coefficient_basis"] = "force_stability_representative_mean"
            objective_runtime = (
                ((self._active_attempt_runtime or {}).get("objective_mapping") or {}).get("runtime_summary") or {}
            )
            control_points = (self._active_attempt_runtime or {}).get("control_points") or {}
            geometry_gate = geometry_change_gate(
                self._baseline_geometry(),
                candidate_geometry,
                maximum_design_iteration=objective_runtime.get("maximum_design_iteration"),
                design_variable_status=str(control_points.get("design_variable_status", "INACTIVE")),
                minimum_displacement_over_chord=float(
                    deep_get(
                        self.cfg,
                        "optimization_run.geometry_change.minimum_displacement_over_chord",
                        1.0e-6,
                    )
                ),
            )
            validation["geometry_change"] = geometry_gate
            baseline_representative = (self.baseline_force_stability or {}).get("representative") or {}
            candidate_representative = stability.get("representative") or {}
            confidence = improvement_confidence(
                baseline_representative,
                candidate_representative,
                noise_floor=float(self.numerical_qualification.get("noise_floor", 5.0e-4)),
            )
            validation["drag_improvement_confidence"] = confidence
            validation["numerical_qualification"] = copy.deepcopy(self.numerical_qualification)
            validation["status"] = "PASS" if (
                isinstance(minimum_oq, (int, float))
                and minimum_oq >= required_oq
                and isinstance(validation["quadrilateral_cells"], int)
                and validation["quadrilateral_cells"] > 0
                and validation["triangular_cells"] == 0
                and validation["has_wall"]
                and validation["has_velocity_inlet"]
                and validation["has_pressure_outlet"]
                and validation["wall_faces"] == self.context.get("initial_wall_faces", validation["wall_faces"])
                and validation["velocity_inlet_faces"] == self.context.get("initial_velocity_inlet_faces", validation["velocity_inlet_faces"])
                and validation["pressure_outlet_faces"] == self.context.get("initial_pressure_outlet_faces", validation["pressure_outlet_faces"])
                and not validation["negative_volume_in_validation"]
                and shape_report.get("status") in {"PASS", "WARN", "DISABLED"}
                and anchor_report.get("status") in {"PASS", "DISABLED"}
                and thickness_report.get("status") in {"PASS", "DISABLED"}
                and stability.get("status") == "PASS"
                and geometry_gate.get("status") == "PASS"
                and confidence.get("accepted") is True
                and self.numerical_qualification.get("qualification") == "QUALIFIED"
                and self.cfd_qualification.get("qualification") == "QUALIFIED"
            ) else "FAIL"
            run_cfg = deep_get(self.cfg, "optimization_run", {})
            gate = assess_candidate(
                original,
                previous,
                final,
                morph_audit,
                validation,
                lift_tolerance=float(deep_get(self.cfg, "completion.lift_relative_tolerance", 0.005)),
                accept_recovered=bool(run_cfg.get("accept_recovered_attempts", False)),
                minimum_relative_drag_improvement=float(run_cfg.get("minimum_relative_drag_improvement", 5.0e-4)),
                required_orthogonal_quality=required_oq,
                shape_report=shape_report,
                minimum_lift_ratio=float(deep_get(self.cfg, "completion.minimum_lift_ratio", 0.998)),
                require_lift_to_drag_improvement=bool(
                    deep_get(self.cfg, "completion.require_stepwise_lift_to_drag_improvement", False)
                ),
            )
            trust_reasons = [
                *geometry_gate.get("reasons", []),
                *confidence.get("reasons", []),
            ]
            if self.numerical_qualification.get("qualification") != "QUALIFIED":
                trust_reasons.append("numerical_calibration_not_qualified")
            if self.cfd_qualification.get("qualification") != "QUALIFIED":
                trust_reasons.append("cfd_qualification_not_qualified")
            if trust_reasons:
                gate["accepted"] = False
                gate["status"] = "FAIL_TRUST_GATE"
                gate["reasons"] = list(dict.fromkeys([*gate.get("reasons", []), *trust_reasons]))
            gate["geometry_change"] = geometry_gate
            gate["drag_improvement_confidence"] = confidence
            gate["numerical_qualification"] = copy.deepcopy(self.numerical_qualification)
            gate["cfd_qualification"] = copy.deepcopy(self.cfd_qualification)
            if not gate["accepted"]:
                return {"validation": validation, "candidate_gate": gate, "final": final.__dict__}
            accepted_dir = (
                candidate_case.parent / "q"
                if stage_only
                else self.output_dir / "accepted_steps" / f"step_{cycle:02d}"
            )
            accepted_dir.mkdir(parents=True, exist_ok=True)
            accepted_case = accepted_dir / "accepted.cas.h5"
            accepted_data = accepted_dir / "accepted.dat.h5"
            solver.settings.file.write_case(file_name=str(accepted_case))
            solver.settings.file.write_data(file_name=str(accepted_data))
            staged_export_dir = accepted_dir / "x" if stage_only else accepted_dir / "staged_exports" / "optimized"
            self.exports = []
            self._save_solution_exports(solver, "optimized", include_sensitivity=False, export_dir=staged_export_dir)
            staged_sensitivity = staged_export_dir / "optimized_shape_sensitivity.sens"
            shutil.copy2(preserved_sensitivity, staged_sensitivity)
            self.exports.append(
                {
                    "label": "optimized",
                    "kind": "shape_sensitivity",
                    "path": str(staged_sensitivity),
                    "status": "PASS",
                    "preserved_before_force_stability": True,
                }
            )
            manifest_path = staged_export_dir / "export_manifest.json"
            manifest = load_json(manifest_path)
            manifest.update(
                {
                    "includes_sensitivity": True,
                    "shape_sensitivity": str(staged_sensitivity),
                    "shape_sensitivity_phase": sensitivity_phase,
                    "sensitivity_ensight_case": None,
                    "sensitivity_ensight_files": [],
                }
            )
            write_json(manifest_path, manifest)
            if not stage_only:
                self._promote_staged_exports(staged_export_dir)
                solver.settings.file.write_case(file_name=str(self.context["optimized_case_path"]))
            return {
                "validation": validation,
                "candidate_gate": gate,
                "final": final.__dict__,
                "geometry_path": str(validation_dir / "candidate_airfoil_geometry.json"),
                "checkpoint": {"case": str(accepted_case), "data": str(accepted_data)},
                "exports": list(self.exports),
                "staged_export_dir": str(staged_export_dir),
            }
        except Exception as exc:
            validation["error"] = f"{type(exc).__name__}: {exc}"
            validation["status"] = "FAIL"
            return {
                "validation": validation,
                "candidate_gate": {"status": "FAIL", "accepted": False, "recovered": False, "reasons": ["candidate_validation_or_export_exception"]},
            }
        finally:
            self._close_solver(solver)

    def _promote_best_of_cycle_candidate(self, selected: dict[str, Any], cycle: int) -> dict[str, Any]:
        source_checkpoint = selected.get("checkpoint") or {}
        source_case = Path(str(source_checkpoint.get("case", "")))
        source_data = Path(str(source_checkpoint.get("data", "")))
        staged_export_dir = Path(str(selected.get("staged_export_dir", "")))
        if not source_case.is_file() or not source_data.is_file() or not staged_export_dir.is_dir():
            raise RuntimeError("best-of-cycle selected candidate is missing checkpoint or staged exports")
        accepted_dir = self.output_dir / "accepted_steps" / f"step_{cycle:02d}"
        accepted_dir.mkdir(parents=True, exist_ok=True)
        accepted_case = accepted_dir / "accepted.cas.h5"
        accepted_data = accepted_dir / "accepted.dat.h5"
        shutil.copy2(source_case, accepted_case)
        shutil.copy2(source_data, accepted_data)
        canonical_staged = accepted_dir / "staged_exports" / "optimized"
        if canonical_staged.exists():
            shutil.rmtree(canonical_staged)
        shutil.copytree(staged_export_dir, canonical_staged)
        canonical_manifest = canonical_staged / "export_manifest.json"
        if canonical_manifest.exists():
            manifest = load_json(canonical_manifest)
            source_prefix = str(staged_export_dir.resolve())
            canonical_prefix = str(canonical_staged.resolve())

            def canonical_path(value: Any) -> Any:
                if isinstance(value, str) and value.startswith(source_prefix):
                    return canonical_prefix + value[len(source_prefix) :]
                if isinstance(value, dict):
                    return {key: canonical_path(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [canonical_path(item) for item in value]
                return value

            write_json(canonical_manifest, canonical_path(manifest))
        self._promote_staged_exports(canonical_staged)
        optimized_case = Path(str(self.context["optimized_case_path"]))
        optimized_case.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(accepted_case, optimized_case)
        return {"checkpoint": {"case": str(accepted_case), "data": str(accepted_data)}}

    def _stabilize_force_coefficients(self, solver: Any, label: str) -> tuple[dict[str, Any], Coefficients]:
        cfg = deep_get(self.cfg, "optimization_run.force_stability", {}) or {}
        enabled = bool(cfg.get("enabled", True))
        block_iterations = int(cfg.get("iterations_per_block", 10))
        max_iterations = int(cfg.get("max_iterations", 100))
        relative_tolerance = float(cfg.get("relative_tolerance", 5.0e-6))
        consecutive_blocks = int(cfg.get("consecutive_blocks", 2))
        if block_iterations < 1 or max_iterations < block_iterations or consecutive_blocks < 1:
            raise ValueError("force stability iteration settings are invalid")
        if not 0.0 < relative_tolerance < 1.0:
            raise ValueError("force stability relative_tolerance must be in (0, 1)")
        first = self._compute_coefficients(solver, f"{label}_stability_0")
        initial_iteration = self._current_solver_iteration(solver)
        samples = [
            {
                "requested_iterations": 0,
                "actual_iterations": 0,
                "solver_iteration": initial_iteration,
                "cd": first.cd,
                "cl": first.cl,
                "source": first.source,
            }
        ]

        def representative_coefficient(state: dict[str, Any], fallback: Coefficients) -> Coefficients:
            representative = coefficient_representative(samples, tail_count=3)
            state["representative"] = representative
            if representative.get("status") != "PASS":
                return fallback
            return Coefficients(
                cd=float(representative["cd"]["mean"]),
                cl=float(representative["cl"]["mean"]),
                source=f"{label}_force_stability_tail_mean",
            )

        if not enabled:
            disabled = {
                "status": "DISABLED",
                "stable": False,
                "samples": samples,
                "reason": "optimization_run.force_stability.enabled=false",
                "coefficient_drift_from_initial": force_stability_drift(samples),
            }
            return disabled, representative_coefficient(disabled, first)
        final = first
        residual_equations = solver.settings.solution.monitor.residual.equations
        convergence_checks = {
            str(name): bool(residual_equations[name].check_convergence())
            for name in residual_equations.keys()
        }
        actual_completed = 0
        protocol_errors: list[str] = []
        try:
            for name in convergence_checks:
                residual_equations[name].check_convergence = False
            disabled_readback = {
                name: bool(residual_equations[name].check_convergence())
                for name in convergence_checks
            }
            if any(disabled_readback.values()):
                raise RuntimeError("Fluent residual convergence checks could not be disabled for force stability audit")
            for requested in range(block_iterations, max_iterations + 1, block_iterations):
                self._check_hard_deadline(f"{label} before stability block {requested}")
                before_iteration = self._current_solver_iteration(solver)
                solver.settings.solution.run_calculation.iterate(iter_count=block_iterations)
                after_iteration = self._current_solver_iteration(solver)
                actual_block = after_iteration - before_iteration
                actual_completed = after_iteration - initial_iteration
                if actual_block != block_iterations:
                    protocol_errors.append(
                        f"requested_{block_iterations}_actual_{actual_block}_at_solver_iteration_{before_iteration}"
                    )
                final = self._compute_coefficients(solver, f"{label}_stability_{requested}")
                samples.append(
                    {
                        "requested_iterations": requested,
                        "actual_iterations": actual_completed,
                        "actual_block_iterations": actual_block,
                        "solver_iteration": after_iteration,
                        "cd": final.cd,
                        "cl": final.cl,
                        "source": final.source,
                    }
                )
                state = force_stability_state(
                    samples,
                    relative_tolerance=relative_tolerance,
                    consecutive_blocks=consecutive_blocks,
                )
                state.update(
                    {
                        "iterations_per_block": block_iterations,
                        "maximum_requested_iterations": max_iterations,
                        "requested_iterations_completed": requested,
                        "actual_iterations_completed": actual_completed,
                        "convergence_checks_temporarily_disabled": True,
                        "iteration_protocol_errors": list(protocol_errors),
                        "coefficient_drift_from_initial": force_stability_drift(samples),
                    }
                )
                if state["stable"] and not protocol_errors:
                    return state, representative_coefficient(state, final)
        finally:
            for name, enabled_before in convergence_checks.items():
                residual_equations[name].check_convergence = enabled_before
            restored = {
                name: bool(residual_equations[name].check_convergence())
                for name in convergence_checks
            }
            if restored != convergence_checks:
                raise RuntimeError(
                    "Fluent residual convergence checks were not restored after force stability audit: "
                    f"expected={convergence_checks}, actual={restored}"
                )
        state = force_stability_state(
            samples,
            relative_tolerance=relative_tolerance,
            consecutive_blocks=consecutive_blocks,
        )
        state.update(
            {
                "iterations_per_block": block_iterations,
                "maximum_requested_iterations": max_iterations,
                "requested_iterations_completed": max_iterations,
                "actual_iterations_completed": actual_completed,
                "convergence_checks_temporarily_disabled": True,
                "iteration_protocol_errors": list(protocol_errors),
                "errors": ["force_coefficients_not_stable", *protocol_errors],
                "coefficient_drift_from_initial": force_stability_drift(samples),
            }
        )
        return state, representative_coefficient(state, final)

    @staticmethod
    def _current_solver_iteration(solver: Any) -> int:
        evaluator = getattr(getattr(solver, "scheme_eval", None), "scheme_eval", None)
        if evaluator is None:
            scheme = getattr(solver, "scheme", None)
            evaluator = getattr(scheme, "eval", None) or getattr(scheme, "scheme_eval", None)
        if evaluator is None:
            raise RuntimeError("Fluent solver iteration counter is unavailable")
        value = evaluator("(get-current-iteration)")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Fluent returned an invalid solver iteration counter: {value!r}") from exc

    def _promote_staged_exports(self, staged_export_dir: Path) -> None:
        official = self.output_dir / "exports" / "optimized"
        if official.exists():
            shutil.rmtree(official)
        shutil.copytree(staged_export_dir, official)
        manifest_path = official / "export_manifest.json"
        if manifest_path.exists():
            manifest = load_json(manifest_path)
            staged_prefix = str(staged_export_dir.resolve())
            official_prefix = str(official.resolve())

            def promoted_path(value: Any) -> Any:
                if isinstance(value, str) and value.startswith(staged_prefix):
                    return official_prefix + value[len(staged_prefix) :]
                if isinstance(value, dict):
                    return {key: promoted_path(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [promoted_path(item) for item in value]
                return value

            write_json(manifest_path, promoted_path(manifest))

    def _render_plan(self) -> dict[str, Any]:
        return {
            "context": self.context,
            "advanced_settings": deep_get(self.cfg, "advanced_settings", {}),
            "optimizer": deep_get(self.cfg, "optimizer", {}),
            "exports": deep_get(self.cfg, "exports", {}),
            "retry_profiles": self.cfg.get("retry_profiles", []),
            "optimization_run": deep_get(self.cfg, "optimization_run", {}),
            "shape_guard": self.shape_guard,
            "resolved_attempt_profiles": optimization_attempt_profiles(self.cfg),
        }

    def _seed_optimizer_context(self) -> None:
        optimizer_defaults = deep_get(self.cfg, "optimizer", {})
        design_tool_defaults = deep_get(self.cfg, "advanced_settings.design_tool", {})
        self.context.setdefault("minimum_allowed_cl", 0.0)
        self.context["x_control_points"] = int(design_tool_defaults.get("x_control_points", optimizer_defaults.get("x_control_points", 24)))
        self.context["y_control_points"] = int(design_tool_defaults.get("y_control_points", optimizer_defaults.get("y_control_points", 8)))
        self.context["design_iterations"] = int(optimizer_defaults.get("design_iterations", deep_get(self.cfg, "iterations.design", 5)))
        self.context["flow_iterations"] = int(optimizer_defaults.get("flow_iterations", deep_get(self.cfg, "iterations.flow", 1000)))
        self.context["adjoint_iterations"] = int(optimizer_defaults.get("adjoint_iterations", deep_get(self.cfg, "iterations.adjoint", 297)))

    def _run_optimizer_with_retries(self, solver: Any, baseline: Coefficients) -> dict[str, Any]:
        profiles = optimization_attempt_profiles(self.cfg)
        attempts = []
        for index, profile in enumerate(profiles):
            self.context["design_iterations"] = int(profile.get("design_iterations", deep_get(self.cfg, "iterations.design", 5)))
            self.context["flow_iterations"] = int(profile.get("flow_iterations", deep_get(self.cfg, "iterations.flow", 1000)))
            self.context["adjoint_iterations"] = int(profile.get("adjoint_iterations", deep_get(self.cfg, "iterations.adjoint", 297)))
            self.context["drag_step_percent"] = float(profile.get("drag_step_percent", deep_get(self.cfg, "advanced_settings.optimizer.drag_step_percent", -0.05)))
            self.context["post_design_flow_iterations"] = int(
                profile.get("post_design_flow_iterations", deep_get(self.cfg, "advanced_settings.optimizer.post_design_flow_iterations", 0))
            )
            self.context["lift_force_report_to_observable_factor"] = float(
                profile.get(
                    "lift_force_report_to_observable_factor",
                    deep_get(self.cfg, "advanced_settings.optimizer.lift_force_report_to_observable_factor", 1.0),
                )
            )
            self.context["minimum_allowed_lift_force"] = self._minimum_allowed_lift_force(baseline.cl)
            if "x_control_points" in profile:
                self.context["x_control_points"] = int(profile["x_control_points"])
            if "y_control_points" in profile:
                self.context["y_control_points"] = int(profile["y_control_points"])
            if index > 0:
                self._setup_design_tool(solver)
            self._configure_and_run_optimizer(solver)
            self._post_design_flow_update(solver, profile)
            final = self._compute_coefficients(solver, f"attempt_{index}_final")
            transcript_audit = transcript_morphing_audit(self.transcript_path)
            passed = self._passes_completion_gate(baseline, final)
            attempts.append(
                {
                    "index": index,
                    "profile": profile,
                    "final": final.__dict__,
                    "transcript_audit": transcript_audit,
                    "passed_completion_gate": passed,
                }
            )
            if transcript_audit["invalid_morphing"]:
                return {
                    "status": "FAIL_TRANSCRIPT_NEGATIVE_VOLUME",
                    "baseline": baseline.__dict__,
                    "final": final.__dict__,
                    "attempts": attempts,
                    "strict_audit_failure": "Fluent transcript reported negative cell-volume during morphing.",
                }
            if passed:
                return {"status": "PASS", "baseline": baseline.__dict__, "final": final.__dict__, "attempts": attempts}
        return {"status": "FAIL_COMPLETION_GATE", "baseline": baseline.__dict__, "attempts": attempts}

    def _post_design_flow_update(self, solver: Any, profile: dict[str, Any]) -> None:
        iterations = int(
            profile.get(
                "post_design_flow_iterations",
                self.context.get("post_design_flow_iterations", deep_get(self.cfg, "advanced_settings.optimizer.post_design_flow_iterations", 0)),
            )
        )
        if iterations <= 0:
            return
        self._settings_step(
            f"post-design flow iterate {iterations}",
            lambda: solver.settings.solution.run_calculation.iterate(iter_count=iterations),
            required=True,
        )

    def _save_solution_exports(self, solver: Any, label: str, *, include_sensitivity: bool, export_dir: Path | None = None) -> None:
        export_dir = export_dir or (self.output_dir / "exports" / label)
        export_dir.mkdir(parents=True, exist_ok=True)
        case_path = export_dir / f"{label}.cas.h5"
        data_path = export_dir / f"{label}.dat.h5"
        tecplot_path = export_dir / f"{label}.plt"
        ensight_prefix = export_dir / f"{label}_ensight"
        required = bool(deep_get(self.cfg, "exports.required", True))
        flow_variables = [str(item) for item in deep_get(self.cfg, "exports.ensight.variables", ["pressure", "velocity-magnitude", "x-velocity", "y-velocity"])]
        self._export_step(label, "case", case_path, lambda: solver.settings.file.write_case(file_name=str(case_path)), required=required)
        self._export_step(label, "data", data_path, lambda: solver.settings.file.write_data(file_name=str(data_path)), required=required)
        tecplot_metadata = self._export_airfoil_tecplot(solver, label, tecplot_path, required=required)
        self._export_ensight_family(
            solver,
            label,
            "ensight_flow_pressure",
            ensight_prefix,
            flow_variables,
            require_all_variables=True,
            required=required,
        )
        if include_sensitivity:
            sensitivity_path = export_dir / f"{label}_shape_sensitivity.sens"
            self._export_with_tui_candidates(
                label,
                "shape_sensitivity",
                [sensitivity_path],
                [
                    f'/adjoint/design-tool/objectives/manage/export-sensitivities "{sensitivity_path}"',
                ],
                required=required,
            )
            if bool(deep_get(self.cfg, "exports.ensight.export_sensitivity", True)):
                sensitivity_ensight = export_dir / f"{label}_sensitivity_ensight"
                sensitivity_variables = [str(item) for item in deep_get(self.cfg, "exports.ensight.sensitivity_variables", ["shape-sensitivity", "surface-sensitivity"])]
                sensitivity_required = required and bool(deep_get(self.cfg, "exports.ensight.sensitivity_required", False))
                self._export_ensight_family(
                    solver,
                    label,
                    "ensight_sensitivity",
                    sensitivity_ensight,
                    sensitivity_variables,
                    require_all_variables=False,
                    required=sensitivity_required,
                    optional_unavailable_if_disallowed=True,
                )
            else:
                self.exports.append({"label": label, "kind": "ensight_sensitivity", "status": "SKIP", "reason": "disabled by exports.ensight.export_sensitivity"})
                self.commands.append({"label": f"export {label} ensight_sensitivity", "kind": "export", "status": "SKIP", "reason": "disabled by config"})
        manifest = {
            "label": label,
            "case": str(case_path),
            "data": str(data_path),
            "tecplot": str(tecplot_path),
            "tecplot_surface_only": bool(tecplot_metadata.get("surface_only")),
            "surface_only": bool(tecplot_metadata.get("surface_only")),
            "surfaces": tecplot_metadata.get("surfaces", []),
            "variables": tecplot_metadata.get("variables", []),
            "tecplot_node_count": tecplot_metadata.get("node_count"),
            "tecplot_element_count": tecplot_metadata.get("element_count"),
            "ensight_prefix": str(ensight_prefix),
            "ensight_case": str(ensight_prefix.with_suffix(".case")),
            "ensight_files": [str(path) for path in ensight_family_files(ensight_prefix)],
            "includes_sensitivity": include_sensitivity,
        }
        if include_sensitivity:
            sensitivity_ensight = export_dir / f"{label}_sensitivity_ensight"
            manifest["sensitivity_ensight_case"] = str(sensitivity_ensight.with_suffix(".case"))
            manifest["sensitivity_ensight_files"] = [str(path) for path in ensight_family_files(sensitivity_ensight)]
        write_json(export_dir / "export_manifest.json", manifest)

    def _export_airfoil_tecplot(self, solver: Any, label: str, path: Path, *, required: bool) -> dict[str, Any]:
        wall_zone = str(deep_get(self.cfg, "advanced_settings.solver.wall_zone", "airfoil"))
        metadata: dict[str, Any] = {
            "surface_only": True,
            "surfaces": [wall_zone],
            "variables": [],
            "node_count": None,
            "element_count": None,
        }
        try:
            geometry = self._surface_geometry_snapshot(solver)
            if geometry.get("status") != "PASS":
                raise RuntimeError(f"Airfoil surface geometry is invalid: {geometry.get('errors', [])}")
            metadata["node_count"] = int(geometry["vertex_count"])
            metadata["element_count"] = int(geometry["edge_count"])
            solver.settings.file.export.tecplot(
                file_name=str(path),
                surfaces=[wall_zone],
                cell_func_domain_export=[],
            )
            if not path.exists() or path.stat().st_size <= 0:
                raise RuntimeError(f"Surface Tecplot export did not create {path}")
            if metadata["node_count"] <= 0 or metadata["element_count"] <= 0:
                raise RuntimeError("Surface Tecplot export has invalid wall node/element counts")
            record = {"label": label, "kind": "tecplot", "path": str(path), "status": "PASS", **metadata}
            self.exports.append(record)
            self.commands.append({"label": f"export {label} tecplot", "kind": "export", "status": "PASS", **metadata})
            return metadata
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.exports.append({"label": label, "kind": "tecplot", "path": str(path), "status": "FAIL", "error": error, **metadata})
            self.failures.append({"label": f"export {label} tecplot", "error": error})
            if required:
                raise RuntimeError(f"Required airfoil-only Tecplot export failed: {error}") from exc
            return metadata

    def _export_step(self, label: str, kind: str, expected_path: Path, action: Any, *, required: bool) -> None:
        try:
            action()
            exists = expected_path.exists() and expected_path.stat().st_size > 0
            record = {"label": label, "kind": kind, "path": str(expected_path), "status": "PASS" if exists else "MISSING"}
            self.exports.append(record)
            self.commands.append({"label": f"export {label} {kind}", "kind": "export", "status": record["status"], "path": str(expected_path)})
            if required and not exists:
                raise RuntimeError(f"Export did not create {expected_path}")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.exports.append({"label": label, "kind": kind, "path": str(expected_path), "status": "FAIL", "error": error})
            self.failures.append({"label": f"export {label} {kind}", "error": error})
            if required:
                raise RuntimeError(f"Required export failed: {label} {kind}: {error}") from exc

    def _export_with_tui_candidates(
        self,
        label: str,
        kind: str,
        expected_paths: list[Path],
        commands: list[str],
        *,
        required: bool,
    ) -> None:
        errors = []
        for command in commands:
            try:
                if self.transcript_path:
                    with self.transcript_path.open("a", encoding="utf-8", errors="ignore") as handle:
                        handle.write(f"\n[Codex export] {label} {kind}: {command}\n")
                # Fluent export TUI syntax differs slightly between releases; keep candidates small and verify files.
                self._current_solver.execute_tui(command)  # type: ignore[attr-defined]
                if any(path.exists() and path.stat().st_size > 0 for path in expected_paths):
                    self.exports.append({"label": label, "kind": kind, "paths": [str(p) for p in expected_paths], "status": "PASS", "command": command})
                    self.commands.append({"label": f"export {label} {kind}", "kind": "export", "status": "PASS", "command": command})
                    return
                errors.append(f"{command}: command ran but expected files were not found")
            except Exception as exc:
                errors.append(f"{command}: {type(exc).__name__}: {exc}")
        error = " | ".join(errors)
        self.exports.append({"label": label, "kind": kind, "paths": [str(p) for p in expected_paths], "status": "FAIL", "error": error})
        self.commands.append({"label": f"export {label} {kind}", "kind": "export", "status": "FAIL", "error": error})
        if required:
            self.failures.append({"label": f"export {label} {kind}", "error": error})
            raise RuntimeError(f"Required export failed: {label} {kind}: {error}")

    def _export_ensight_family(
        self,
        solver: Any,
        label: str,
        kind: str,
        prefix: Path,
        variables: list[str],
        *,
        require_all_variables: bool,
        required: bool,
        optional_unavailable_if_disallowed: bool = False,
    ) -> None:
        errors = []
        cell_zone = str(deep_get(self.cfg, "advanced_settings.solver.cell_zone", "fluid"))
        actions = [
            (
                "settings.file.export.ensight_gold",
                lambda: solver.settings.file.export.ensight_gold(
                    file_name=str(prefix),
                    cell_func_domain_export=variables,
                    binary_format=False,
                    cellzones=[cell_zone],
                    interior_zone_surfaces=[],
                    cell_centered=False,
                ),
            ),
            (
                "settings.file.export.ensight",
                lambda: solver.settings.file.export.ensight(
                    file_name=str(prefix),
                    cell_func_domain_export=variables,
                ),
            ),
        ]
        for action_label, action in actions:
            try:
                if self.transcript_path:
                    with self.transcript_path.open("a", encoding="utf-8", errors="ignore") as handle:
                        handle.write(f"\n[Codex export] {label} {kind}: {action_label} {prefix}\n")
                action()
                case_alias = ensure_ensight_case_alias(prefix)
                has_variables = ensight_case_has_variables(prefix, variables, require_all=require_all_variables)
                if case_alias and has_variables:
                    files = ensight_family_files(prefix)
                    self.exports.append(
                        {
                            "label": label,
                            "kind": kind,
                            "case": str(case_alias),
                            "files": [str(path) for path in files],
                            "variables": variables,
                            "status": "PASS",
                            "command": action_label,
                        }
                    )
                    self.commands.append({"label": f"export {label} {kind}", "kind": "export", "status": "PASS", "command": action_label})
                    return
                errors.append(f"{action_label}: exported files missing required variables {variables} or case file")
            except Exception as exc:
                errors.append(f"{action_label}: {type(exc).__name__}: {exc}")
        error = " | ".join(errors)
        status = export_failure_status(error, required=required, optional_unavailable_if_disallowed=optional_unavailable_if_disallowed)
        self.exports.append(
            {
                "label": label,
                "kind": kind,
                "case": str(prefix.with_suffix(".case")),
                "files": [str(path) for path in ensight_family_files(prefix)],
                "variables": variables,
                "status": status,
                "error": error,
            }
        )
        self.commands.append({"label": f"export {label} {kind}", "kind": "export", "status": status, "error": error})
        if status == "FAIL" and required:
            self.failures.append({"label": f"export {label} {kind}", "error": error})
            raise RuntimeError(f"Required export failed: {label} {kind}: {error}")

    def _apply_physics_settings(self, solver: Any) -> None:
        attempts = [
            (
                "set viscous model k-omega",
                lambda: self._set_first_setting_path(solver.settings.setup.models.viscous, ["model"], "k-omega"),
            ),
            (
                "set k-omega variant SST",
                lambda: self._set_first_setting_path(solver.settings.setup.models.viscous, ["k_omega_model"], "sst"),
            ),
            ("enable energy", lambda: setattr(solver.settings.setup.models.energy, "enabled", True)),
            (
                "set air density ideal gas",
                lambda: solver.settings.setup.materials.fluid["air"].density.option.set_state("ideal-gas"),
            ),
            (
                "set air viscosity sutherland",
                lambda: solver.settings.setup.materials.fluid["air"].viscosity.option.set_state("sutherland"),
            ),
        ]
        for label, action in attempts:
            self._settings_step(label, action, required=True)
        # Setting calls succeeding is insufficient evidence: read every
        # critical value back from the live Fluent session and stop before any
        # expensive iteration when the fingerprint differs.
        readback = collect_physics_readback(
            solver,
            inlet_zone=str(deep_get(self.cfg, "advanced_settings.solver.inlet_zone", "velocity_inlet")),
        )
        preliminary_expected = {
            "turbulence_intensity": float(self.context.get("turbulence_intensity", 0.01)),
            "velocity_m_s": float(self.context.get("velocity_m_s", 0.0)),
            "density_kg_m3": float(self.context.get("density_kg_m3", 0.0)),
            "reference_area_m2": float(self.context.get("reference_area_m2", 0.0)),
            "effective_chord_m": float(self.context.get("effective_chord_m", 0.0)),
        }
        # Reference and inlet values are applied by _apply_flow_settings, so
        # this first readback validates only the model/material subset.
        normalized = lambda value: str(value).strip().lower().replace("_", "-").replace(" ", "-")
        model_checks = {
            "energy": readback.get("energy_enabled") is True,
            "sst": "sst" in normalized(readback.get("viscous_model")),
            "ideal_gas": normalized(readback.get("density_model")) in {"ideal-gas", "idealgas"},
            "sutherland": "sutherland" in normalized(readback.get("viscosity_model")),
        }
        self.context["physics_model_readback"] = readback
        if not all(model_checks.values()):
            failed = [name for name, passed in model_checks.items() if not passed]
            raise RuntimeError(f"Fluent physics model readback failed: {', '.join(failed)}")

    def _collect_cfd_qualification(self, solver: Any, force_stability: dict[str, Any]) -> dict[str, Any]:
        errors: dict[str, str] = {}
        wall_zone = str(deep_get(self.cfg, "advanced_settings.solver.wall_zone", "airfoil"))
        inlet_zone = str(deep_get(self.cfg, "advanced_settings.solver.inlet_zone", "velocity_inlet"))
        outlet_zone = str(deep_get(self.cfg, "advanced_settings.solver.outlet_zone", "pressure_outlet"))
        try:
            wall_y_plus = collect_wall_y_plus(solver, wall_zone)
        except Exception as exc:
            wall_y_plus = []
            errors["wall_y_plus"] = f"{type(exc).__name__}: {exc}"
        try:
            wall_y_plus_distribution = collect_wall_y_plus_distribution(solver, wall_zone)
        except Exception as exc:
            wall_y_plus_distribution = {
                "status": "UNVERIFIED",
                "errors": [f"{type(exc).__name__}: {exc}"],
            }
            errors["wall_y_plus_distribution"] = wall_y_plus_distribution["errors"][0]
        try:
            mass_fluxes = collect_boundary_mass_fluxes(solver, [inlet_zone, outlet_zone])
        except Exception as exc:
            mass_fluxes = {}
            errors["mass_balance"] = f"{type(exc).__name__}: {exc}"
        try:
            physics = collect_physics_readback(solver, inlet_zone=inlet_zone)
        except Exception as exc:
            physics = {"readback_error": f"{type(exc).__name__}: {exc}"}
            errors["physics_readback"] = physics["readback_error"]
        try:
            residuals = collect_final_residuals(solver)
        except Exception as exc:
            residuals = {}
            errors["residuals"] = f"{type(exc).__name__}: {exc}"
        qualification = build_cfd_qualification(
            wall_y_plus=wall_y_plus,
            boundary_mass_fluxes=mass_fluxes,
            physics_readback=physics,
            expected_physics=self.context,
            residuals=residuals,
            residual_criteria=deep_get(self.cfg, "advanced_settings.residual_criteria", {}) or {},
            force_stability=force_stability,
            mesh=self.context.get("primary_mesh") or {},
            target_y_plus=float(self.context.get("target_y_plus", deep_get(self.cfg, "flow.target_y_plus", 1.0))),
            grid_convergence=deep_get(self.cfg, "cfd_qualification.grid_convergence", None),
            quick_validation=bool(self.context.get("quick_validation", False)),
        )
        qualification["collection_errors"] = errors
        qualification["boundary_zones"] = {"wall": wall_zone, "inlet": inlet_zone, "outlet": outlet_zone}
        qualification["wall_y_plus_distribution"] = wall_y_plus_distribution
        return qualification

    def _verify_full_physics_fingerprint(self, solver: Any) -> dict[str, Any]:
        """Fail before iteration unless live Fluent values match the request."""
        inlet_zone = str(deep_get(self.cfg, "advanced_settings.solver.inlet_zone", "velocity_inlet"))
        readback = collect_physics_readback(solver, inlet_zone=inlet_zone)
        evidence = physics_readback_qualification(readback, self.context)
        self.context["physics_fingerprint"] = copy.deepcopy(evidence)
        if evidence.get("status") != "PASS":
            raise RuntimeError(
                "Fluent physics fingerprint mismatch: " + ", ".join(evidence.get("failures") or ["unknown"])
            )
        return evidence

    def _run_checkpoint_aa_calibration(
        self,
        pyfluent: Any,
        case_path: Path,
        data_path: Path,
        *,
        repeat_count: int,
        qualification_repeats: int | None = None,
    ) -> dict[str, Any]:
        if repeat_count < 1:
            raise ValueError("A/A repeat_count must be positive")
        records: list[dict[str, Any]] = []
        cd_values: list[float] = []
        aa_root = self.output_dir / "aa_calibration"
        aa_root.mkdir(parents=True, exist_ok=True)
        for index in range(1, repeat_count + 1):
            try:
                self._check_hard_deadline(f"before A/A reload {index}")
            except TimeoutError as exc:
                records.append({"index": index, "status": "TIMEOUT", "error": str(exc), "fresh_fluent_session": False})
                break
            solver = None
            transcript = aa_root / f"reload_{index:02d}.trn"
            try:
                solver = self._launch_solver(pyfluent, aa_root / f"reload_{index:02d}")
                solver.transcript.start(file_name=str(transcript))
                solver.settings.file.read_case(file_name=str(case_path))
                solver.settings.file.read_data(file_name=str(data_path))
                # Required physics/boundary/reference values are deliberately
                # re-applied and later read back in every independent reload.
                # This prevents a checkpoint with stale implicit defaults from
                # acquiring production qualification.
                self._apply_physics_settings(solver)
                self._apply_flow_settings(solver)
                self._verify_full_physics_fingerprint(solver)
                stability, coefficient = self._stabilize_force_coefficients(solver, f"aa_reload_{index:02d}")
                passed = stability.get("status") == "PASS" and isinstance(coefficient.cd, (int, float))
                if passed:
                    cd_values.append(float(coefficient.cd))
                reload_cfd = self._collect_cfd_qualification(solver, stability) if index == 1 else None
                records.append(
                    {
                        "index": index,
                        "status": "PASS" if passed else "FAIL",
                        "checkpoint": {"case": str(case_path), "data": str(data_path)},
                        "fresh_fluent_session": True,
                        "processor_count": int(self.cfg.get("processor_count", 1)),
                        "force_stability": stability,
                        "representative": coefficient.__dict__,
                        "cfd_qualification": reload_cfd,
                        "transcript": str(transcript),
                    }
                )
            except Exception as exc:
                records.append(
                    {
                        "index": index,
                        "status": "FAIL",
                        "fresh_fluent_session": True,
                        "error": f"{type(exc).__name__}: {exc}",
                        "transcript": str(transcript),
                    }
                )
                if isinstance(exc, TimeoutError):
                    break
            finally:
                self._close_solver(solver)
        result = aa_noise_floor(
            cd_values,
            engineering_floor=float(deep_get(self.cfg, "optimization_run.numerical_uncertainty.engineering_floor", 5.0e-4)),
            required_repeats=int(qualification_repeats or repeat_count),
        )
        result.update(
            {
                "source": "independent_checkpoint_reloads",
                "checkpoint": {"case": str(case_path), "data": str(data_path)},
                "records": records,
                "same_stabilization_protocol_as_baseline_and_candidate": True,
            }
        )
        write_json(aa_root / "aa_calibration.json", result)
        return result

    def _check_hard_deadline(self, stage: str) -> None:
        deadline = self.context.get("hard_deadline_monotonic")
        if isinstance(deadline, (int, float)) and time.monotonic() >= float(deadline):
            raise TimeoutError(f"hard validation deadline exceeded at {stage}")

    def _settings_step(self, label: str, action: Any, *, required: bool = False) -> Any:
        try:
            result = action()
            self.commands.append({"label": label, "kind": "settings", "status": "PASS"})
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.failures.append({"label": label, "error": error})
            self.commands.append({"label": label, "kind": "settings", "status": "FAIL", "error": error})
            if required:
                raise RuntimeError(f"Required Fluent settings step failed: {label}: {error}") from exc
            return None

    def _apply_flow_settings(self, solver: Any) -> None:
        topology = str(deep_get(self.cfg, "advanced_settings.solver.boundary_topology", "")).lower()
        if topology == "velocity-inlet-pressure-outlet":
            self._apply_cgrid_velocity_inlet_outlet(solver)
        else:
            farfield_zone = str(deep_get(self.cfg, "advanced_settings.solver.farfield_zone", "farfield"))
            bc = solver.settings.setup.boundary_conditions.pressure_far_field[farfield_zone]
            self._settings_step("set farfield Mach", lambda: setattr(bc.momentum.mach_number, "value", float(self.context["mach"])), required=True)
            self._settings_step("set farfield flow direction x", lambda: setattr(bc.momentum.flow_direction[0], "value", float(self.context["drag_x"])), required=True)
            self._settings_step("set farfield flow direction y", lambda: setattr(bc.momentum.flow_direction[1], "value", float(self.context["drag_y"])), required=True)
            self._settings_step("set farfield static temperature", lambda: setattr(bc.thermal.temperature, "value", float(self.context["temperature_k"])), required=True)
            self._settings_step("set farfield turbulence intensity", lambda: setattr(bc.turbulence, "turbulent_intensity", float(self.context["turbulence_intensity"])), required=True)
        self._settings_step("set operating pressure", lambda: setattr(solver.settings.setup.general.operating_conditions, "operating_pressure", float(self.context["static_pressure_pa"])), required=True)
        refs = solver.settings.setup.reference_values
        self._settings_step("set reference velocity", lambda: setattr(refs, "velocity", float(self.context["velocity_m_s"])), required=True)
        self._settings_step("set reference density", lambda: setattr(refs, "density", float(self.context["density_kg_m3"])), required=True)
        self._settings_step("set reference area", lambda: setattr(refs, "area", float(self.context["reference_area_m2"])), required=True)
        self._settings_step(
            "set reference length",
            lambda: setattr(refs, "length", float(self.context.get("effective_chord_m", deep_get(self.cfg, "flow.chord_m", 1.0)))),
            required=True,
        )
        self._settings_step("set reference temperature", lambda: setattr(refs, "temperature", float(self.context["temperature_k"])), required=True)
        self._settings_step("set reference viscosity", lambda: setattr(refs, "viscosity", float(self.context["dynamic_viscosity_pa_s"])), required=True)

    def _apply_cgrid_velocity_inlet_outlet(self, solver: Any) -> None:
        inlet_zone = str(deep_get(self.cfg, "advanced_settings.solver.inlet_zone", "velocity_inlet"))
        outlet_zone = str(deep_get(self.cfg, "advanced_settings.solver.outlet_zone", "pressure_outlet"))
        bcs = solver.settings.setup.boundary_conditions
        inlet = bcs.velocity_inlet[inlet_zone]
        outlet = bcs.pressure_outlet[outlet_zone]
        velocity_x = float(self.context["velocity_m_s"]) * float(self.context["drag_x"])
        velocity_y = float(self.context["velocity_m_s"]) * float(self.context["drag_y"])
        self._settings_step(
            "set velocity inlet specification components",
            lambda: setattr(inlet.momentum, "velocity_specification_method", "Components"),
            required=True,
        )
        self._settings_step(
            "set velocity inlet x component",
            lambda: self._set_first_setting_path(
                inlet,
                [
                    "momentum.velocity_components.0.value",
                ],
                velocity_x,
            ),
            required=True,
        )
        self._settings_step(
            "set velocity inlet y component",
            lambda: self._set_first_setting_path(
                inlet,
                [
                    "momentum.velocity_components.1.value",
                ],
                velocity_y,
            ),
            required=True,
        )
        self._settings_step(
            "set velocity inlet temperature",
            lambda: self._set_first_setting_path(inlet, ["thermal.temperature.value", "thermal.total_temperature.value"], float(self.context["temperature_k"])),
        )
        self._settings_step(
            "set velocity inlet turbulence intensity",
            lambda: self._set_first_setting_path(inlet, ["turbulence.turbulent_intensity", "turbulence.turbulent_intensity.value"], float(self.context["turbulence_intensity"])),
            required=True,
        )
        self._settings_step(
            "set pressure outlet gauge pressure",
            lambda: self._set_first_setting_path(outlet, ["momentum.gauge_pressure.value", "gauge_pressure.value"], 0.0),
            required=True,
        )

    @staticmethod
    def _set_first_setting_path(root: Any, dotted_paths: list[str], value: Any) -> None:
        errors = []
        for dotted in dotted_paths:
            try:
                current = root
                parts = dotted.split(".")
                for part in parts[:-1]:
                    current = current[int(part)] if part.isdigit() else getattr(current, part)
                last = parts[-1]
                if last.isdigit():
                    current[int(last)] = value
                else:
                    target = getattr(current, last)
                    if hasattr(target, "set_state"):
                        target.set_state(value)
                    else:
                        setattr(current, last, value)
                return
            except Exception as exc:
                errors.append(f"{dotted}: {type(exc).__name__}: {exc}")
        raise RuntimeError("No supported Fluent setting path worked. " + " | ".join(errors))

    def _apply_solution_settings(self, solver: Any) -> None:
        methods = solver.settings.solution.methods
        controls = solver.settings.solution.controls
        residuals = solver.settings.solution.monitor.residual.equations
        adv = deep_get(self.cfg, "advanced_settings", {})
        solution_methods = adv.get("solution_methods", {})
        solution_controls = adv.get("solution_controls", {})
        residual_criteria = adv.get("residual_criteria", {})
        coupling = solution_methods.get(
            "pressure_velocity_coupling",
            DEFAULT_PRESSURE_VELOCITY_COUPLING,
        ) or DEFAULT_PRESSURE_VELOCITY_COUPLING
        resolved_coupling = normalize_pressure_velocity_coupling(coupling)
        self._settings_step(
            "set pressure velocity coupling",
            lambda: setattr(methods.p_v_coupling, "flow_scheme", resolved_coupling),
            required=True,
        )
        flux_type = solution_methods.get("flux_type")
        if flux_type:
            self._apply_flux_type_setting(methods, str(flux_type))
        self._apply_spatial_discretization(methods, solution_methods)
        if "courant_number" in solution_controls:
            try:
                courant_active = bool(controls.courant_number.is_active())
            except Exception:
                courant_active = True
            if courant_active:
                self._settings_step("set courant number", lambda: setattr(controls, "courant_number", float(solution_controls["courant_number"])))
            else:
                self.commands.append({"label": "set courant number", "kind": "settings", "status": "SKIP", "reason": "inactive for current solver controls"})
        explicit_relaxation = solution_controls.get("explicit_relaxation", {})
        try:
            p_v_controls_active = bool(controls.p_v_controls.is_active())
        except Exception:
            p_v_controls_active = False
        if "pressure" in explicit_relaxation:
            if p_v_controls_active:
                target = controls.p_v_controls.explicit_pressure_under_relaxation
                self._settings_step(
                    "set explicit pressure relaxation",
                    lambda: target.set_state(float(explicit_relaxation["pressure"])),
                )
            else:
                self.commands.append({"label": "set explicit pressure relaxation", "kind": "settings", "status": "SKIP", "reason": "inactive for current coupling scheme"})
        if "momentum" in explicit_relaxation:
            if p_v_controls_active:
                target = controls.p_v_controls.explicit_momentum_under_relaxation
                self._settings_step(
                    "set explicit momentum relaxation",
                    lambda: target.set_state(float(explicit_relaxation["momentum"])),
                )
            else:
                self.commands.append({"label": "set explicit momentum relaxation", "kind": "settings", "status": "SKIP", "reason": "inactive for current coupling scheme"})
        under_relaxation = solution_controls.get("under_relaxation", {})
        if under_relaxation:
            if not controls.under_relaxation.is_active():
                raise RuntimeError("Configured equation under-relaxation is inactive for the selected coupling scheme")
            current_under_relaxation = dict(controls.under_relaxation.get_state())
            current_under_relaxation.update(
                normalize_under_relaxation_updates(under_relaxation, current_under_relaxation)
            )
            self._settings_step(
                "set equation under relaxation",
                lambda: controls.under_relaxation.set_state(current_under_relaxation),
                required=True,
            )
        limits = solution_controls.get("limits", {})
        if "max_turb_visc_ratio" in limits:
            self._settings_step(
                "set maximum turbulent viscosity ratio",
                lambda: setattr(controls.limits, "max_turb_visc_ratio", float(limits["max_turb_visc_ratio"])),
            )
        for fluent_name, value in residual_criteria.items():
            equation = fluent_name.replace("_", "-")
            self._settings_step(
                f"set residual criterion {equation}",
                lambda equation=equation, value=value: setattr(residuals[equation], "absolute_criteria", float(value)),
            )

    def _apply_spatial_discretization(self, methods: Any, solution_methods: dict[str, Any], *, order: str | None = None) -> None:
        gradient = self._normalize_fluent_option(solution_methods.get("gradient"))
        pressure = solution_methods.get("pressure")
        momentum = solution_methods.get("momentum")
        k = solution_methods.get("turbulent_kinetic_energy")
        omega = solution_methods.get("specific_dissipation_rate")
        if order == "first-order":
            momentum = "first-order-upwind"
            k = "first-order-upwind"
            omega = "first-order-upwind"
            pressure = solution_methods.get("first_order_pressure", "standard")
        spatial = methods.spatial_discretization
        state = spatial.get_state()
        scheme = dict(state.get("discretization_scheme", {}))
        updates = {
            "pressure": self._normalize_fluent_option(pressure),
            "mom": self._normalize_fluent_option(momentum),
            "k": self._normalize_fluent_option(k),
            "omega": self._normalize_fluent_option(omega),
        }
        scheme.update({key: value for key, value in updates.items() if value})
        new_state = {"discretization_scheme": scheme}
        if gradient:
            new_state["gradient_scheme"] = gradient
        self._settings_step(
            f"set spatial discretization {order or 'configured'}",
            lambda: spatial.set_state(new_state),
        )

    @staticmethod
    def _normalize_fluent_option(value: Any) -> str | None:
        if value in {None, ""}:
            return None
        return str(value).replace("least-squares", "least-square")

    def _apply_flux_type_setting(self, methods: Any, flux_type: str) -> None:
        normalized = flux_type.strip()
        try:
            state = methods.flux_type.get_state()
            if isinstance(state, dict) and "pbns_cases" in state:
                allowed = {"rhie-chow: momentum based", "rhie-chow: distance based"}
                if normalized.lower() not in allowed:
                    self.commands.append(
                        {
                            "label": "set flux type",
                            "kind": "settings",
                            "status": "SKIP",
                            "reason": f"{normalized} is not available for the pressure-based solver; keeping {state['pbns_cases'].get('flux_type')}",
                        }
                    )
                    return
                new_state = {"pbns_cases": {"flux_auto_select": False, "flux_type": normalized}}
                methods.flux_type.set_state(new_state)
                self.commands.append({"label": "set flux type", "kind": "settings", "status": "PASS", "value": normalized})
                return
            self._set_first_setting_path(methods, ["flux_type"], normalized)
            self.commands.append({"label": "set flux type", "kind": "settings", "status": "PASS", "value": normalized})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.failures.append({"label": "set flux type", "error": error})
            self.commands.append({"label": "set flux type", "kind": "settings", "status": "FAIL", "error": error})

    def _initialize_and_iterate_flow(self, solver: Any, iterations: int) -> None:
        initialization = str(deep_get(self.cfg, "advanced_settings.solution_controls.initialization", "standard")).lower()
        if initialization == "hybrid":
            self._settings_step("hybrid initialize", lambda: solver.settings.solution.initialization.hybrid_initialize(), required=True)
        else:
            topology = str(deep_get(self.cfg, "advanced_settings.solver.boundary_topology", "")).lower()
            default_zone = "velocity_inlet" if topology == "velocity-inlet-pressure-outlet" else "farfield"
            farfield_zone = str(deep_get(self.cfg, "advanced_settings.solver.inlet_zone" if topology == "velocity-inlet-pressure-outlet" else "advanced_settings.solver.farfield_zone", default_zone))
            init = solver.settings.solution.initialization
            self._settings_step(f"compute initialization defaults from {farfield_zone}", lambda: init.compute_defaults(), required=False)
            self._settings_step("standard initialize", lambda: init.standard_initialize(), required=True)
        ramp = deep_get(self.cfg, "advanced_settings.solution_controls.flow_ramp", {})
        first_order_iterations = max(0, min(iterations, int(ramp.get("first_order_iterations", 0)))) if isinstance(ramp, dict) else 0
        if first_order_iterations:
            methods = solver.settings.solution.methods
            solution_methods = deep_get(self.cfg, "advanced_settings.solution_methods", {})
            self._apply_spatial_discretization(methods, solution_methods, order="first-order")
            self._iterate_flow_budget(solver, first_order_iterations, label="first-order")
            self._apply_spatial_discretization(methods, solution_methods)
        remaining = iterations - first_order_iterations
        if remaining:
            self._iterate_flow_budget(solver, remaining, label="configured-order")

    def _iterate_interpolated_flow(self, solver: Any, iterations: int) -> None:
        """Relax an interpolated solution without erasing it through initialization."""
        ramp = deep_get(self.cfg, "advanced_settings.solution_controls.flow_ramp", {})
        configured = ramp.get("interpolation_first_order_iterations", ramp.get("first_order_iterations", 0)) if isinstance(ramp, dict) else 0
        first_order_iterations = max(0, min(iterations, int(configured)))
        if first_order_iterations:
            methods = solver.settings.solution.methods
            solution_methods = deep_get(self.cfg, "advanced_settings.solution_methods", {})
            self._apply_spatial_discretization(methods, solution_methods, order="first-order")
            self._iterate_flow_budget(solver, first_order_iterations, label="interpolated-first-order")
            self._apply_spatial_discretization(methods, solution_methods)
        remaining = iterations - first_order_iterations
        if remaining:
            self._iterate_flow_budget(solver, remaining, label="interpolated-configured-order")

    def _iterate_flow_budget(self, solver: Any, iterations: int, *, label: str) -> None:
        """Run flow iterations in deadline-aware blocks for long production meshes."""
        if "hard_deadline_monotonic" not in self.context:
            self._settings_step(
                f"flow iterate {label} {iterations}",
                lambda: solver.settings.solution.run_calculation.iterate(iter_count=iterations),
                required=True,
            )
            return
        criteria = deep_get(self.cfg, "advanced_settings.residual_criteria", {}) or {}
        block_size = max(1, int(deep_get(self.cfg, "advanced_settings.solution_controls.flow_iteration_block", 20)))
        completed = 0
        while completed < iterations:
            self._check_hard_deadline(f"before {label} flow block at {completed} iterations")
            block = min(block_size, iterations - completed)
            self._settings_step(
                f"flow iterate {label} block {completed + 1}-{completed + block}",
                lambda block=block: solver.settings.solution.run_calculation.iterate(iter_count=block),
                required=True,
            )
            completed += block
            residuals = collect_final_residuals(solver)
            check = residual_qualification(residuals, criteria)
            self.commands.append(
                {
                    "label": f"{label} residual check after {completed}",
                    "kind": "flow_convergence",
                    "status": check.get("status"),
                    "residuals": residuals,
                }
            )
            if check.get("status") == "PASS":
                break

    def _compute_coefficients(self, solver: Any, label: str) -> Coefficients:
        cd = self._projected_wall_coefficient(solver, f"{label}_drag_force", [self.context["drag_x"], self.context["drag_y"]])
        cl = self._projected_wall_coefficient(solver, f"{label}_lift_force", [self.context["lift_x"], self.context["lift_y"]])
        coeff = Coefficients(cd=cd, cl=cl, source=f"fluent_forces:{label}")
        write_json(self.output_dir / f"{label}_coefficients.json", coeff.__dict__)
        return coeff

    def _projected_wall_coefficient(self, solver: Any, label: str, direction: list[float]) -> float | None:
        wall_zone = str(deep_get(self.cfg, "advanced_settings.solver.wall_zone", "airfoil"))
        vector = [float(direction[0]), float(direction[1])]
        kwargs = {
            "option": "forces",
            "domain": "mixture",
            "wall_zones": [wall_zone],
            "direction_vector": vector,
            "momentum_center": [0.0, 0.0],
            "momentum_axis": [0.0, 0.0, 1.0],
            "pressure_coordinate": "x",
            "coordinate_value": 0.0,
        }
        result = self._settings_step(f"query {label}", lambda: solver.settings.results.report.get_forces(**kwargs))
        force = self._extract_total_force_along_direction(result)
        if force is not None:
            self.context[f"{label}_total_force"] = force
        numeric = self._extract_force_coefficient(result)
        if numeric is not None:
            write_json(self.output_dir / f"{label}.json", {"value": numeric, "raw": result})
            return numeric
        report_path = self.output_dir / "fluent" / f"{label}.txt"
        self._settings_step(
            f"write {label}",
            lambda: solver.settings.results.report.forces(**kwargs, write_to_file=True, file_name=str(report_path), append_data=False),
        )
        if report_path.exists():
            text = report_path.read_text(encoding="utf-8", errors="ignore")
            numeric = self._extract_force_coefficient_from_text(text)
            write_json(self.output_dir / f"{label}.json", {"value": numeric, "file": str(report_path), "text": text})
            return numeric
        return None

    @staticmethod
    def _extract_force_coefficient(value: Any) -> float | None:
        if isinstance(value, dict):
            along = value.get("net-along-direction", {})
            if isinstance(along, dict):
                for key in (
                    "net-total-force-coeff-along-direction",
                    "net-coeff-of-pressure-force-along-direction",
                    "net-coeff-of-viscous-force-long-direction",
                ):
                    if isinstance(along.get(key), (int, float)):
                        return float(along[key])
            for zone_data in value.values():
                if isinstance(zone_data, dict) and isinstance(zone_data.get("total-force-coeff-along-direction"), (int, float)):
                    return float(zone_data["total-force-coeff-along-direction"])
        return FluentAdjointRunner._extract_numeric_leaf(value)

    @staticmethod
    def _extract_total_force_along_direction(value: Any) -> float | None:
        if isinstance(value, dict):
            along = value.get("net-along-direction", {})
            if isinstance(along, dict) and isinstance(along.get("net-total-force-along-direction"), (int, float)):
                return float(along["net-total-force-along-direction"])
            for zone_data in value.values():
                if isinstance(zone_data, dict) and isinstance(zone_data.get("total-force-along-direction"), (int, float)):
                    return float(zone_data["total-force-along-direction"])
        return None

    @staticmethod
    def _extract_numeric_leaf(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            for item in value.values():
                found = FluentAdjointRunner._extract_numeric_leaf(item)
                if found is not None:
                    return found
        if isinstance(value, (list, tuple)):
            numbers = [float(item) for item in value if isinstance(item, (int, float))]
            if numbers:
                return numbers[-1]
            for item in value:
                found = FluentAdjointRunner._extract_numeric_leaf(item)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _extract_force_coefficient_from_text(text: str) -> float | None:
        patterns = [
            r"net-total-force-coeff-along-direction\s+([-+0-9.eE]+)\s*$",
            r"total-force-coeff-along-direction\s+([-+0-9.eE]+)\s*$",
            r"force coefficient.*?([-+0-9.eE]+)\s*$",
        ]
        for pattern in patterns:
            values = [float(match.group(1)) for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE)]
            if values:
                return values[-1]
        numbers = re.findall(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?", text)
        return float(numbers[-1]) if numbers else None

    def _setup_adjoint_observables(self, solver: Any) -> None:
        gb = solver.settings.design.gradient_based
        try:
            enable_active = bool(gb.enable.is_active())
        except Exception:
            enable_active = True
        if enable_active:
            self._settings_step("enable gradient based adjoint", lambda: gb.enable(), required=True)
        else:
            self.commands.append(
                {
                    "label": "enable gradient based adjoint",
                    "kind": "settings",
                    "status": "SKIP",
                    "reason": "gradient based adjoint is already active in the restored checkpoint",
                }
            )
        defs = gb.observables.definition
        self._create_force_observable(defs, "cd", [self.context["drag_x"], self.context["drag_y"]])
        self._create_force_observable(defs, "cl", [self.context["lift_x"], self.context["lift_y"]])
        method = str(deep_get(self.cfg, "advanced_settings.adjoint_solver.method", "balanced")).lower()
        method_command = {"balanced": gb.methods.balanced, "best_match": gb.methods.best_match, "default": gb.methods.default}.get(method, gb.methods.balanced)
        self._settings_step(f"set adjoint method {method}", method_command, required=True)

    def _create_force_observable(self, definitions: Any, name: str, vector: list[float]) -> None:
        existing = self._named_object_names(definitions)
        if name not in existing:
            self._settings_step(f"create observable {name}", lambda: definitions.create(name), required=True)
        else:
            self.commands.append(
                {
                    "label": f"reuse observable {name}",
                    "kind": "settings",
                    "status": "PASS",
                    "reason": "restored checkpoint already contains this observable; reuse avoids duplicate zero-valued definitions",
                }
            )
        observable = definitions[name]
        self._settings_step(f"set observable {name} type", lambda: setattr(observable, "type", "force"), required=True)
        self._settings_step(f"set observable {name} walls", lambda: setattr(observable, "walls", [str(deep_get(self.cfg, "advanced_settings.solver.wall_zone", "airfoil"))]), required=True)
        self._settings_step(f"set observable {name} vector", lambda: setattr(observable, "vector", [float(vector[0]), float(vector[1])]), required=True)

    def _setup_design_tool(self, solver: Any) -> None:
        gb = solver.settings.design.gradient_based
        tool = gb.design_tool
        wall_zone = str(deep_get(self.cfg, "advanced_settings.solver.wall_zone", "airfoil"))
        thickness_resolution = resolve_thickness_constraint(
            deep_get(self.cfg, "advanced_settings.design_tool.thickness_constraint", {}) or {}
        )
        morpher_method = self.context.get("morpher_method", deep_get(self.cfg, "advanced_settings.design_tool.morpher.method", None))
        use_fluent_default = isinstance(morpher_method, str) and morpher_method.lower() == "fluent-default"
        if morpher_method and not use_fluent_default:
            self._settings_step("set design morpher method", lambda: setattr(tool.morpher, "method", str(morpher_method)), required=True)
        else:
            self.commands.append(
                {
                    "label": "preserve Fluent default design morpher method",
                    "kind": "settings",
                    "status": "PASS",
                    "configured": morpher_method or "fluent-default",
                }
            )
        actual_morpher = self._setting_state(tool.morpher.method)
        morpher_resolution = {
            "configured": morpher_method,
            "source": "fluent-default" if use_fluent_default or not morpher_method else "configured_override",
            "actual": str(actual_morpher),
        }
        if str(actual_morpher).lower() == "polynomials":
            polynomial_cfg = deep_get(self.cfg, "advanced_settings.design_tool.morpher.polynomials", {}) or {}
            constraint_method = str(
                deep_get(self.cfg, "advanced_settings.design_tool.morpher.constraint_method", "standard")
            ).lower()
            if constraint_method not in {"standard", "enhanced"}:
                raise ValueError("polynomial constraint_method must be standard or enhanced")
            standard_preconditioning = float(polynomial_cfg.get("preconditioning_standard", 10.0))
            enhanced_preconditioning = float(polynomial_cfg.get("preconditioning_enhanced", 10.0))
            mask_shape_sensitivity = bool(polynomial_cfg.get("mask_shape_sensitivity", True))
            freeform = tool.morpher.numerics.polynomials.freeform_motions
            self._settings_step(
                "select standard polynomial constraint numerics",
                lambda: setattr(tool.morpher, "constraint_method", "standard"),
                required=True,
            )
            self._settings_step(
                "set polynomial standard preconditioning",
                lambda: setattr(freeform, "preconditioning_standard", standard_preconditioning),
                required=True,
            )
            standard_readback = float(self._setting_state(freeform.preconditioning_standard))
            polynomial_resolution = {
                "constraint_method": "standard",
                "preconditioning_standard": standard_readback,
                "preconditioning_enhanced": None,
                "mask_shape_sensitivity": None,
                "solving_primary_morpher": None,
            }
            if constraint_method == "enhanced":
                self._settings_step(
                    "select enhanced polynomial constraint numerics",
                    lambda: setattr(tool.morpher, "constraint_method", "enhanced"),
                    required=True,
                )
                self._settings_step(
                    "enable polynomial primary morpher",
                    lambda: setattr(freeform, "solving_primary_morpher", True),
                    required=True,
                )
                self._settings_step(
                    "set polynomial shape sensitivity mask",
                    lambda: setattr(freeform, "mask_shape_sensitivity", mask_shape_sensitivity),
                    required=True,
                )
                self._settings_step(
                    "set polynomial enhanced preconditioning",
                    lambda: setattr(freeform, "preconditioning_enhanced", enhanced_preconditioning),
                    required=True,
                )
                enhanced_state = self._setting_state(freeform)
                polynomial_resolution.update(
                    {
                        "constraint_method": str(self._setting_state(tool.morpher.constraint_method)),
                        "preconditioning_enhanced": float(self._setting_state(freeform.preconditioning_enhanced)),
                        "mask_shape_sensitivity": bool(enhanced_state.get("mask_shape_sensitivity")),
                        "solving_primary_morpher": bool(enhanced_state.get("solving_primary_morpher")),
                    }
                )
            morpher_resolution["polynomial_numerics"] = polynomial_resolution
        if str(actual_morpher).lower() == "radial-basis-function":
            rbf_cfg = resolve_rbf_numerics(
                deep_get(self.cfg, "advanced_settings.design_tool.morpher.rbf", {}) or {}
            )
            freeform = tool.morpher.numerics.rbf.freeform_motions
            linear_solver = freeform.linear_solver
            self._settings_step(
                "set RBF maximum main iterations",
                lambda: setattr(freeform, "max_iterations", rbf_cfg["max_iterations"]),
                required=True,
            )
            self._settings_step(
                "set RBF linear solver tolerance",
                lambda: setattr(linear_solver, "tolerance", rbf_cfg["linear_solver_tolerance"]),
                required=True,
            )
            self._settings_step(
                "set RBF maximum subiterations",
                lambda: setattr(linear_solver, "max_subiteration", rbf_cfg["max_subiteration"]),
                required=True,
            )
            self._settings_step(
                "set RBF number of modes",
                lambda: setattr(linear_solver, "number_of_modes", rbf_cfg["number_of_modes"]),
                required=True,
            )
            rbf_readback = self._setting_state(tool.morpher.numerics.rbf)
            actual_freeform = (rbf_readback or {}).get("freeform_motions", {})
            actual_linear = actual_freeform.get("linear_solver", {})
            expected_pairs = (
                ("max_iterations", actual_freeform.get("max_iterations")),
                ("linear_solver_tolerance", actual_linear.get("tolerance")),
                ("max_subiteration", actual_linear.get("max_subiteration")),
                ("number_of_modes", actual_linear.get("number_of_modes")),
            )
            mismatches = [
                f"{name}={actual!r}, expected {rbf_cfg[name]!r}"
                for name, actual in expected_pairs
                if not math.isclose(float(actual), float(rbf_cfg[name]), rel_tol=1.0e-10, abs_tol=1.0e-15)
            ]
            if mismatches:
                raise RuntimeError("RBF numerics read-back failed: " + "; ".join(mismatches))
            morpher_resolution["rbf_numerics"] = rbf_readback
        self.runtime_resolution["morpher"] = morpher_resolution
        if self._active_attempt_runtime is not None:
            self._active_attempt_runtime["morpher"] = copy.deepcopy(morpher_resolution)
        self._settings_step("set design modifiable zones", lambda: setattr(tool.region, "modifiable_zones", [wall_zone]), required=True)
        self._settings_step("set design region type", lambda: setattr(tool.region, "region_type", "cartesian"))
        self._settings_step("get design region bounds", lambda: tool.region.get_bounds())
        self._set_control_points_if_active(tool)
        definitions = tool.design_conditions.definition
        anchor_config = deep_get(self.cfg, "advanced_settings.design_tool.shape_anchors", {}) or {}
        anchor_resolution = resolve_shape_anchor_ranges(self._baseline_geometry(), anchor_config)
        if anchor_resolution.get("enabled"):
            clip_surfaces = solver.settings.results.surfaces.iso_clip
            clip_specs = (
                (anchor_resolution["surface_names"][0], anchor_resolution["leading_edge_range"]),
                (anchor_resolution["surface_names"][1], anchor_resolution["trailing_edge_range"]),
            )
            for anchor_name, value_range in clip_specs:
                self._delete_named_if_present(clip_surfaces, anchor_name, f"design clip surface {anchor_name}")
                self._settings_step(f"create design clip surface {anchor_name}", lambda name=anchor_name: clip_surfaces.create(name), required=True)
                clip = clip_surfaces[anchor_name]
                self._settings_step(f"set design clip field {anchor_name}", lambda clip=clip: setattr(clip, "field", "x-coordinate"), required=True)
                self._settings_step(f"set design clip source {anchor_name}", lambda clip=clip: setattr(clip, "surfaces", [wall_zone]), required=True)
                self._settings_step(f"compute design clip range {anchor_name}", lambda clip=clip: clip.range.compute(), required=True)
                self._settings_step(
                    f"set design clip minimum {anchor_name}",
                    lambda clip=clip, value=float(value_range[0]): setattr(clip.range, "minimum", value),
                    required=True,
                )
                self._settings_step(
                    f"set design clip maximum {anchor_name}",
                    lambda clip=clip, value=float(value_range[1]): setattr(clip.range, "maximum", value),
                    required=True,
                )
                anchor_resolution.setdefault("clip_surface_states", {})[anchor_name] = self._setting_state(clip)
        applied_conditions: list[str] = []
        self.commands.append(
            {
                "label": "use direct post-design thickness geometry audit",
                "kind": "settings",
                "status": "PASS" if thickness_resolution["enabled"] else "SKIP",
                "reason": "Candidate and baseline wall coordinates are compared section-by-section; no surface mesh is imported.",
            }
        )
        if anchor_resolution.get("enabled"):
            fixed_condition_name = "leading_trailing_edge_anchors"
            self._delete_named_if_present(definitions, fixed_condition_name, "leading/trailing edge fixed-wall condition")
            self._settings_step("create leading/trailing edge fixed-wall condition", lambda: definitions.create(fixed_condition_name), required=True)
            fixed_condition = definitions[fixed_condition_name]
            self._settings_step(
                "set leading/trailing edge fixed-wall condition type",
                lambda: setattr(fixed_condition, "type", "fixed-walls-constraint"),
                required=True,
            )
            self._settings_step(
                "set leading/trailing edge fixed-wall surfaces",
                lambda: setattr(fixed_condition, "surfaces", list(anchor_resolution["surface_names"])),
                required=True,
            )
            fixed_state = self._setting_state(fixed_condition)
            if set(fixed_state.get("surfaces") or []) != set(anchor_resolution["surface_names"]):
                raise ShapeAnchorSetupError(f"fixed-wall anchor read-back is incomplete: {fixed_state!r}")
            anchor_resolution["fixed_condition_name"] = fixed_condition_name
            anchor_resolution["fixed_condition_state"] = fixed_state
            applied_conditions.append(fixed_condition_name)
        self.runtime_resolution["shape_anchors"] = copy.deepcopy(anchor_resolution)
        self.runtime_resolution["thickness_constraint"] = copy.deepcopy(thickness_resolution)
        if self._active_attempt_runtime is not None:
            self._active_attempt_runtime["shape_anchors"] = copy.deepcopy(anchor_resolution)
            self._active_attempt_runtime["thickness_constraint"] = copy.deepcopy(thickness_resolution)
        self._settings_step(
            "apply thickness and anchor design conditions",
            lambda: setattr(tool.design_conditions.selection, "applied_conditions", applied_conditions),
            required=True,
        )

    def _set_control_points_if_active(self, tool: Any) -> None:
        try:
            x_active = bool(tool.region.cartesian.conditions.x.points.is_active())
            y_active = bool(tool.region.cartesian.conditions.y.points.is_active())
        except Exception:
            x_active = y_active = True
        if x_active:
            self._settings_step("set x control points", lambda: setattr(tool.region.cartesian.conditions.x, "points", int(self.context["x_control_points"])), required=True)
        else:
            self.commands.append({"label": "set x control points", "kind": "settings", "status": "SKIP", "reason": "inactive for current morpher method"})
        if y_active:
            self._settings_step("set y control points", lambda: setattr(tool.region.cartesian.conditions.y, "points", int(self.context["y_control_points"])), required=True)
        else:
            self.commands.append({"label": "set y control points", "kind": "settings", "status": "SKIP", "reason": "inactive for current morpher method"})
        motion = str(self.cfg.get("control_point_motion", "")).strip().lower()
        if motion not in {"x-only", "y-only", "xy"}:
            raise ValueError("control_point_motion must be x-only, y-only, or xy")
        requested_motion = {"x": motion in {"x-only", "xy"}, "y": motion in {"y-only", "xy"}}
        self._settings_step("set x control-point motion", lambda: setattr(tool.region.cartesian.conditions.x, "motion_enabled", requested_motion["x"]), required=True)
        self._settings_step("set y control-point motion", lambda: setattr(tool.region.cartesian.conditions.y, "motion_enabled", requested_motion["y"]), required=True)
        actual_motion = {
            "x": bool(self._setting_state(tool.region.cartesian.conditions.x.motion_enabled)),
            "y": bool(self._setting_state(tool.region.cartesian.conditions.y.motion_enabled)),
        }
        if actual_motion != requested_motion:
            raise RuntimeError(f"control-point motion read-back failed: requested={requested_motion}, actual={actual_motion}")
        motion_resolution = {"requested": motion, "requested_axes": requested_motion, "actual_axes": actual_motion, "status": "PASS"}
        self.runtime_resolution["control_point_motion"] = motion_resolution
        if self._active_attempt_runtime is not None:
            self._active_attempt_runtime["control_point_motion"] = copy.deepcopy(motion_resolution)
        actual_x = int(self._setting_state(tool.region.cartesian.conditions.x.points)) if x_active else None
        actual_y = int(self._setting_state(tool.region.cartesian.conditions.y.points)) if y_active else None
        requested = {"x": int(self.context["x_control_points"]), "y": int(self.context["y_control_points"])}
        actual = {"x": actual_x, "y": actual_y}
        mismatches = [axis for axis in ("x", "y") if actual[axis] is not None and actual[axis] != requested[axis]]
        cartesian_not_applicable = not x_active and not y_active
        morpher_actual = str((self.runtime_resolution.get("morpher") or {}).get("actual", "")).lower()
        design_variable_status = (
            "ACTIVE"
            if not mismatches
            and (
                (cartesian_not_applicable and morpher_actual in {"radial-basis-function", "polynomials", "direct-interpolation"})
                or (not cartesian_not_applicable and any(actual_motion.values()))
            )
            else "INACTIVE"
        )
        resolution = {
            "requested": requested,
            "actual": actual,
            "active": {"x": x_active, "y": y_active},
            "status": "NOT_APPLICABLE" if cartesian_not_applicable else ("PASS" if not mismatches else "FAIL"),
            "design_variable_status": design_variable_status,
            "mismatches": mismatches,
        }
        self.runtime_resolution["control_points"] = resolution
        if self._active_attempt_runtime is not None:
            self._active_attempt_runtime["control_points"] = copy.deepcopy(resolution)
        if mismatches:
            raise RuntimeError(f"Design control-point read-back mismatch: {resolution!r}")

    def _configure_and_run_optimizer(self, solver: Any) -> None:
        opt = solver.settings.design.gradient_based.optimizer
        self._settings_step("reset optimizer", lambda: opt.reset())
        self._settings_step("set optimizer type", lambda: setattr(opt, "optimizer_type", "shape-opt"), required=True)
        self._settings_step("apply optimizer defaults", lambda: opt.default(), required=True)
        settings = opt.optimizer_settings
        self._settings_step("set design iterations", lambda: setattr(settings, "design_iterations", int(self.context["design_iterations"])), required=True)
        self._settings_step("set flow iterations", lambda: setattr(settings, "flow_iterations", int(self.context["flow_iterations"])), required=True)
        self._settings_step("set adjoint iterations", lambda: setattr(settings, "adjoint_iterations", int(self.context["adjoint_iterations"])), required=True)
        configured_min_oq = deep_get(self.cfg, "advanced_settings.optimizer.min_orthogonal_quality", None)
        min_oq = configured_min_oq
        min_oq_source = "configured"
        if isinstance(min_oq, str) and min_oq.lower() == "fluent-default":
            min_oq = None
            min_oq_source = "fluent-default"
        elif isinstance(min_oq, str) and min_oq.lower() == "auto":
            min_oq_source = "auto"
            initial_oq = self.context.get("initial_fluent_minimum_orthogonal_quality")
            if isinstance(initial_oq, (int, float)):
                min_oq = max(0.10, min(0.20, 0.60 * float(initial_oq)))
            else:
                min_oq = 0.10
        if min_oq is not None:
            self._settings_step(
                "set optimizer minimum orthogonal quality",
                lambda: setattr(opt.mesh_quality.criteria, "min_orthogonal", float(min_oq)),
                required=True,
            )
        else:
            self.commands.append(
                {
                    "label": "preserve Fluent default optimizer minimum orthogonal quality",
                    "kind": "settings",
                    "status": "PASS",
                }
            )
        actual_min_oq = self._setting_state(opt.mesh_quality.criteria.min_orthogonal)
        try:
            resolved_min_oq = float(actual_min_oq)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Unable to read Fluent optimizer minimum orthogonal quality: {actual_min_oq!r}") from exc
        self.context["resolved_optimizer_min_orthogonal_quality"] = resolved_min_oq
        quality_resolution = {
            "configured": configured_min_oq,
            "source": min_oq_source,
            "actual": resolved_min_oq,
            "initial_mesh": self.context.get("initial_fluent_minimum_orthogonal_quality"),
        }
        self.runtime_resolution["minimum_orthogonal_quality"] = quality_resolution
        if self._active_attempt_runtime is not None:
            self._active_attempt_runtime["minimum_orthogonal_quality"] = copy.deepcopy(quality_resolution)
        self._settings_step("set optimizer convergence criteria", lambda: setattr(settings, "convergence_criteria", float(deep_get(self.cfg, "advanced_settings.convergence_criteria.optimizer", 5.0e-4))))
        objectives = opt.objectives
        self._settings_step("select optimizer observables", lambda: setattr(objectives.observables, "selection", ["cd", "cl"]), required=True)
        objective_rows = objectives.objectives
        settings_cd_index, settings_cl_index = self._optimizer_objective_indices(objective_rows)
        binding = resolve_objective_binding_strategy(
            deep_get(self.cfg, "advanced_settings.optimizer.objective_binding_strategy", "auto"),
            fluent_version=(self.runtime_resolution.get("versions") or {}).get("fluent") or self.cfg.get("product_version"),
            pyfluent_version=(self.runtime_resolution.get("versions") or {}).get("pyfluent"),
        )
        if binding["resolved"] == "fluent-251-runtime-reverse":
            write_cd_index, write_cl_index = settings_cl_index, settings_cd_index
        else:
            write_cd_index, write_cl_index = settings_cd_index, settings_cl_index
        drag_step = float(self.context.get("drag_step_percent", deep_get(self.cfg, "advanced_settings.optimizer.drag_step_percent", -1.0)))
        lift_step = float(self.context.get("lift_step_percent", deep_get(self.cfg, "advanced_settings.optimizer.lift_step_percent", 0.0001)))
        objective_strategy = str(self.context.get("objective_strategy", "drag-with-lift-bound")).strip().lower()
        self._settings_step("set drag objective goal", lambda: setattr(objective_rows[write_cd_index], "goal", "step-size"), required=True)
        self._settings_step("set drag reduction step", lambda: setattr(objective_rows[write_cd_index], "value", drag_step), required=True)
        self._settings_step("set drag reduction step as percent", lambda: setattr(objective_rows[write_cd_index], "value_as_percentage", True), required=True)
        if objective_strategy == "coupled-drag-lift-step":
            if lift_step <= 0.0:
                raise RuntimeError(f"Coupled lift step must be positive, got {lift_step!r}")
            self._settings_step("set lift objective goal", lambda: setattr(objective_rows[write_cl_index], "goal", "step-size"), required=True)
            self._settings_step("set lift increase step", lambda: setattr(objective_rows[write_cl_index], "value", lift_step), required=True)
            self._settings_step("set lift increase step as percent", lambda: setattr(objective_rows[write_cl_index], "value_as_percentage", True), required=True)
        elif objective_strategy == "drag-with-lift-bound":
            self._settings_step("set lift constraint goal", lambda: setattr(objective_rows[write_cl_index], "goal", "bounded"), required=True)
            self._settings_step("set lift lower bound", lambda: setattr(objective_rows[write_cl_index], "lower_bound", float(self.context["minimum_allowed_lift_force"])), required=True)
            lift_bound_tolerance = float(
                self.context.get(
                    "lift_bound_tolerance_percent",
                    deep_get(self.cfg, "advanced_settings.optimizer.lift_bound_tolerance_percent", 0.02),
                )
            )
            self._settings_step(
                "set lift bound feasibility tolerance",
                lambda: setattr(objective_rows[write_cl_index], "tolerance", lift_bound_tolerance),
                required=True,
            )
            self._settings_step(
                "set lift bound feasibility tolerance as percent",
                lambda: setattr(objective_rows[write_cl_index], "tolerance_as_percentage", True),
                required=True,
            )
        else:
            raise RuntimeError(f"Unknown objective strategy: {objective_strategy!r}")
        objective_resolution = self._verify_optimizer_objectives(
            objective_rows,
            settings_cd_index,
            settings_cl_index,
            drag_step,
            float(self.context["minimum_allowed_lift_force"]),
            objective_strategy=objective_strategy,
            lift_step=lift_step,
            write_cd_index=write_cd_index,
            write_cl_index=write_cl_index,
            lift_bound_tolerance_percent=float(self.context.get("lift_bound_tolerance_percent", 0.02)),
        )
        objective_resolution["binding_strategy"] = binding
        self.runtime_resolution["objective_mapping"] = objective_resolution
        if self._active_attempt_runtime is not None:
            self._active_attempt_runtime["objective_mapping"] = copy.deepcopy(objective_resolution)
        self._settings_step("initialize optimizer", lambda: opt.initialize(), required=True)
        self._settings_step("run gradient based optimizer", lambda: opt.optimize(), required=True)
        self._settings_step("summarize optimizer runtime binding", lambda: opt.summarize())
        runtime_audit = optimizer_runtime_objective_audit(
            self.transcript_path,
            lift_lower_bound=(float(self.context["minimum_allowed_lift_force"]) if objective_strategy == "drag-with-lift-bound" else None),
            objective_strategy=objective_strategy,
        )
        objective_resolution["runtime_summary"] = runtime_audit
        objective_resolution["runtime_verified"] = bool(runtime_audit.get("verified"))
        objective_resolution["runtime_binding_verified"] = bool(runtime_audit.get("binding_verified"))
        objective_resolution["runtime_design_step_valid"] = bool(runtime_audit.get("design_step_valid"))
        self.runtime_resolution["objective_mapping"] = copy.deepcopy(objective_resolution)
        if self._active_attempt_runtime is not None:
            self._active_attempt_runtime["objective_mapping"] = copy.deepcopy(objective_resolution)
        if not runtime_audit.get("verified"):
            message = "; ".join(runtime_audit.get("errors") or ["Fluent optimizer runtime could not be verified"])
            if runtime_audit.get("linear_solver_stalled") or runtime_audit.get("final_cl_feasible") is False:
                raise OptimizerDesignStepError(message)
            raise ObjectiveRuntimeBindingError(message)

    def _optimizer_objective_indices(self, objective_rows: Any) -> tuple[int, int]:
        state = self._objective_rows_state(objective_rows)
        cd_index = cl_index = None
        for index, row in enumerate(state):
            observable = str(row.get("observable", "")).lower() if isinstance(row, dict) else ""
            if observable == "cd":
                cd_index = index
            if observable == "cl":
                cl_index = index
        source = "fluent_observable_state"
        if cd_index is None and cl_index is None:
            configured_order = deep_get(self.cfg, "advanced_settings.optimizer.objective_row_order", None)
            if configured_order:
                order = [str(item).lower() for item in configured_order]
                if "cd" in order and "cl" in order:
                    cd_index = order.index("cd")
                    cl_index = order.index("cl")
                    source = "configured_order_fallback"
        if cd_index is None or cl_index is None:
            raise RuntimeError(f"Optimizer objective rows do not unambiguously contain both cd and cl: {state}")
        self.commands.append(
            {
                "label": "map optimizer objective rows",
                "kind": "settings",
                "status": "PASS",
                "source": source,
                "cd_index": cd_index,
                "cl_index": cl_index,
            }
        )
        return cd_index, cl_index

    @staticmethod
    def _setting_state(setting: Any) -> Any:
        getter = getattr(setting, "get_state", None)
        if callable(getter):
            return getter()
        if callable(setting):
            return setting()
        return setting

    @staticmethod
    def _objective_rows_state(objective_rows: Any) -> list[dict[str, Any]]:
        state = objective_rows.get_state()
        if isinstance(state, list):
            return state
        if isinstance(state, tuple):
            return list(state)
        if isinstance(state, dict):
            return list(state.values())
        raise RuntimeError(f"Unexpected Fluent optimizer objective state: {state!r}")

    @staticmethod
    def _objective_row_value(row: dict[str, Any], name: str) -> Any:
        for key in (name, name.replace("_", "-"), name.replace("-", "_")):
            if key in row:
                return row[key]
        return None

    def _verify_optimizer_objectives(
        self,
        objective_rows: Any,
        cd_index: int,
        cl_index: int,
        drag_step: float,
        lift_lower_bound: float,
        *,
        objective_strategy: str = "drag-with-lift-bound",
        lift_step: float | None = None,
        write_cd_index: int | None = None,
        write_cl_index: int | None = None,
        lift_bound_tolerance_percent: float = 0.02,
    ) -> dict[str, Any]:
        state = self._objective_rows_state(objective_rows)
        write_cd_index = cd_index if write_cd_index is None else write_cd_index
        write_cl_index = cl_index if write_cl_index is None else write_cl_index
        try:
            settings_cd_row = state[cd_index]
            settings_cl_row = state[cl_index]
            write_cd_row = state[write_cd_index]
            write_cl_row = state[write_cl_index]
        except IndexError as exc:
            raise RuntimeError(f"Optimizer objective row index is outside the read-back state: {state}") from exc

        def normalized(value: Any) -> str:
            return str(value).strip().lower().replace("_", "-")

        errors: list[str] = []
        cd_observable = normalized(self._objective_row_value(settings_cd_row, "observable"))
        cl_observable = normalized(self._objective_row_value(settings_cl_row, "observable"))
        cd_goal = normalized(self._objective_row_value(write_cd_row, "goal"))
        cl_goal = normalized(self._objective_row_value(write_cl_row, "goal"))
        cd_value = self._objective_row_value(write_cd_row, "value")
        cd_percentage = self._objective_row_value(write_cd_row, "value_as_percentage")
        cl_value = self._objective_row_value(write_cl_row, "value")
        cl_percentage = self._objective_row_value(write_cl_row, "value_as_percentage")
        cl_lower_bound = self._objective_row_value(write_cl_row, "lower_bound")
        cl_tolerance = self._objective_row_value(write_cl_row, "tolerance")
        cl_tolerance_percentage = self._objective_row_value(write_cl_row, "tolerance_as_percentage")
        if cd_observable not in {"", "none", "cd"}:
            errors.append(f"Cd row observable is {cd_observable!r}")
        if cl_observable not in {"", "none", "cl"}:
            errors.append(f"Cl row observable is {cl_observable!r}")
        if cd_goal != "step-size":
            errors.append(f"Cd goal is {cd_goal!r}, expected 'step-size'")
        strategy = str(objective_strategy or "drag-with-lift-bound").strip().lower()
        expected_cl_goal = "step-size" if strategy == "coupled-drag-lift-step" else "bounded"
        if cl_goal != expected_cl_goal:
            errors.append(f"Cl goal is {cl_goal!r}, expected {expected_cl_goal!r}")
        try:
            if not math.isclose(float(cd_value), drag_step, rel_tol=1.0e-8, abs_tol=1.0e-12):
                errors.append(f"Cd step is {cd_value!r}, expected {drag_step!r}")
        except (TypeError, ValueError):
            errors.append(f"Cd step is not numeric: {cd_value!r}")
        if cd_percentage is not True:
            errors.append(f"Cd value_as_percentage is {cd_percentage!r}, expected True")
        if strategy == "coupled-drag-lift-step":
            try:
                if lift_step is None or not math.isclose(float(cl_value), float(lift_step), rel_tol=1.0e-8, abs_tol=1.0e-12):
                    errors.append(f"Cl step is {cl_value!r}, expected {lift_step!r}")
            except (TypeError, ValueError):
                errors.append(f"Cl step is not numeric: {cl_value!r}")
            if cl_percentage is not True:
                errors.append(f"Cl value_as_percentage is {cl_percentage!r}, expected True")
        elif strategy == "drag-with-lift-bound":
            try:
                if not math.isclose(float(cl_lower_bound), lift_lower_bound, rel_tol=1.0e-8, abs_tol=1.0e-9):
                    errors.append(f"Cl lower bound is {cl_lower_bound!r}, expected {lift_lower_bound!r}")
            except (TypeError, ValueError):
                errors.append(f"Cl lower bound is not numeric: {cl_lower_bound!r}")
            try:
                if not math.isclose(
                    float(cl_tolerance), float(lift_bound_tolerance_percent), rel_tol=1.0e-8, abs_tol=1.0e-12
                ):
                    errors.append(
                        f"Cl bound tolerance is {cl_tolerance!r}, expected {lift_bound_tolerance_percent!r}"
                    )
            except (TypeError, ValueError):
                errors.append(f"Cl bound tolerance is not numeric: {cl_tolerance!r}")
            if cl_tolerance_percentage is not True:
                errors.append(
                    f"Cl tolerance_as_percentage is {cl_tolerance_percentage!r}, expected True"
                )
        else:
            errors.append(f"Unknown objective strategy {strategy!r}")

        resolution = {
            "source": next(
                (command.get("source") for command in reversed(self.commands) if command.get("label") == "map optimizer objective rows"),
                "unknown",
            ),
            "cd_index": cd_index,
            "cl_index": cl_index,
            "settings_view": {
                "cd_index": cd_index,
                "cl_index": cl_index,
                "rows": state,
            },
            "binding_indices": {"cd": write_cd_index, "cl": write_cl_index},
            "objective_strategy": strategy,
            "requested_steps_percent": {"cd": drag_step, "cl": lift_step if strategy == "coupled-drag-lift-step" else None},
            "lift_lower_bound": lift_lower_bound if strategy == "drag-with-lift-bound" else None,
            "lift_runtime_bound_ratio": (
                float(self.context.get("lift_runtime_bound_ratio"))
                if strategy == "drag-with-lift-bound" and self.context.get("lift_runtime_bound_ratio") is not None
                else None
            ),
            "lift_bound_tolerance_percent": (
                float(lift_bound_tolerance_percent) if strategy == "drag-with-lift-bound" else None
            ),
            "readback": state,
            "verified": not errors,
        }
        self.commands.append(
            {
                "label": "verify optimizer objectives read-back",
                "kind": "settings",
                "status": "PASS" if not errors else "FAIL",
                **resolution,
                "errors": errors,
            }
        )
        if errors:
            raise RuntimeError("Optimizer objective read-back verification failed before morphing: " + "; ".join(errors))
        return resolution

    def _delete_named_if_present(self, collection: Any, name: str, label: str) -> None:
        existing = self._named_object_names(collection)
        if name in existing:
            self._settings_step(f"delete existing {label}", lambda: collection.delete(name), required=True)
        else:
            self.commands.append({"label": f"delete existing {label}", "kind": "settings", "status": "SKIP", "reason": "not present"})

    def _named_object_names(self, collection: Any) -> set[str]:
        for getter_name in ("get_object_names", "list"):
            getter = getattr(collection, getter_name, None)
            if not callable(getter):
                continue
            try:
                names = getter()
            except Exception:
                continue
            if isinstance(names, str):
                return set(re.findall(r"[A-Za-z0-9_.:-]+", names))
            if isinstance(names, (list, tuple, set)):
                return {str(item) for item in names}
        try:
            state = collection.get_state()
        except Exception:
            return set()
        if isinstance(state, dict):
            return {str(key) for key in state}
        if isinstance(state, (list, tuple)):
            return {
                str(item.get("name"))
                for item in state
                if isinstance(item, dict) and item.get("name") is not None
            }
        return set()

    def _minimum_allowed_cl(self, baseline_cl: float | None) -> float:
        if baseline_cl is None:
            return 0.0
        ratio = deep_get(self.cfg, "completion.minimum_lift_ratio", None)
        if ratio is None:
            ratio = 1.0 - float(deep_get(self.cfg, "completion.lift_relative_tolerance", 0.005))
        return baseline_cl * float(ratio)

    def _minimum_allowed_lift_force(self, baseline_cl: float | None) -> float:
        ratio = float(
            self.context.get(
                "lift_runtime_bound_ratio",
                deep_get(
                    self.cfg,
                    "advanced_settings.optimizer.lift_runtime_bound_ratio",
                    deep_get(self.cfg, "completion.minimum_lift_ratio", 1.0 - float(deep_get(self.cfg, "completion.lift_relative_tolerance", 0.005))),
                ),
            )
        )
        calibration = float(
            self.context.get(
                "lift_force_report_to_observable_factor",
                deep_get(self.cfg, "advanced_settings.optimizer.lift_force_report_to_observable_factor", 0.982),
            )
        )
        baseline_force = self.context.get("baseline_lift_force_total_force")
        if isinstance(baseline_force, (int, float)):
            return float(baseline_force) * ratio * calibration
        baseline_value = float(baseline_cl or 0.0)
        return baseline_value * ratio * float(self.context["dynamic_pressure_pa"]) * float(self.context["reference_area_m2"]) * calibration

    def _passes_completion_gate(self, baseline: Coefficients, final: Coefficients) -> bool:
        if baseline.cd is None or baseline.cl is None or final.cd is None or final.cl is None:
            return False
        if self._transcript_reports_invalid_morphing():
            return False
        drag_ok = 0.0 < final.cd < baseline.cd
        lift_ok = final.cl >= self._minimum_allowed_cl(baseline.cl)
        return drag_ok and lift_ok

    def _transcript_reports_invalid_morphing(self) -> bool:
        if not self.transcript_path or not self.transcript_path.exists():
            return False
        text = self.transcript_path.read_text(encoding="utf-8", errors="ignore").lower()
        invalid_markers = (
            "negative cell volumes are detected",
            "stopped due to (negative volume cell)",
        )
        return any(marker in text for marker in invalid_markers)
