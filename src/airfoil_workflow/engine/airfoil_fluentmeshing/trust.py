from __future__ import annotations

import hashlib
import json
import math
import statistics
import uuid
from typing import Any, Iterable


SCHEMA_VERSION = 2
TECHNICAL_STATUSES = {"DRY_RUN", "COMPLETED", "FAILED", "CANCELLED"}
DESIGN_STATUSES = {
    "NOT_RUN",
    "NO_MOVE",
    "INFEASIBLE",
    "NO_STATISTICAL_IMPROVEMENT",
    "STATISTICALLY_IMPROVED",
    "TARGET_ACHIEVED",
}
ACCEPTANCE_STATUSES = {"UNVERIFIED", "REJECTED", "ACCEPTED"}
TERMINATION_REASONS = {
    "TARGET_ACHIEVED",
    "CONVERGED",
    "BUDGET_EXHAUSTED",
    "USER_STOP",
    "FAILURE",
    "CANCELLED",
}


def resolve_run_id(value: str | None = None) -> str:
    """Return a canonical UUID4 run id, validating an id supplied by a caller."""
    if value:
        return str(uuid.UUID(str(value)))
    return str(uuid.uuid4())


def legacy_display_status(summary: dict[str, Any]) -> str:
    if int(summary.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        return "LEGACY_UNVERIFIED"
    return str(summary.get("acceptance_status") or "UNVERIFIED")


def build_v2_outcome(
    internal_status: str,
    result: dict[str, Any] | None = None,
    *,
    dry_run: bool = False,
    cancelled: bool = False,
) -> dict[str, str]:
    """Map engine details into orthogonal technical, design, and acceptance states."""
    result = result or {}
    status = str(internal_status or "").upper()
    if cancelled:
        return {
            "technical_status": "CANCELLED",
            "design_status": "NOT_RUN",
            "acceptance_status": "REJECTED",
            "termination_reason": "CANCELLED",
        }
    if dry_run or status == "DRY_RUN":
        return {
            "technical_status": "DRY_RUN",
            "design_status": "NOT_RUN",
            "acceptance_status": "UNVERIFIED",
            "termination_reason": "USER_STOP",
        }
    technical_failure_prefixes = (
        "FAIL_RUNTIME",
        "FAIL_MESH",
        "FAIL_BASELINE",
        "FAIL_OBJECTIVE_RUNTIME_BINDING",
    )
    if status.startswith(technical_failure_prefixes):
        return {
            "technical_status": "FAILED",
            "design_status": "NOT_RUN",
            "acceptance_status": "REJECTED",
            "termination_reason": "FAILURE",
        }

    attempts = list(result.get("attempts") or [])
    accepted_steps = list(result.get("accepted_steps") or [])
    reasons = [
        str(reason)
        for attempt in attempts
        for reason in (attempt.get("candidate_gate") or {}).get("reasons", [])
    ]
    performance = result.get("performance_target") or {}
    if performance.get("achieved"):
        design_status = "TARGET_ACHIEVED"
        termination = "TARGET_ACHIEVED"
    elif accepted_steps:
        design_status = "STATISTICALLY_IMPROVED"
        completion = str(result.get("completion_reason") or "").lower()
        termination = "BUDGET_EXHAUSTED" if "maximum" in completion or "budget" in completion else "CONVERGED"
    elif any(reason in {"geometry_not_changed", "design_iteration_zero", "geometry_displacement_below_threshold"} for reason in reasons):
        design_status = "NO_MOVE"
        termination = "CONVERGED"
    elif any("noise" in reason or "confidence" in reason or "drag_not_reduced" in reason for reason in reasons):
        design_status = "NO_STATISTICAL_IMPROVEMENT"
        termination = "CONVERGED"
    else:
        design_status = "INFEASIBLE"
        completion = str(result.get("completion_reason") or "").lower()
        termination = (
            "BUDGET_EXHAUSTED"
            if any(token in completion for token in ("maximum", "budget", "exhausted"))
            else "CONVERGED"
        )

    qualification = result.get("numerical_qualification") or {}
    cfd_qualification = result.get("cfd_qualification") or {}
    production_qualified = (
        qualification.get("qualification") == "QUALIFIED"
        and cfd_qualification.get("qualification") == "QUALIFIED"
    )
    if accepted_steps and production_qualified:
        acceptance = "ACCEPTED"
    elif accepted_steps:
        acceptance = "UNVERIFIED"
    else:
        acceptance = "REJECTED"
    return {
        "technical_status": "COMPLETED",
        "design_status": design_status,
        "acceptance_status": acceptance,
        "termination_reason": termination,
    }


def validate_v2_outcome(summary: dict[str, Any]) -> None:
    if int(summary.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        raise ValueError("summary schema_version must be 2")
    resolve_run_id(str(summary.get("run_id") or ""))
    if summary.get("technical_status") not in TECHNICAL_STATUSES:
        raise ValueError("invalid technical_status")
    if summary.get("design_status") not in DESIGN_STATUSES:
        raise ValueError("invalid design_status")
    if summary.get("acceptance_status") not in ACCEPTANCE_STATUSES:
        raise ValueError("invalid acceptance_status")
    if summary.get("termination_reason") not in TERMINATION_REASONS:
        raise ValueError("invalid termination_reason")


def exit_code_for_summary(summary: dict[str, Any]) -> int:
    validate_v2_outcome(summary)
    technical = summary["technical_status"]
    if technical == "CANCELLED":
        return 130
    if technical == "FAILED":
        return 1
    if technical == "DRY_RUN":
        return 0
    return 0 if summary["acceptance_status"] == "ACCEPTED" else 2


def _normalised_points(snapshot: dict[str, Any]) -> list[list[float]]:
    points = snapshot.get("points") or []
    clean = [[float(point[0]), float(point[1])] for point in points if isinstance(point, (list, tuple)) and len(point) >= 2]
    if len(clean) < 2:
        return []
    xmin = min(point[0] for point in clean)
    xmax = max(point[0] for point in clean)
    chord = xmax - xmin
    if not math.isfinite(chord) or chord <= 0.0:
        return []
    return [[round((x - xmin) / chord, 12), round(y / chord, 12)] for x, y in clean]


def canonical_geometry_hash(snapshot: dict[str, Any]) -> str | None:
    points = _normalised_points(snapshot)
    if not points:
        return None
    payload = json.dumps(points, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def geometry_change_gate(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    maximum_design_iteration: int | None,
    design_variable_status: str,
    minimum_displacement_over_chord: float = 1.0e-6,
) -> dict[str, Any]:
    baseline_points = _normalised_points(baseline)
    candidate_points = _normalised_points(candidate)
    reasons: list[str] = []
    baseline_hash = canonical_geometry_hash(baseline)
    candidate_hash = canonical_geometry_hash(candidate)
    if not baseline_points or not candidate_points:
        reasons.append("missing_geometry_evidence")
        displacement = None
    else:
        count = min(len(baseline_points), len(candidate_points))
        displacement = max(
            math.dist(baseline_points[index], candidate_points[index])
            for index in range(count)
        )
        if len(baseline_points) != len(candidate_points):
            reasons.append("geometry_point_count_changed")
    if baseline_hash is not None and baseline_hash == candidate_hash:
        reasons.append("geometry_not_changed")
    if displacement is not None and displacement < minimum_displacement_over_chord:
        reasons.append("geometry_displacement_below_threshold")
    if maximum_design_iteration is None or int(maximum_design_iteration) < 1:
        reasons.append("design_iteration_zero")
    if str(design_variable_status).upper() != "ACTIVE":
        reasons.append("design_variables_not_active")
    return {
        "status": "PASS" if not reasons else "FAIL",
        "baseline_hash": baseline_hash,
        "candidate_hash": candidate_hash,
        "maximum_design_iteration": maximum_design_iteration,
        "design_variable_status": str(design_variable_status).upper(),
        "maximum_displacement_over_chord": displacement,
        "minimum_displacement_over_chord": float(minimum_displacement_over_chord),
        "reasons": reasons,
    }


def coefficient_representative(samples: Iterable[dict[str, Any]], *, tail_count: int = 3) -> dict[str, Any]:
    usable = [sample for sample in samples if all(isinstance(sample.get(key), (int, float)) and math.isfinite(float(sample[key])) for key in ("cd", "cl"))]
    if len(usable) < tail_count:
        return {"status": "UNVERIFIED", "sample_count": len(usable), "required_sample_count": tail_count}
    tail = usable[-tail_count:]
    result: dict[str, Any] = {"status": "PASS", "sample_count": len(tail), "samples": tail}
    for key in ("cd", "cl"):
        values = [float(sample[key]) for sample in tail]
        mean = statistics.fmean(values)
        stdev = statistics.stdev(values) if len(values) > 1 else 0.0
        result[key] = {"mean": mean, "stdev": stdev, "standard_error": stdev / math.sqrt(len(values))}
    return result


def aa_noise_floor(
    cd_values: Iterable[float],
    *,
    engineering_floor: float = 5.0e-4,
    required_repeats: int = 5,
) -> dict[str, Any]:
    values = [float(value) for value in cd_values if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0.0]
    if not values:
        return {
            "qualification": "UNQUALIFIED",
            "repeat_count": 0,
            "required_repeats": required_repeats,
            "engineering_floor": engineering_floor,
            "noise_floor": engineering_floor,
            "errors": ["missing_aa_measurements"],
        }
    centre = statistics.fmean(values)
    deviations = [(value - centre) / centre for value in values]
    sigma_rel = statistics.stdev(deviations) if len(deviations) > 1 else 0.0
    max_abs = max(abs(value) for value in deviations)
    floor = max(float(engineering_floor), 3.0 * sigma_rel, max_abs)
    return {
        "qualification": "QUALIFIED" if len(values) >= required_repeats else "PROVISIONAL",
        "repeat_count": len(values),
        "required_repeats": required_repeats,
        "engineering_floor": float(engineering_floor),
        "relative_sigma": sigma_rel,
        "maximum_absolute_relative_deviation": max_abs,
        "noise_floor": floor,
        "cd_values": values,
    }


def improvement_confidence(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    noise_floor: float,
) -> dict[str, Any]:
    if baseline.get("status") != "PASS" or candidate.get("status") != "PASS":
        return {"status": "UNVERIFIED", "accepted": False, "reasons": ["unstable_coefficient_evidence"]}
    baseline_cd = float(baseline["cd"]["mean"])
    candidate_cd = float(candidate["cd"]["mean"])
    if baseline_cd <= 0.0:
        return {"status": "UNVERIFIED", "accepted": False, "reasons": ["invalid_baseline_drag"]}
    relative_improvement = (baseline_cd - candidate_cd) / baseline_cd
    combined_se = math.sqrt(float(baseline["cd"]["standard_error"]) ** 2 + float(candidate["cd"]["standard_error"]) ** 2)
    lower_95 = relative_improvement - 1.96 * combined_se / baseline_cd
    accepted = lower_95 > float(noise_floor)
    return {
        "status": "PASS" if accepted else "FAIL",
        "accepted": accepted,
        "relative_improvement": relative_improvement,
        "combined_standard_error": combined_se,
        "lower_95_relative_improvement": lower_95,
        "noise_floor": float(noise_floor),
        "reasons": [] if accepted else ["drag_improvement_confidence_below_noise_floor"],
    }


def yplus_qualification(values: Iterable[float], *, target_y_plus: float = 1.0) -> dict[str, Any]:
    clean = sorted(float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) >= 0.0)
    if not clean:
        return {"status": "UNVERIFIED", "errors": ["missing_wall_y_plus"]}
    index = max(0, math.ceil(0.95 * len(clean)) - 1)
    p95 = clean[index]
    limit = max(2.0 * float(target_y_plus), float(target_y_plus) + 1.0)
    return {
        "status": "PASS" if p95 <= limit else "FAIL",
        "count": len(clean),
        "minimum": clean[0],
        "mean": statistics.fmean(clean),
        "p95": p95,
        "maximum": clean[-1],
        "target_y_plus": float(target_y_plus),
        "p95_limit": limit,
        "warnings": ["maximum_wall_y_plus_above_5x_target"] if clean[-1] > 5.0 * float(target_y_plus) else [],
    }


def mass_balance_qualification(fluxes: dict[str, float], *, tolerance: float = 0.01) -> dict[str, Any]:
    clean = {str(name): float(value) for name, value in fluxes.items() if isinstance(value, (int, float)) and math.isfinite(float(value))}
    nonzero = [abs(value) for value in clean.values() if abs(value) > 0.0]
    if len(clean) < 2 or not nonzero:
        return {"status": "UNVERIFIED", "fluxes": clean, "errors": ["insufficient_mass_flux_evidence"]}
    net = sum(clean.values())
    reference = min(nonzero)
    relative_imbalance = abs(net) / reference
    return {
        "status": "PASS" if relative_imbalance < float(tolerance) else "FAIL",
        "fluxes": clean,
        "net_flux": net,
        "reference_flux": reference,
        "relative_imbalance": relative_imbalance,
        "tolerance": float(tolerance),
    }


def gci_three_grid(
    *,
    coarse_value: float,
    medium_value: float,
    fine_value: float,
    coarse_h: float,
    medium_h: float,
    fine_h: float,
    target_improvement: float,
) -> dict[str, Any]:
    values = [float(coarse_value), float(medium_value), float(fine_value)]
    spacings = [float(coarse_h), float(medium_h), float(fine_h)]
    if not all(math.isfinite(value) for value in [*values, *spacings]) or not coarse_h > medium_h > fine_h > 0.0:
        return {"status": "INCONCLUSIVE", "errors": ["invalid_grid_order_or_values"]}
    r32 = coarse_h / medium_h
    r21 = medium_h / fine_h
    if min(r32, r21) <= 1.2:
        return {"status": "INCONCLUSIVE", "refinement_ratios": {"coarse_medium": r32, "medium_fine": r21}, "errors": ["refinement_ratio_not_above_1p2"]}
    e32 = medium_value - coarse_value
    e21 = fine_value - medium_value
    if e32 == 0.0 or e21 == 0.0 or e32 * e21 <= 0.0:
        return {"status": "INCONCLUSIVE", "refinement_ratios": {"coarse_medium": r32, "medium_fine": r21}, "errors": ["non_monotonic_grid_sequence"]}
    error_ratio = abs(e32 / e21)

    def ratio_for_order(order: float) -> float:
        numerator = r21 ** order * (r32 ** order - 1.0)
        denominator = r21 ** order - 1.0
        return numerator / denominator

    lower_order = 1.0e-6
    upper_order = 20.0
    lower_ratio = ratio_for_order(lower_order)
    upper_ratio = ratio_for_order(upper_order)
    if not lower_ratio <= error_ratio <= upper_ratio:
        return {
            "status": "INCONCLUSIVE",
            "refinement_ratios": {"coarse_medium": r32, "medium_fine": r21},
            "error_ratio": error_ratio,
            "errors": ["no_positive_observed_order_for_unequal_refinement_ratios"],
        }
    for _ in range(100):
        middle = 0.5 * (lower_order + upper_order)
        if ratio_for_order(middle) < error_ratio:
            lower_order = middle
        else:
            upper_order = middle
    observed_order = 0.5 * (lower_order + upper_order)
    denominator = r21 ** max(observed_order, 1.0e-12) - 1.0
    if denominator <= 0.0 or fine_value == 0.0:
        return {"status": "INCONCLUSIVE", "errors": ["gci_denominator_invalid"]}
    extrapolated = fine_value + e21 / denominator
    fine_gci = 1.25 * abs(e21 / fine_value) / denominator
    limit = 0.2 * float(target_improvement)
    return {
        "status": "PASS" if fine_gci <= limit else "FAIL",
        "observed_order": observed_order,
        "error_ratio": error_ratio,
        "refinement_ratios": {"coarse_medium": r32, "medium_fine": r21},
        "extrapolated_value": extrapolated,
        "fine_grid_gci": fine_gci,
        "gci_limit": limit,
        "monotonic": True,
    }


def aggregate_grid_family(
    grids: Iterable[dict[str, Any]],
    *,
    target_cd_improvement: float,
) -> dict[str, Any]:
    records = [dict(item) for item in grids]
    if len(records) != 3:
        return {"status": "INCONCLUSIVE", "errors": ["exactly_three_grids_required"], "grids": records}
    try:
        for record in records:
            if "h" not in record:
                cells = float(record["cell_count"])
                record["h"] = 1.0 / math.sqrt(cells)
        records.sort(key=lambda item: float(item["h"]), reverse=True)
        kwargs = {
            "coarse_h": float(records[0]["h"]),
            "medium_h": float(records[1]["h"]),
            "fine_h": float(records[2]["h"]),
            "target_improvement": float(target_cd_improvement),
        }
        cd = gci_three_grid(
            coarse_value=float(records[0]["cd"]),
            medium_value=float(records[1]["cd"]),
            fine_value=float(records[2]["cd"]),
            **kwargs,
        )
        cl = gci_three_grid(
            coarse_value=float(records[0]["cl"]),
            medium_value=float(records[1]["cl"]),
            fine_value=float(records[2]["cl"]),
            **kwargs,
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        return {"status": "INCONCLUSIVE", "errors": [f"invalid_grid_record: {type(exc).__name__}: {exc}"], "grids": records}
    return {
        "status": "PASS" if cd.get("status") == "PASS" and cl.get("status") != "INCONCLUSIVE" else (
            "INCONCLUSIVE" if "INCONCLUSIVE" in {cd.get("status"), cl.get("status")} else "FAIL"
        ),
        "production_qualified": cd.get("status") == "PASS" and cl.get("status") != "INCONCLUSIVE",
        "target_cd_improvement": float(target_cd_improvement),
        "cd": cd,
        "cl": cl,
        "grids": records,
    }
