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
from dataclasses import dataclass
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any

from airfoil_fluentmeshing.boundary_layer import first_layer_from_velocity
from airfoil_fluentmeshing.fluent_runner import fluent_path_arg, fluent_product_version_arg, prepare_fluent_env
from airfoil_fluentmeshing.geometry import read_dat, split_normalized, write_dat_sections
from airfoil_fluentmeshing.shape_guard import build_geometry_snapshot, compare_geometry, resolve_shape_guard
from airfoil_fluentmeshing.optimization_profiles import (
    build_optimization_profile,
    validate_aerodynamic_controls,
    validate_control_points,
)

from airfoil_fluentmeshing.adjoint_support import *

# Pure transcript, geometry, performance, export, and interaction audits.

def transcript_morphing_audit(transcript_path: str | Path | None) -> dict[str, Any]:
    if not transcript_path:
        return {"status": "NOT_AVAILABLE", "invalid_morphing": False, "markers": [], "negative_cell_volume_samples": []}
    path = Path(transcript_path)
    if not path.exists():
        return {
            "status": "MISSING",
            "invalid_morphing": False,
            "path": str(path),
            "markers": [],
            "negative_cell_volume_samples": [],
        }
    text = path.read_text(encoding="utf-8", errors="ignore")
    lowered = text.lower()
    marker_patterns = [
        "negative cell volumes are detected",
        "stopped due to (negative volume cell)",
        "negative volume cell",
    ]
    markers = [marker for marker in marker_patterns if marker in lowered]
    negative_samples: list[dict[str, Any]] = []
    min_value: float | None = None
    last_value: float | None = None
    last_orthogonal_quality: float | None = None
    orthogonal_quality_limit: float | None = None
    max_boundary_displacement: float | None = None
    max_average_boundary_displacement: float | None = None
    step_reduction_factors: list[float] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        design_quality = re.search(
            r"^\s*\d+\|\s*([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)\|\s*"
            r"([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)",
            line,
        )
        if design_quality:
            value = float(design_quality.group(1))
            last_value = value
            min_value = value if min_value is None else min(min_value, value)
            last_orthogonal_quality = float(design_quality.group(2))
            if value < 0.0:
                negative_samples.append({"line": line_no, "value": value, "text": line.strip()})
        minimum_volume = re.search(
            r"Minimum\s+Volume\s*=\s*([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)",
            line,
            re.IGNORECASE,
        )
        if minimum_volume:
            value = float(minimum_volume.group(1))
            last_value = value
            min_value = value if min_value is None else min(min_value, value)
        displacement = re.search(
            r"maximum\s+boundary\s+displacement\s*(?:is|[:=])\s*([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)",
            line,
            re.IGNORECASE,
        )
        if displacement:
            value = abs(float(displacement.group(1)))
            max_boundary_displacement = value if max_boundary_displacement is None else max(max_boundary_displacement, value)
        average_displacement = re.search(
            r"average(?:d)?\s+boundary\s+displacement\s*(?:is|[:=])\s*([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)",
            line,
            re.IGNORECASE,
        )
        if average_displacement:
            value = abs(float(average_displacement.group(1)))
            max_average_boundary_displacement = (
                value if max_average_boundary_displacement is None else max(max_average_boundary_displacement, value)
            )
        reduction = re.search(
            r"step(?:\s+size)?\s+(?:is\s+)?reduc(?:ed|tion).*?(?:factor|by)\s*[:=]?\s*"
            r"([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)",
            line,
            re.IGNORECASE,
        )
        if reduction:
            step_reduction_factors.append(float(reduction.group(1)))
        else:
            reduction = re.search(
                r"scale\s+of\s+the\s+current\s+step\s+size\s+is\s+reduced\s+to\s+"
                r"([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)",
                line,
                re.IGNORECASE,
            )
            if reduction:
                step_reduction_factors.append(float(reduction.group(1)))
        match = re.search(r"min\s+cell-volume\s*:\s*.*?current value\s+([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)", line, re.IGNORECASE)
        if not match:
            continue
        value = float(match.group(1))
        last_value = value
        min_value = value if min_value is None else min(min_value, value)
        if value < 0.0:
            negative_samples.append({"line": line_no, "value": value, "text": line.strip()})
        oq_matches = re.findall(
            r"min\s+orthogonal-quality\s*:\s*limit\s+"
            r"([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)\s*;\s*current value\s+"
            r"([-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)",
            line,
            re.IGNORECASE,
        )
        if oq_matches:
            orthogonal_quality_limit = float(oq_matches[-1][0])
            last_orthogonal_quality = float(oq_matches[-1][1])
    invalid = bool(markers or negative_samples)
    return {
        "status": "FAIL" if invalid else "PASS",
        "invalid_morphing": invalid,
        "path": str(path),
        "markers": markers,
        "minimum_reported_cell_volume": min_value,
        "last_reported_cell_volume": last_value,
        "last_reported_orthogonal_quality": last_orthogonal_quality,
        "optimizer_minimum_orthogonal_quality_limit": orthogonal_quality_limit,
        "maximum_reported_boundary_displacement": max_boundary_displacement,
        "maximum_reported_average_boundary_displacement": max_average_boundary_displacement,
        "step_reduction_factors": step_reduction_factors,
        "step_reduced_to_zero": any(abs(value) <= 1.0e-15 for value in step_reduction_factors),
        "negative_cell_volume_samples": negative_samples[:20],
        "negative_cell_volume_count": len(negative_samples),
    }


def resolve_objective_binding_strategy(
    configured: Any,
    *,
    fluent_version: Any,
    pyfluent_version: Any,
) -> dict[str, Any]:
    requested = str(configured or "auto").strip().lower()
    if requested not in {"auto", "fluent-251-runtime-reverse", "settings-observable"}:
        raise ValueError(
            "advanced_settings.optimizer.objective_binding_strategy must be auto, "
            "fluent-251-runtime-reverse, or settings-observable"
        )
    fluent_text = str(fluent_version or "")
    pyfluent_text = str(pyfluent_version or "")
    proven_fluent = bool(re.search(r"(?:^|\D)25\.1(?:\.\d+)?(?:\D|$)", fluent_text)) or "2025 r1" in fluent_text.lower()
    proven_pair = proven_fluent and bool(
        re.search(r"(?:^|\D)0\.40(?:\.\d+)?(?:\D|$)", pyfluent_text)
    )
    resolved = "fluent-251-runtime-reverse" if requested == "auto" and proven_pair else requested
    if resolved == "auto":
        resolved = "settings-observable"
    return {
        "requested": requested,
        "resolved": resolved,
        "fluent_version": fluent_text,
        "pyfluent_version": pyfluent_text,
        "compatibility_proven": proven_pair if resolved == "fluent-251-runtime-reverse" else False,
        "runtime_verification_required": True,
    }


def resolve_rbf_numerics(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = config or {}
    resolved = {
        "max_iterations": int(raw.get("max_iterations", 10)),
        "linear_solver_tolerance": float(raw.get("linear_solver_tolerance", 1.0e-5)),
        "max_subiteration": int(raw.get("max_subiteration", 100)),
        "number_of_modes": int(raw.get("number_of_modes", 40)),
    }
    if resolved["max_iterations"] < 1 or resolved["max_subiteration"] < 1 or resolved["number_of_modes"] < 1:
        raise ValueError("RBF iteration and mode settings must be positive integers")
    if not 0.0 < resolved["linear_solver_tolerance"] <= 1.0:
        raise ValueError("RBF linear solver tolerance must be in (0, 1]")
    return resolved


def optimizer_runtime_objective_audit(
    transcript_path: str | Path | None,
    *,
    lift_lower_bound: float | None = None,
    objective_strategy: str = "drag-with-lift-bound",
) -> dict[str, Any]:
    path = Path(transcript_path) if transcript_path else None
    if path is None or not path.exists():
        return {"status": "FAIL", "verified": False, "path": str(path) if path else None, "errors": ["optimizer transcript is missing"]}
    text = path.read_text(encoding="utf-8", errors="ignore")
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if "|" not in line:
            continue
        columns = [part.strip() for part in line.split("|")]
        if len(columns) < 10 or columns[3].lower() not in {"cd", "cl"}:
            continue
        try:
            design_iteration = int(columns[0])
            observable_value = float(columns[6])
        except ValueError:
            continue
        rows.append(
            {
                "line": line_no,
                "design_iteration": design_iteration,
                "id": columns[1],
                "condition": columns[2],
                "observable": columns[3].lower(),
                "flow_converged": columns[4],
                "adjoint_converged": columns[5],
                "observable_value": observable_value,
                "expected_change": columns[7],
                "observable_minus_target": columns[8],
                "feasible": columns[9],
                "text": line.strip(),
            }
        )
    initial: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["design_iteration"] == 0:
            initial[row["observable"]] = row
    errors: list[str] = []
    cd_row = initial.get("cd")
    cl_row = initial.get("cl")
    strategy = str(objective_strategy or "drag-with-lift-bound").strip().lower()
    if strategy not in {"coupled-drag-lift-step", "drag-with-lift-bound"}:
        errors.append(f"unknown objective strategy {strategy!r}")

    def expected_change(row: dict[str, Any] | None) -> float | None:
        if row is None:
            return None
        try:
            return float(row.get("expected_change"))
        except (TypeError, ValueError):
            return None

    if cd_row is None or cl_row is None:
        errors.append("initial optimizer summary does not contain both Cd and Cl rows")
    else:
        if cd_row["feasible"].upper() not in {"", "-"}:
            errors.append(f"Cd is incorrectly treated as a bounded constraint (feasible={cd_row['feasible']!r})")
        if cd_row["observable_minus_target"] not in {"", "-"}:
            errors.append(
                "Cd has an unexpected constraint difference "
                f"{cd_row['observable_minus_target']!r}; the Cl lower bound may be bound to Cd"
            )
        cd_change = expected_change(cd_row)
        if cd_change is None or cd_change >= 0.0:
            errors.append(f"Cd expected change is not negative: {cd_row['expected_change']!r}")
        if strategy == "coupled-drag-lift-step":
            if cl_row["feasible"].upper() not in {"", "-"}:
                errors.append(f"Cl is unexpectedly treated as a bounded constraint (feasible={cl_row['feasible']!r})")
            if cl_row["observable_minus_target"] not in {"", "-"}:
                errors.append(f"Cl has an unexpected constraint difference {cl_row['observable_minus_target']!r}")
            cl_change = expected_change(cl_row)
            if cl_change is None or cl_change <= 0.0:
                errors.append(f"Cl expected change is not positive: {cl_row['expected_change']!r}")
        else:
            if cl_row["feasible"].upper() != "Y":
                errors.append(f"Cl is not the feasible bounded constraint (feasible={cl_row['feasible']!r})")
            if lift_lower_bound is None:
                errors.append("lift lower bound is missing for drag-with-lift-bound")
            else:
                displayed_tolerance = max(1.0, 0.01 * abs(float(lift_lower_bound)))
                if cl_row["observable_value"] + displayed_tolerance < float(lift_lower_bound):
                    errors.append(
                        f"Cl observable {cl_row['observable_value']!r} is below configured lower bound {lift_lower_bound!r}"
                    )
    binding_errors = list(errors)
    maximum_design_iteration = max((int(row["design_iteration"]) for row in rows), default=None)
    final_cl_rows = [
        row
        for row in rows
        if row["observable"] == "cl" and row["design_iteration"] == maximum_design_iteration
    ]
    final_cl_feasible = None
    if maximum_design_iteration is not None and maximum_design_iteration > 0 and final_cl_rows:
        final_cl_feasible = final_cl_rows[-1]["feasible"].upper() == "Y"
        if not final_cl_feasible:
            errors.append(
                "final optimizer design step violates the Cl lower-bound constraint "
                f"(feasible={final_cl_rows[-1]['feasible']!r})"
            )
    linear_solver_stalled = "linear solver exits due to divergence or stalling" in text.lower()
    if linear_solver_stalled:
        errors.append("optimizer design-change linear solver diverged or stalled")
    return {
        "status": "PASS" if not errors else "FAIL",
        "verified": not errors,
        "binding_verified": not binding_errors,
        "design_step_valid": not linear_solver_stalled and final_cl_feasible is not False,
        "path": str(path),
        "objective_strategy": strategy,
        "lift_lower_bound": float(lift_lower_bound) if lift_lower_bound is not None else None,
        "settings_summary_rows": initial,
        "all_summary_row_count": len(rows),
        "maximum_design_iteration": maximum_design_iteration,
        "final_cl_feasible": final_cl_feasible,
        "linear_solver_stalled": linear_solver_stalled,
        "unfeasible_constraint_message_present": "unfeasible constraints detected" in text.lower(),
        "errors": errors,
        "binding_errors": binding_errors,
    }


def resolve_shape_anchor_ranges(
    baseline_geometry: dict[str, Any],
    anchor_config: dict[str, Any] | None,
) -> dict[str, Any]:
    cfg = anchor_config or {}
    enabled = bool(cfg.get("enabled", False))
    if not enabled:
        return {"status": "DISABLED", "enabled": False}
    metrics = baseline_geometry.get("metrics") or {}
    xmin = float(metrics.get("xmin"))
    xmax = float(metrics.get("xmax"))
    chord = float(metrics.get("chord", xmax - xmin))
    mode = str(cfg.get("mode", "endpoints-only"))
    max_displacement = float(cfg.get("max_anchor_displacement_over_chord", 0.0))
    if not math.isfinite(chord) or chord <= 0.0:
        raise ShapeAnchorSetupError(f"baseline chord is invalid: {chord!r}")
    if mode != "endpoints-only":
        raise ShapeAnchorSetupError("shape anchor mode must be endpoints-only")
    if not math.isfinite(max_displacement) or max_displacement < 0.0:
        raise ShapeAnchorSetupError("max_anchor_displacement_over_chord must be finite and non-negative")
    points = baseline_geometry.get("points") or []
    tolerance = max(1.0e-12 * chord, 1.0e-14)
    # Fluent rejects an iso-clip with identical minimum and maximum values.
    # This one-sided interval selects the same endpoint vertices safely.
    clip_width = max(1.0e-8 * chord, 10.0 * tolerance)
    le_candidates = [index for index, point in enumerate(points) if abs(float(point[0]) - xmin) <= tolerance]
    te_candidates = [index for index, point in enumerate(points) if abs(float(point[0]) - xmax) <= tolerance]
    le_indices = [min(le_candidates, key=lambda index: abs(float(points[index][1])))] if le_candidates else []
    if len(te_candidates) <= 2:
        te_indices = te_candidates
    else:
        te_indices = [
            min(te_candidates, key=lambda index: float(points[index][1])),
            max(te_candidates, key=lambda index: float(points[index][1])),
        ]
        te_indices = list(dict.fromkeys(te_indices))
    if not le_indices or not te_indices:
        raise ShapeAnchorSetupError("leading-edge or trailing-edge anchor contains no baseline vertices")
    if set(le_indices) & set(te_indices):
        raise ShapeAnchorSetupError("leading-edge and trailing-edge anchors overlap")
    return {
        "status": "PASS",
        "enabled": True,
        "surface_names": ["airfoil_anchor_le", "airfoil_anchor_te"],
        "field": "x-coordinate",
        "xmin": xmin,
        "xmax": xmax,
        "chord": chord,
        "mode": mode,
        "leading_edge_range": [xmin, min(xmax, xmin + clip_width)],
        "trailing_edge_range": [max(xmin, xmax - clip_width), xmax],
        "clip_width": clip_width,
        "leading_edge_vertex_count": len(le_indices),
        "trailing_edge_vertex_count": len(te_indices),
        "leading_edge_indices": le_indices,
        "trailing_edge_indices": te_indices,
        "max_anchor_displacement_over_chord": max_displacement,
    }


def anchor_displacement_audit(
    baseline_geometry: dict[str, Any],
    candidate_geometry: dict[str, Any],
    anchor_resolution: dict[str, Any] | None,
) -> dict[str, Any]:
    resolution = anchor_resolution or {}
    if not resolution.get("enabled"):
        return {"status": "DISABLED", "enabled": False}
    baseline_points = baseline_geometry.get("points") or []
    candidate_points = candidate_geometry.get("points") or []
    if len(baseline_points) != len(candidate_points):
        return {
            "status": "FAIL",
            "enabled": True,
            "errors": [f"anchor point correspondence changed: {len(baseline_points)} != {len(candidate_points)}"],
        }
    chord = float(resolution["chord"])
    maximum_allowed = float(resolution["max_anchor_displacement_over_chord"])
    groups: dict[str, Any] = {}
    maximum = 0.0
    for label, key in (("leading_edge", "leading_edge_indices"), ("trailing_edge", "trailing_edge_indices")):
        displacements = [
            math.hypot(
                float(candidate_points[index][0]) - float(baseline_points[index][0]),
                float(candidate_points[index][1]) - float(baseline_points[index][1]),
            )
            for index in resolution[key]
        ]
        group_max = max(displacements, default=math.inf)
        maximum = max(maximum, group_max)
        groups[label] = {"vertex_count": len(displacements), "maximum_displacement": group_max, "maximum_displacement_over_chord": group_max / chord}
    ratio = maximum / chord
    passed = math.isfinite(ratio) and ratio <= maximum_allowed
    return {
        "status": "PASS" if passed else "FAIL",
        "enabled": True,
        "maximum_displacement": maximum,
        "maximum_displacement_over_chord": ratio,
        "maximum_allowed_over_chord": maximum_allowed,
        "groups": groups,
        "errors": [] if passed else [f"anchor displacement {ratio:.6g}c exceeds {maximum_allowed:.6g}c"],
    }


def resolve_thickness_constraint(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = config or {}
    if raw.get("mode") == "fluent-envelope":
        raise ValueError("fluent-envelope has been removed; use the direct geometry thickness audit")
    enabled = bool(raw.get("enabled", True))
    clearance = float(raw.get("clearance_percent_of_baseline_max_thickness", 5.0))
    minimum_local_thickness_ratio = float(raw.get("minimum_local_thickness_ratio", 0.90))
    minimum_area_ratio = float(raw.get("minimum_area_ratio", 0.95))
    samples = int(raw.get("samples", 401))
    if enabled and (not math.isfinite(clearance) or not 0.0 < clearance < 50.0):
        raise ValueError("thickness clearance percent must be in (0, 50)")
    if samples < 25:
        raise ValueError("thickness audit samples must be at least 25")
    if not 0.0 < minimum_local_thickness_ratio <= 1.0:
        raise ValueError("minimum local thickness ratio must be in (0, 1]")
    if not 0.0 < minimum_area_ratio <= 1.0:
        raise ValueError("minimum area ratio must be in (0, 1]")
    return {
        "enabled": enabled,
        "audit": "direct-section-geometry",
        "clearance_percent_of_baseline_max_thickness": clearance,
        "minimum_local_thickness_ratio": minimum_local_thickness_ratio,
        "minimum_area_ratio": minimum_area_ratio,
        "samples": samples,
    }


def _section_ordinates(points: list[list[float]], x: float) -> list[float]:
    ordinates: list[float] = []
    for index, first in enumerate(points):
        second = points[(index + 1) % len(points)]
        x0, y0 = float(first[0]), float(first[1])
        x1, y1 = float(second[0]), float(second[1])
        if abs(x1 - x0) <= 1.0e-15:
            continue
        if (x0 <= x < x1) or (x1 <= x < x0):
            ratio = (x - x0) / (x1 - x0)
            ordinates.append(y0 + ratio * (y1 - y0))
    ordinates.sort()
    return ordinates


def _polygon_area(points: list[list[float]]) -> float:
    if len(points) < 3:
        return 0.0
    return abs(
        0.5
        * sum(
            float(points[index][0]) * float(points[(index + 1) % len(points)][1])
            - float(points[(index + 1) % len(points)][0]) * float(points[index][1])
            for index in range(len(points))
        )
    )


def thickness_geometry_audit(
    baseline_geometry: dict[str, Any],
    candidate_geometry: dict[str, Any],
    *,
    margin_percent: float,
    enabled: bool = True,
    samples: int = 401,
    minimum_local_thickness_ratio: float | None = None,
    minimum_area_ratio: float | None = None,
) -> dict[str, Any]:
    """Compare candidate thickness directly with the baseline at each section."""
    if not enabled:
        return {"status": "DISABLED", "enabled": False, "audit": "direct-section-geometry"}
    if not math.isfinite(float(margin_percent)) or float(margin_percent) <= 0.0:
        return {"status": "FAIL", "enabled": True, "errors": ["margin_percent must be positive"]}
    if samples < 25:
        return {"status": "FAIL", "enabled": True, "errors": ["samples must be at least 25"]}
    if minimum_local_thickness_ratio is not None and not 0.0 < float(minimum_local_thickness_ratio) <= 1.0:
        return {"status": "FAIL", "enabled": True, "errors": ["minimum_local_thickness_ratio must be in (0, 1]"]}
    if minimum_area_ratio is not None and not 0.0 < float(minimum_area_ratio) <= 1.0:
        return {"status": "FAIL", "enabled": True, "errors": ["minimum_area_ratio must be in (0, 1]"]}
    if baseline_geometry.get("status") != "PASS" or candidate_geometry.get("status") != "PASS":
        return {
            "status": "FAIL",
            "enabled": True,
            "errors": ["baseline or candidate geometry is invalid"],
        }
    baseline_points = list(baseline_geometry.get("points") or [])
    candidate_points = list(candidate_geometry.get("points") or [])
    base_metrics = baseline_geometry.get("metrics") or {}
    xmin = float(base_metrics["xmin"])
    xmax = float(base_metrics["xmax"])
    chord = float(base_metrics["chord"])
    max_thickness = float(base_metrics["maximum_thickness"])
    baseline_area = float(base_metrics.get("area", _polygon_area(baseline_points)))
    candidate_area = float((candidate_geometry.get("metrics") or {}).get("area", _polygon_area(candidate_points)))
    area_ratio = candidate_area / baseline_area if baseline_area > 0.0 else math.nan
    clearance = float(margin_percent) / 100.0 * max_thickness
    tolerance = max(1.0e-7 * chord, 1.0e-10)
    if chord <= 0.0 or max_thickness <= 0.0 or 2.0 * clearance >= max_thickness:
        return {
            "status": "FAIL",
            "enabled": True,
            "errors": ["baseline chord, thickness, or requested clearance is invalid"],
        }

    audited = 0
    invalid_sections: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    minimum_upper_margin = math.inf
    minimum_lower_margin = math.inf
    minimum_thickness_ratio = math.inf
    worst_thickness_ratio_x: float | None = None
    thickness_ratio_violations: list[dict[str, Any]] = []
    worst_upper_x: float | None = None
    worst_lower_x: float | None = None
    x_start: float | None = None
    x_end: float | None = None
    for index in range(1, samples):
        theta = math.pi * index / samples
        x = xmin + 0.5 * chord * (1.0 - math.cos(theta))
        baseline_ys = _section_ordinates(baseline_points, x)
        candidate_ys = _section_ordinates(candidate_points, x)
        if len(baseline_ys) != 2:
            invalid_sections.append({"x": x, "geometry": "baseline", "intersection_count": len(baseline_ys)})
            continue
        baseline_lower, baseline_upper = baseline_ys
        if baseline_upper - baseline_lower <= 2.0 * clearance:
            continue
        if len(candidate_ys) != 2:
            invalid_sections.append({"x": x, "geometry": "candidate", "intersection_count": len(candidate_ys)})
            continue
        candidate_lower, candidate_upper = candidate_ys
        baseline_thickness = baseline_upper - baseline_lower
        candidate_thickness = candidate_upper - candidate_lower
        local_thickness_ratio = candidate_thickness / baseline_thickness
        if local_thickness_ratio < minimum_thickness_ratio:
            minimum_thickness_ratio, worst_thickness_ratio_x = local_thickness_ratio, x
        if (
            minimum_local_thickness_ratio is not None
            and local_thickness_ratio < float(minimum_local_thickness_ratio) - 1.0e-10
        ):
            thickness_ratio_violations.append({"x": x, "local_thickness_ratio": local_thickness_ratio})
        required_lower = baseline_lower + clearance
        required_upper = baseline_upper - clearance
        upper_margin = candidate_upper - required_upper
        lower_margin = required_lower - candidate_lower
        audited += 1
        x_start = x if x_start is None else x_start
        x_end = x
        if upper_margin < minimum_upper_margin:
            minimum_upper_margin, worst_upper_x = upper_margin, x
        if lower_margin < minimum_lower_margin:
            minimum_lower_margin, worst_lower_x = lower_margin, x
        if upper_margin < -tolerance or lower_margin < -tolerance:
            violations.append(
                {
                    "x": x,
                    "upper_clearance_margin": upper_margin,
                    "lower_clearance_margin": lower_margin,
                }
            )
    errors: list[str] = []
    if invalid_sections:
        errors.append("section_intersection_count_invalid")
    if not audited:
        errors.append("no_valid_thickness_sections")
    if violations:
        errors.append("candidate_violates_baseline_thickness_limit")
    if thickness_ratio_violations:
        errors.append("candidate_below_minimum_local_thickness_ratio")
    if minimum_area_ratio is not None and (
        not math.isfinite(area_ratio) or area_ratio < float(minimum_area_ratio) - 1.0e-10
    ):
        errors.append("candidate_below_minimum_area_ratio")
    passed = not errors
    return {
        "status": "PASS" if passed else "FAIL",
        "audit": "direct-section-geometry",
        "enabled": True,
        "margin_percent": float(margin_percent),
        "clearance": clearance,
        "clearance_over_chord": clearance / chord,
        "tolerance": tolerance,
        "audited_section_count": audited,
        "x_start": x_start,
        "x_end": x_end,
        "minimum_upper_clearance_margin": minimum_upper_margin if audited else None,
        "minimum_lower_clearance_margin": minimum_lower_margin if audited else None,
        "minimum_local_thickness_ratio": minimum_thickness_ratio if audited else None,
        "required_minimum_local_thickness_ratio": minimum_local_thickness_ratio,
        "worst_thickness_ratio_x": worst_thickness_ratio_x,
        "thickness_ratio_violation_count": len(thickness_ratio_violations),
        "area_ratio": area_ratio,
        "required_minimum_area_ratio": minimum_area_ratio,
        "worst_upper_x": worst_upper_x,
        "worst_lower_x": worst_lower_x,
        "violation_count": len(violations),
        "violations": violations[:20],
        "invalid_sections": invalid_sections[:20],
        "errors": errors,
    }


def optimization_attempt_profiles(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    base = dict(deep_get(cfg, "optimizer", {}))
    base.setdefault("name", "standard_primary")
    # The advanced design-tool values are the public source of truth.  Older
    # configs may still carry duplicated optimizer.x/y_control_points fields.
    base["x_control_points"] = int(
        deep_get(cfg, "advanced_settings.design_tool.x_control_points", base.get("x_control_points", 24))
    )
    base["y_control_points"] = int(
        deep_get(cfg, "advanced_settings.design_tool.y_control_points", base.get("y_control_points", 8))
    )
    base.setdefault("drag_step_percent", deep_get(cfg, "advanced_settings.optimizer.drag_step_percent", -0.0001))
    base.setdefault("lift_step_percent", deep_get(cfg, "advanced_settings.optimizer.lift_step_percent", 0.0001))
    base.setdefault("objective_strategy", deep_get(cfg, "advanced_settings.optimizer.objective_strategy", "drag-with-lift-bound"))
    base.setdefault("lift_runtime_bound_ratio", deep_get(cfg, "advanced_settings.optimizer.lift_runtime_bound_ratio", 0.999))
    base.setdefault("lift_bound_tolerance_percent", deep_get(cfg, "advanced_settings.optimizer.lift_bound_tolerance_percent", 0.02))
    base["tier"] = "standard"
    profiles = [base]
    for index, raw in enumerate(cfg.get("retry_profiles", []), start=1):
        profile = dict(raw)
        profile.setdefault("name", f"standard_retry_{index}")
        profile.setdefault("objective_strategy", base["objective_strategy"])
        profile.setdefault("lift_step_percent", base["lift_step_percent"])
        profile.setdefault("lift_runtime_bound_ratio", base["lift_runtime_bound_ratio"])
        profile.setdefault("lift_bound_tolerance_percent", base["lift_bound_tolerance_percent"])
        profile["tier"] = "standard"
        profiles.append(profile)
    if bool(deep_get(cfg, "optimization_run.repair_on_profile_exhaustion", False)):
        for index, raw in enumerate(deep_get(cfg, "optimization_run.repair_profiles", []), start=1):
            profile = dict(raw)
            profile.setdefault("name", f"repair_retry_{index}")
            profile.setdefault("objective_strategy", base["objective_strategy"])
            profile.setdefault("lift_step_percent", base["lift_step_percent"])
            profile.setdefault("lift_runtime_bound_ratio", base["lift_runtime_bound_ratio"])
            profile.setdefault("lift_bound_tolerance_percent", base["lift_bound_tolerance_percent"])
            profile["tier"] = "repair"
            profiles.append(profile)
    return profiles


def assess_candidate(
    original: Coefficients,
    previous: Coefficients,
    final: Coefficients,
    audit: dict[str, Any],
    validation: dict[str, Any],
    *,
    lift_tolerance: float,
    accept_recovered: bool,
    minimum_relative_drag_improvement: float,
    required_orthogonal_quality: float,
    shape_report: dict[str, Any] | None = None,
    minimum_lift_ratio: float | None = None,
    require_lift_to_drag_improvement: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    coefficient_values = (original.cd, original.cl, previous.cd, previous.cl, final.cd, final.cl)
    if any(value is None or not math.isfinite(float(value)) for value in coefficient_values):
        reasons.append("missing_force_coefficients")
        return {"status": "FAIL", "accepted": False, "recovered": False, "reasons": reasons}
    assert original.cd is not None and original.cl is not None and previous.cd is not None and previous.cl is not None
    assert final.cd is not None and final.cl is not None
    relative_improvement = (previous.cd - final.cd) / previous.cd if previous.cd > 0.0 else float("-inf")
    if not (0.0 < final.cd < original.cd and final.cd < previous.cd):
        reasons.append("drag_not_reduced")
    if relative_improvement < minimum_relative_drag_improvement:
        reasons.append("drag_change_is_numerical_noise")
    resolved_lift_ratio = float(minimum_lift_ratio) if minimum_lift_ratio is not None else 1.0 - float(lift_tolerance)
    if final.cl < original.cl * resolved_lift_ratio:
        reasons.append("lift_below_original_baseline_gate")
    original_ld = original.cl / original.cd
    previous_ld = previous.cl / previous.cd
    final_ld = final.cl / final.cd
    relative_ld_improvement = (final_ld - previous_ld) / previous_ld
    if require_lift_to_drag_improvement and not final_ld > previous_ld:
        reasons.append("lift_to_drag_not_improved")
    if audit.get("step_reduced_to_zero"):
        reasons.append("fluent_step_reduced_to_zero")
    if validation.get("status") != "PASS":
        reasons.append("candidate_case_data_validation_failed")
    final_cell_volume = validation.get("last_reported_cell_volume", audit.get("last_reported_cell_volume"))
    if isinstance(final_cell_volume, (int, float)):
        if float(final_cell_volume) <= 0.0:
            reasons.append("final_cell_volume_not_positive")
    elif validation.get("status") != "PASS" or validation.get("negative_volume_in_validation"):
        reasons.append("final_cell_volume_not_verified")
    final_oq = validation.get("minimum_orthogonal_quality")
    if not isinstance(final_oq, (int, float)) or float(final_oq) < required_orthogonal_quality:
        reasons.append("final_orthogonal_quality_below_optimizer_gate")
    if shape_report and shape_report.get("status") == "FAIL":
        reasons.extend(str(item) for item in shape_report.get("hard_failures", []))
    negative_history = bool(audit.get("invalid_morphing"))
    if negative_history and not accept_recovered:
        reasons.append("negative_volume_history_not_allowed")
    accepted = not reasons
    status = ("RECOVERED_PASS" if negative_history else "CLEAN_PASS") if accepted else (
        "FAIL_LD_GATE" if "lift_to_drag_not_improved" in reasons else "FAIL"
    )
    return {
        "status": status,
        "accepted": accepted,
        "recovered": accepted and negative_history,
        "relative_drag_improvement_from_previous": relative_improvement,
        "relative_drag_improvement_from_original": (original.cd - final.cd) / original.cd,
        "lift_ratio_to_original": final.cl / original.cl,
        "minimum_lift_ratio": resolved_lift_ratio,
        "original_lift_to_drag": original_ld,
        "previous_lift_to_drag": previous_ld,
        "final_lift_to_drag": final_ld,
        "relative_lift_to_drag_improvement_from_previous": relative_ld_improvement,
        "relative_lift_to_drag_improvement_from_original": (final_ld - original_ld) / original_ld,
        "reasons": list(dict.fromkeys(reasons)),
        "shape_warnings": list((shape_report or {}).get("warnings", [])),
    }


def design_convergence_state(improvements: list[float], threshold: float, consecutive_steps: int) -> dict[str, Any]:
    trailing = improvements[-consecutive_steps:] if consecutive_steps > 0 else []
    converged = len(trailing) == consecutive_steps and all(value < threshold for value in trailing)
    return {"converged": converged, "threshold": threshold, "required_consecutive_steps": consecutive_steps, "trailing_improvements": trailing}


def performance_target_state(
    original: Coefficients,
    final: Coefficients,
    *,
    minimum_cumulative_cd_reduction: float,
    minimum_cumulative_ld_improvement: float,
    minimum_lift_ratio: float,
) -> dict[str, Any]:
    values = (original.cd, original.cl, final.cd, final.cl)
    if any(value is None or not math.isfinite(float(value)) for value in values):
        return {
            "status": "FAIL",
            "achieved": False,
            "errors": ["missing_force_coefficients"],
        }
    assert original.cd is not None and original.cl is not None and final.cd is not None and final.cl is not None
    original_ld = original.cl / original.cd
    final_ld = final.cl / final.cd
    cd_reduction = (original.cd - final.cd) / original.cd
    ld_improvement = (final_ld - original_ld) / original_ld
    lift_ratio = final.cl / original.cl
    def meets(value: float, threshold: float) -> bool:
        return value >= threshold or math.isclose(value, threshold, rel_tol=1.0e-12, abs_tol=1.0e-15)

    gates = {
        "cumulative_cd_reduction": meets(cd_reduction, float(minimum_cumulative_cd_reduction)),
        "cumulative_lift_to_drag_improvement": meets(ld_improvement, float(minimum_cumulative_ld_improvement)),
        "minimum_lift_ratio": meets(lift_ratio, float(minimum_lift_ratio)),
    }
    achieved = all(gates.values())
    return {
        "status": "PASS" if achieved else "INCOMPLETE",
        "achieved": achieved,
        "original_lift_to_drag": original_ld,
        "final_lift_to_drag": final_ld,
        "cumulative_cd_reduction": cd_reduction,
        "cumulative_lift_to_drag_improvement": ld_improvement,
        "lift_ratio_to_original": lift_ratio,
        "targets": {
            "minimum_cumulative_cd_reduction": float(minimum_cumulative_cd_reduction),
            "minimum_cumulative_lift_to_drag_improvement": float(minimum_cumulative_ld_improvement),
            "minimum_lift_ratio": float(minimum_lift_ratio),
        },
        "gates": gates,
        "errors": [],
    }


def design_completion_state(
    *,
    performance_targets_enabled: bool,
    performance: dict[str, Any] | None,
    convergence: dict[str, Any],
    accepted_step_limit_reached: bool,
) -> dict[str, Any]:
    """Resolve cycle completion without allowing ordinary convergence to bypass performance targets."""
    if not performance_targets_enabled:
        if convergence.get("converged"):
            return {"action": "STOP", "status": "PASS", "completion_reason": "relative_drag_improvement_converged"}
        if accepted_step_limit_reached:
            return {"action": "STOP", "status": "PASS", "completion_reason": "maximum_accepted_design_steps"}
        return {"action": "CONTINUE", "status": None, "completion_reason": None}
    if performance and performance.get("achieved"):
        return {"action": "STOP", "status": "PASS", "completion_reason": "cumulative_performance_targets_achieved"}
    if convergence.get("converged"):
        return {
            "action": "STOP",
            "status": "INCOMPLETE_PERFORMANCE_TARGET",
            "completion_reason": "ordinary_convergence_before_cumulative_performance_targets",
        }
    if accepted_step_limit_reached:
        return {
            "action": "STOP",
            "status": "INCOMPLETE_PERFORMANCE_TARGET",
            "completion_reason": "maximum_design_cycles_before_cumulative_performance_targets",
        }
    return {"action": "CONTINUE", "status": None, "completion_reason": None}


def render_optimization_report(summary: dict[str, Any]) -> str:
    result = summary.get("adjoint_result", {})
    baseline = result.get("baseline", {})
    final = result.get("final") or baseline
    accepted = result.get("accepted_steps", [])
    cd0, cl0 = baseline.get("cd"), baseline.get("cl")
    cdf, clf = final.get("cd"), final.get("cl")
    def fmt(value: Any) -> str:
        return f"{float(value):.10g}" if isinstance(value, (int, float)) else "无"

    lines = [
        "# Fluent 翼型优化报告",
        "",
        (
            "- v2 状态："
            f"技术=`{summary.get('technical_status', 'UNKNOWN')}`，"
            f"设计=`{summary.get('design_status', 'UNKNOWN')}`，"
            f"验收=`{summary.get('acceptance_status', 'UNKNOWN')}`，"
            f"终止=`{summary.get('termination_reason', 'UNKNOWN')}`"
        ),
        f"- 运行目录：`{summary.get('run_dir', '')}`",
        f"- 请求/实际运行名：`{summary.get('requested_run_name') or '自动时间戳'}` / `{summary.get('resolved_run_name', '')}`",
        f"- 接受的设计步数：{len(accepted)}",
        f"- 是否在失败尝试后恢复并完成：{'是' if result.get('recovered_from_failed_attempts') else '否'}",
        f"- 是否采用含瞬态负体积历史的结果：{'是' if result.get('accepted_recovered_attempts') else '否'}",
        f"- 完成原因：`{result.get('completion_reason', 'unknown')}`",
        f"- 优化方案：`{json.dumps(summary.get('optimization_profile'), ensure_ascii=False)}`",
        f"- 累计性能目标：`{json.dumps(result.get('performance_target'), ensure_ascii=False)}`",
        f"- Fluent/PyFluent 版本：`{json.dumps((result.get('runtime_resolution') or {}).get('versions'), ensure_ascii=False)}`",
        f"- 运行时目标映射：`{json.dumps((result.get('runtime_resolution') or {}).get('objective_mapping'), ensure_ascii=False)}`",
        f"- 运行时形变器：`{json.dumps((result.get('runtime_resolution') or {}).get('morpher'), ensure_ascii=False)}`",
        f"- 前后缘锚定：`{json.dumps((result.get('runtime_resolution') or {}).get('shape_anchors'), ensure_ascii=False)}`",
        f"- 厚度约束：`{json.dumps((result.get('runtime_resolution') or {}).get('thickness_constraint'), ensure_ascii=False)}`",
        f"- 运行时质量门槛：`{json.dumps((result.get('runtime_resolution') or {}).get('minimum_orthogonal_quality'), ensure_ascii=False)}`",
        f"- 控制点请求/读回：`{json.dumps((result.get('runtime_resolution') or {}).get('control_points'), ensure_ascii=False)}`",
        f"- 候选接受策略：`{json.dumps((result.get('runtime_resolution') or {}).get('acceptance_policy'), ensure_ascii=False)}`",
        "",
        "## 翼型形变保护",
        "",
        f"- 档位：`{(summary.get('shape_guard') or result.get('shape_guard') or {}).get('profile', 'balanced')}`",
        f"- 预警比例：{fmt((summary.get('shape_guard') or result.get('shape_guard') or {}).get('warning_fraction'))}",
        f"- 预设门槛：`{json.dumps((summary.get('shape_guard') or result.get('shape_guard') or {}).get('preset_thresholds', {}), ensure_ascii=False)}`",
        f"- 高级覆盖：`{json.dumps((summary.get('shape_guard') or result.get('shape_guard') or {}).get('overrides', {}), ensure_ascii=False)}`",
        f"- 最终生效门槛：`{json.dumps((summary.get('shape_guard') or result.get('shape_guard') or {}).get('thresholds', {}), ensure_ascii=False)}`",
        "",
        "## 升阻力对比",
        "",
        "| 状态 | Cd | Cl | Cl/Cd |",
        "|---|---:|---:|---:|",
    ]
    ratio0 = cl0 / cd0 if isinstance(cl0, (int, float)) and isinstance(cd0, (int, float)) and cd0 else None
    ratiof = clf / cdf if isinstance(clf, (int, float)) and isinstance(cdf, (int, float)) and cdf else None
    lines.extend((f"| 原始基准 | {fmt(cd0)} | {fmt(cl0)} | {fmt(ratio0)} |", f"| 最终结果 | {fmt(cdf)} | {fmt(clf)} | {fmt(ratiof)} |"))
    if isinstance(cd0, (int, float)) and isinstance(cdf, (int, float)) and cd0:
        lines.append(f"\n阻力变化：{(cdf - cd0) / cd0 * 100.0:.6f}%")
    if isinstance(cl0, (int, float)) and isinstance(clf, (int, float)) and cl0:
        lines.append(f"\n升力变化：{(clf - cl0) / cl0 * 100.0:.6f}%")
    if isinstance(ratio0, (int, float)) and isinstance(ratiof, (int, float)) and ratio0:
        lines.append(f"\n升阻比变化：{(ratiof - ratio0) / ratio0 * 100.0:.6f}%")
    primary = summary.get("primary_mesh", {})
    revalidation = summary.get("final_revalidation") or (accepted[-1].get("candidate_validation", {}) if accepted else {})
    required_oq = revalidation.get("required_orthogonal_quality")
    lines.extend(
        (
            "",
            "## 网格与最终复读",
            "",
            f"- 初始网格：{primary.get('fluent_quadrilateral_cells', '无')} 个四边形、{primary.get('fluent_triangular_cells', '无')} 个三角形，"
            f"最低正交质量 {fmt(primary.get('minimum_orthogonal_quality'))}，最大长宽比 {fmt(primary.get('maximum_aspect_ratio'))}",
            f"- 最终网格：{revalidation.get('quadrilateral_cells', '无')} 个四边形、{revalidation.get('triangular_cells', '无')} 个三角形，"
            f"最低正交质量 {fmt(revalidation.get('minimum_orthogonal_quality'))}（门槛 {fmt(required_oq)}），最大长宽比 {fmt(revalidation.get('maximum_aspect_ratio'))}",
            f"- 正式 case+data 新会话复读：`{revalidation.get('status', '未单独执行')}`；最终负体积：{'是' if revalidation.get('negative_volume') else '否'}",
        )
    )
    if isinstance(revalidation.get("maximum_aspect_ratio"), (int, float)) and float(revalidation["maximum_aspect_ratio"]) > 1000.0:
        lines.append("- 警告：最终最大长宽比超过 wake/出口目标 1000，但不属于本次最终硬门槛；正交质量仍高于优化器门槛。")
    lines.extend(("", "## 已接受设计步", ""))
    if not accepted:
        lines.append("没有通过最终候选门槛的设计步；保留原始基准解。")
    for step in accepted:
        coeff = step.get("final", {})
        gate = step.get("candidate_gate", {})
        lines.append(
            f"- 第 {step.get('cycle')} 步 `{gate.get('status')}`：Cd={fmt(coeff.get('cd'))}，Cl={fmt(coeff.get('cl'))}，"
            f"Cl/Cd={fmt(gate.get('final_lift_to_drag'))}，Cl 保留率={float(gate.get('lift_ratio_to_original', 0.0)) * 100.0:.6f}%，"
            f"相对上一步阻力改善={float(gate.get('relative_drag_improvement_from_previous', 0.0)) * 100.0:.6f}%，"
            f"累计阻力改善={float(gate.get('relative_drag_improvement_from_original', 0.0)) * 100.0:.6f}%，"
            f"相对上一步升阻比改善={float(gate.get('relative_lift_to_drag_improvement_from_previous', 0.0)) * 100.0:.6f}%"
        )
    lines.extend(("", "## 形变与修复审计", ""))
    for attempt in result.get("attempts", []):
        audit = attempt.get("transcript_audit", {})
        reasons = ", ".join(attempt.get("candidate_gate", {}).get("reasons", [])) or "无"
        shape = attempt.get("shape_guard_validation") or attempt.get("shape_guard_quick") or {}
        anchor = attempt.get("shape_anchor_validation") or attempt.get("shape_anchor_quick") or {}
        objective_runtime = ((attempt.get("runtime_resolution") or {}).get("objective_mapping") or {}).get("runtime_summary") or {}
        lines.append(
            f"- 周期 {attempt.get('cycle')} / `{attempt.get('profile', {}).get('name', 'unknown')}`："
            f"{attempt.get('status')}，负体积 {audit.get('negative_cell_volume_count', 0)} 次，"
            f"最小体积 {fmt(audit.get('minimum_reported_cell_volume'))}，最大位移 {fmt(audit.get('maximum_reported_boundary_displacement'))} m，"
            f"形变门槛 `{shape.get('status', '未检查')}`（{', '.join(shape.get('hard_failures', []) + shape.get('warnings', [])) or '无告警'}），"
            f"锚区 `{anchor.get('status', '未检查')}`，目标绑定 `{objective_runtime.get('status', '未检查')}`，原因：{reasons}"
        )
    rejected = [
        attempt for attempt in result.get("attempts", [])
        if isinstance(attempt.get("final", {}).get("cd"), (int, float))
        and math.isfinite(float(attempt["final"]["cd"]))
        and not attempt.get("candidate_gate", {}).get("accepted")
    ]
    if rejected:
        best = min(rejected, key=lambda item: float(item["final"]["cd"]))
        lines.extend(
            (
                "",
                "## 最佳未采用候选",
                "",
                f"- 档位：`{best.get('profile', {}).get('name')}`",
                f"- Cd={fmt(best.get('final', {}).get('cd'))}，Cl={fmt(best.get('final', {}).get('cl'))}",
                f"- 未采用原因：{', '.join(best.get('candidate_gate', {}).get('reasons', []))}",
            )
        )
    final_checkpoint = result.get("final_checkpoint") or {}
    baseline_checkpoint = result.get("baseline_checkpoint") or {}
    accepted_transcript = accepted[-1].get("transcript_path") if accepted else None
    validation_transcript = revalidation.get("transcript") or revalidation.get("transcript_path")
    lines.extend(
        (
            "",
            "## 文件",
            "",
            f"- 初始网格：`{primary.get('mesh_path')}`",
            f"- 基准 case：`{baseline_checkpoint.get('case')}`",
            f"- 基准 data：`{baseline_checkpoint.get('data')}`",
            f"- 最终 case：`{final_checkpoint.get('case')}`",
            f"- 最终 data：`{final_checkpoint.get('data')}`",
            f"- 正式导出：`{result.get('optimized_export_dir')}`",
            f"- 最终采用尝试 transcript：`{accepted_transcript}`",
            f"- 正式导出复读 transcript：`{validation_transcript}`",
        )
    )
    legacy_status = summary.get("legacy_engine_status") or summary.get("status")
    if legacy_status == "INCOMPLETE_REPAIR_EXHAUSTED":
        lines.extend(("", "## 剩余阻塞", "", "所有有限标准/修复档位均未产生新的有效设计步，优化不完整；上表最终结果为最后一个有效检查点。"))
    if legacy_status == "INCOMPLETE_SHAPE_GUARD_EXHAUSTED":
        lines.extend(("", "## 剩余阻塞", "", "所有有限候选均越过翼型形变硬门槛，已自动回退并保留最后有效检查点；未输出被拒绝候选为正式 optimized 结果。"))
    if legacy_status == "INCOMPLETE_PERFORMANCE_TARGET":
        lines.extend(
            (
                "",
                "## 剩余阻塞",
                "",
                "最后有效检查点满足逐步安全门槛，但有限档位未达到累计 Cd/L/D 目标；本次结果不得提升为正式多周期默认方案。",
            )
        )
    if any(attempt.get("status") == "FAIL_OBJECTIVE_RUNTIME_BINDING" for attempt in result.get("attempts", [])):
        lines.extend(
            (
                "",
                "## 根因更正",
                "",
                "本次运行确认 Fluent 优化器实际 Cd/Cl 绑定与 Python 设置视图不一致；此前将异常大形变归因于厚度限制的结论已被运行时摘要证据推翻。",
            )
        )
    return "\n".join(lines) + "\n"


def ensight_case_candidates(prefix: Path) -> list[Path]:
    return [prefix.with_suffix(".case"), prefix.with_suffix(".encas")]


def ensight_family_files(prefix: Path) -> list[Path]:
    return sorted(prefix.parent.glob(prefix.name + ".*"))


def ensure_ensight_case_alias(prefix: Path) -> Path | None:
    case_path = prefix.with_suffix(".case")
    encas_path = prefix.with_suffix(".encas")
    if case_path.exists() and case_path.stat().st_size > 0:
        return case_path
    if encas_path.exists() and encas_path.stat().st_size > 0:
        shutil.copy2(encas_path, case_path)
        return case_path
    return None


def ensight_case_has_variables(prefix: Path, variables: list[str], *, require_all: bool) -> bool:
    case_path = next((path for path in ensight_case_candidates(prefix) if path.exists() and path.stat().st_size > 0), None)
    if case_path is None:
        return False
    text = case_path.read_text(encoding="utf-8", errors="ignore").lower().replace("-", "_")
    expected = [str(variable).lower().replace("-", "_") for variable in variables]
    if not expected:
        return True
    checks = []
    for variable in expected:
        if variable == "velocity_magnitude":
            checks.append("velocity_magnitude" in text)
        elif variable in {"x_velocity", "y_velocity"}:
            checks.append(variable in text)
        elif variable in {"shape_sensitivity", "surface_sensitivity"}:
            checks.append(variable in text or "sensitivity" in text)
        else:
            checks.append(variable in text)
    return all(checks) if require_all else any(checks)


def export_failure_status(error: str, *, required: bool, optional_unavailable_if_disallowed: bool) -> str:
    if optional_unavailable_if_disallowed and "disallowed entries" in error.lower():
        return "SKIP_UNAVAILABLE"
    return "FAIL" if required else "OPTIONAL_FAIL"


def repair_branch_guidance(status: str, adjoint_result: dict[str, Any] | None = None, primary_mesh: dict[str, Any] | None = None) -> dict[str, Any]:
    repair_root = Path("ai_contract") / "repair_prompts" / "skills"
    triggered = status not in {"PASS", "DRY_RUN"}
    result = adjoint_result or {}
    mesh = primary_mesh or {}
    recommended = [str(repair_root / "REPAIR_ROUTER_PROMPT.md")]
    lowered_status = status.lower()
    failures = " ".join(str(item.get("label", "")) + " " + str(item.get("error", "")) for item in result.get("failures", []) if isinstance(item, dict)).lower()
    export_issues = any(
        item.get("status") in {"FAIL", "OPTIONAL_FAIL", "SKIP_UNAVAILABLE"} or "disallowed" in str(item.get("error", "")).lower()
        for item in result.get("exports", [])
        if isinstance(item, dict)
    )
    if "negative_volume" in lowered_status or "negative volume" in failures:
        recommended.append(str(repair_root / "NEGATIVE_VOLUME_REPAIR_SKILL.md"))
    if "export" in lowered_status or "export" in failures or export_issues:
        recommended.append(str(repair_root / "PYFLUENT_EXPORT_COMPATIBILITY_SKILL.md"))
    if "mesh" in lowered_status or float(mesh.get("maximum_aspect_ratio") or 0.0) > 30000.0:
        recommended.append(str(repair_root / "CGRID_STANDARD_COMPATIBILITY_REVIEW.md"))
        recommended.append(str(repair_root / "C型网格生成与优化标准.md"))
    if "stage1" in lowered_status or result.get("stage1_gate_reconciliation"):
        recommended.append(str(repair_root / "STAGE1_GATE_REPAIR_SKILL.md"))
    return {
        "triggered": triggered,
        "read_only_when_triggered": True,
        "entrypoint": str(repair_root / "REPAIR_ROUTER_PROMPT.md"),
        "recommended_files": list(dict.fromkeys(recommended)),
    }


def prompt_string(label: str, default: str | None = None, required: bool = False) -> str:
    suffix = f" [{default}]" if default not in {None, ""} else ""
    while True:
        try:
            value = input(f"{label}{suffix}: ").strip()
        except EOFError:
            if default not in {None, ""}:
                return str(default)
            if not required:
                return ""
            raise
        if value:
            return value
        if default not in {None, ""}:
            return str(default)
        if not required:
            return ""
        print("This value is required.")


def prompt_float(label: str, default: float) -> float:
    while True:
        try:
            raw = input(f"{label} [{default}]: ").strip()
        except EOFError:
            return float(default)
        if not raw:
            return float(default)
        try:
            return float(raw)
        except ValueError:
            print("Please enter a number.")


def configure_normal_inputs(cfg: dict[str, Any], *, interactive: bool) -> dict[str, Any]:
    if not interactive:
        return cfg
    airfoil_default = deep_get(cfg, "airfoil_dat", "")
    airfoil_dat = prompt_string("Airfoil DAT path", airfoil_default, required=True)
    deep_set(cfg, "airfoil_dat", airfoil_dat)
    deep_set(cfg, "flow.velocity_m_s", prompt_float("Flow speed, m/s", float(deep_get(cfg, "flow.velocity_m_s", 32.5))))
    deep_set(cfg, "flow.altitude_m", prompt_float("Altitude, m", float(deep_get(cfg, "flow.altitude_m", 0.0))))
    deep_set(cfg, "flow.angle_of_attack_deg", prompt_float("Angle of attack, deg", float(deep_get(cfg, "flow.angle_of_attack_deg", 2.0))))
    deep_set(cfg, "flow.chord_m", prompt_float("Chord, m", float(deep_get(cfg, "flow.chord_m", 1.0))))
    deep_set(cfg, "flow.target_y_plus", prompt_float("Target y+", float(deep_get(cfg, "flow.target_y_plus", 1.0))))
    deep_set(
        cfg,
        "advanced_settings.design_tool.thickness_constraint.clearance_percent_of_baseline_max_thickness",
        prompt_float(
            "Direct thickness audit clearance, percent of baseline max thickness",
            float(deep_get(cfg, "advanced_settings.design_tool.thickness_constraint.clearance_percent_of_baseline_max_thickness", 5.0)),
        ),
    )
    print("推荐气动设置：Cd 目标 -0.15%，Fluent 内部 Cl 下限 99.9%，最终 Cl 门槛 99.8%。")
    deep_set(
        cfg,
        "advanced_settings.optimizer.drag_step_percent",
        prompt_float(
            "Cd 目标变化百分比（推荐 -0.15）",
            float(deep_get(cfg, "advanced_settings.optimizer.drag_step_percent", -0.15)),
        ),
    )
    lift_bound_percent = prompt_float(
        "Fluent 内部 Cl 下限百分比（推荐 99.9）",
        100.0 * float(deep_get(cfg, "advanced_settings.optimizer.lift_runtime_bound_ratio", 0.999)),
    )
    minimum_lift_percent = prompt_float(
        "最终 Cl 验收门槛百分比（推荐 99.8）",
        100.0 * float(deep_get(cfg, "completion.minimum_lift_ratio", 0.998)),
    )
    deep_set(cfg, "advanced_settings.optimizer.lift_runtime_bound_ratio", round(lift_bound_percent / 100.0, 12))
    deep_set(cfg, "completion.minimum_lift_ratio", round(minimum_lift_percent / 100.0, 12))
    deep_set(cfg, "advanced_settings.optimizer.objective_strategy", "drag-with-lift-bound")
    warnings = validate_aerodynamic_controls(cfg)
    cfg.setdefault("_interaction", {}).setdefault("validation_warnings", []).extend(warnings)
    for warning in warnings:
        print(f"警告：{warning}")
    run_name = prompt_string("运行名称（可选，留空使用时间戳）", str(cfg.get("run_name", "")), required=False)
    if run_name:
        validate_run_name(run_name)
    cfg["run_name"] = run_name
    current_profile = str(deep_get(cfg, "shape_guard.profile", "balanced"))
    profile_map = {"1": "conservative", "2": "balanced", "3": "aggressive"}
    print("形变保护档位：1. 保守  2. 平衡（默认）  3. 激进")
    while True:
        profile_default = {value: key for key, value in profile_map.items()}.get(current_profile, "2")
        raw_profile = prompt_string("选择形变保护档位", profile_default, required=False).lower()
        profile = profile_map.get(raw_profile, raw_profile if raw_profile in profile_map.values() else "")
        if profile:
            deep_set(cfg, "shape_guard.profile", profile)
            break
        print(f"请输入 1、2、3 或档位英文名（当前配置：{current_profile}）。")
    return cfg
