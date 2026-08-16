from __future__ import annotations

import copy
import math
import re
from typing import Any, Iterable

from airfoil_fluentmeshing.trust import mass_balance_qualification, yplus_qualification
from airfoil_fluentmeshing.mesh_policy import MAX_CELLS


def _normalise_token(value: Any) -> str:
    return str(value).strip().lower().replace("_", "-").replace(" ", "-")


def _is_close(value: Any, expected: float, *, relative_tolerance: float = 1.0e-6) -> bool:
    try:
        return math.isclose(float(value), float(expected), rel_tol=relative_tolerance, abs_tol=1.0e-12)
    except (TypeError, ValueError):
        return False


def physics_readback_qualification(readback: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """Validate values read back from Fluent after all required settings writes."""
    checks: dict[str, bool] = {
        "energy": readback.get("energy_enabled") is True,
        "sst": "sst" in _normalise_token(readback.get("viscous_model")),
        "ideal_gas": _normalise_token(readback.get("density_model")) in {"ideal-gas", "idealgas"},
        "sutherland": "sutherland" in _normalise_token(readback.get("viscosity_model")),
        "inlet_turbulence_intensity": _is_close(
            readback.get("inlet_turbulence_intensity"),
            float(expected.get("turbulence_intensity", 0.01)),
            relative_tolerance=1.0e-4,
        ),
        "reference_velocity": _is_close(readback.get("reference_velocity"), float(expected.get("velocity_m_s", 0.0))),
        "reference_density": _is_close(readback.get("reference_density"), float(expected.get("density_kg_m3", 0.0))),
        "reference_area": _is_close(readback.get("reference_area"), float(expected.get("reference_area_m2", 0.0))),
        "reference_length": _is_close(readback.get("reference_length"), float(expected.get("effective_chord_m", 0.0))),
    }
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "status": "PASS" if not failures else "FAIL",
        "checks": checks,
        "failures": failures,
        "readback": readback,
        "expected": expected,
        "fingerprint": {
            "solver_type": "pressure-based",
            "time": "steady",
            "energy": readback.get("energy_enabled"),
            "viscous_model": readback.get("viscous_model"),
            "density_model": readback.get("density_model"),
            "viscosity_model": readback.get("viscosity_model"),
            "inlet_turbulence_intensity": readback.get("inlet_turbulence_intensity"),
            "reference_velocity": readback.get("reference_velocity"),
            "reference_density": readback.get("reference_density"),
            "reference_area": readback.get("reference_area"),
            "reference_length": readback.get("reference_length"),
        },
    }


def residual_qualification(
    residuals: dict[str, float] | None,
    criteria: dict[str, float] | None,
) -> dict[str, Any]:
    residuals = residuals or {}
    criteria = criteria or {}
    if not residuals:
        return {"status": "UNVERIFIED", "residuals": {}, "criteria": criteria, "errors": ["missing_residual_readback"]}
    normalised_criteria = {_normalise_token(name): value for name, value in criteria.items()}
    checks: dict[str, bool] = {}
    for name, value in residuals.items():
        normalised_name = _normalise_token(name)
        limit = normalised_criteria.get(normalised_name)
        if limit is None:
            limit = next(
                (candidate for key, candidate in normalised_criteria.items() if key in normalised_name or normalised_name in key),
                None,
            )
        if limit is not None and isinstance(value, (int, float)):
            checks[name] = float(value) <= float(limit)
    if not checks:
        return {
            "status": "UNVERIFIED",
            "residuals": residuals,
            "criteria": criteria,
            "errors": ["no_residual_has_a_matching_criterion"],
        }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "status": "PASS" if not failed else "FAIL",
        "residuals": residuals,
        "criteria": criteria,
        "checks": checks,
        "failures": failed,
    }


def mesh_qualification(mesh: dict[str, Any]) -> dict[str, Any]:
    quality_gate = mesh.get("quality_gate") or {}
    minimum_oq = mesh.get("minimum_orthogonal_quality", quality_gate.get("minimum_fluent_orthogonal_quality_actual"))
    negative = bool(mesh.get("negative_volume_detected")) or int(mesh.get("negative_volume_cells", 0) or 0) > 0
    source_status = str(quality_gate.get("status") or mesh.get("status") or "").upper()
    cell_count = int(mesh.get("cell_count", mesh.get("fluent_quadrilateral_cells")) or 0)
    maximum_cells = int(
        (quality_gate.get("cell_budget") or {}).get("maximum")
        or (mesh.get("quality_policy") or {}).get("maximum_cells_hard")
        or MAX_CELLS
    )
    passed = (
        source_status in {"PASS", "WARNING", "PASS_WITH_WARNING"}
        and not negative
        and isinstance(minimum_oq, (int, float))
        and float(minimum_oq) > 0.01
        and cell_count <= maximum_cells
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "source_status": source_status,
        "minimum_orthogonal_quality": minimum_oq,
        "maximum_skewness": mesh.get("maximum_skewness", quality_gate.get("maximum_skewness_actual")),
        "negative_volume_detected": negative,
        "cell_count": cell_count,
        "maximum_cells": maximum_cells,
        "warnings": list(quality_gate.get("warnings") or []),
    }


def build_cfd_qualification(
    *,
    wall_y_plus: Iterable[float],
    boundary_mass_fluxes: dict[str, float],
    physics_readback: dict[str, Any],
    expected_physics: dict[str, Any],
    residuals: dict[str, float] | None,
    residual_criteria: dict[str, float] | None,
    force_stability: dict[str, Any],
    mesh: dict[str, Any],
    target_y_plus: float = 1.0,
    grid_convergence: dict[str, Any] | None = None,
    quick_validation: bool = False,
) -> dict[str, Any]:
    """Build the single evidence object used by production acceptance."""
    parts = {
        "wall_y_plus": yplus_qualification(wall_y_plus, target_y_plus=target_y_plus),
        "mass_balance": mass_balance_qualification(boundary_mass_fluxes),
        "physics_readback": physics_readback_qualification(physics_readback, expected_physics),
        "residuals": residual_qualification(residuals, residual_criteria),
        "force_stability": force_stability,
        "mesh_quality": mesh_qualification(mesh),
        "grid_convergence": grid_convergence or {"status": "NOT_RUN"},
    }
    required_names = ["wall_y_plus", "mass_balance", "physics_readback", "residuals", "force_stability", "mesh_quality"]
    failed = [name for name in required_names if str(parts[name].get("status", "")).upper() == "FAIL"]
    unverified = [name for name in required_names if str(parts[name].get("status", "")).upper() not in {"PASS", "WARNING"}]
    gci_status = str(parts["grid_convergence"].get("status", "NOT_RUN")).upper()
    if failed:
        qualification = "UNQUALIFIED"
    elif unverified or gci_status != "PASS" or quick_validation:
        qualification = "PROVISIONAL"
    else:
        qualification = "QUALIFIED"
    return {
        "qualification": qualification,
        "production_qualified": qualification == "QUALIFIED",
        "quick_validation": bool(quick_validation),
        "failed_components": failed,
        "unverified_components": unverified,
        "missing_production_components": ["grid_convergence"] if gci_status != "PASS" else [],
        **parts,
    }


def attach_grid_convergence(
    qualification: dict[str, Any],
    grid_convergence: dict[str, Any],
) -> dict[str, Any]:
    """Return a production qualification after attaching real three-grid evidence."""
    result = copy.deepcopy(qualification)
    result["grid_convergence"] = copy.deepcopy(grid_convergence)
    result["missing_production_components"] = (
        [] if str(grid_convergence.get("status", "")).upper() == "PASS" else ["grid_convergence"]
    )
    required_names = ["wall_y_plus", "mass_balance", "physics_readback", "residuals", "force_stability", "mesh_quality"]
    failed = [name for name in required_names if str((result.get(name) or {}).get("status", "")).upper() == "FAIL"]
    unverified = [
        name
        for name in required_names
        if str((result.get(name) or {}).get("status", "")).upper() not in {"PASS", "WARNING"}
    ]
    result["failed_components"] = failed
    result["unverified_components"] = unverified
    grid_status = str(grid_convergence.get("status", "")).upper()
    if failed:
        status = "UNQUALIFIED"
    elif unverified or grid_status != "PASS" or bool(result.get("quick_validation")):
        status = "PROVISIONAL"
    else:
        status = "QUALIFIED"
    result["qualification"] = status
    result["production_qualified"] = status == "QUALIFIED"
    return result


def flatten_numeric_values(value: Any) -> list[float]:
    """Flatten PyFluent field-data response shapes without depending on one API revision."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return [float(value)]
    if isinstance(value, dict):
        values: list[float] = []
        for item in value.values():
            values.extend(flatten_numeric_values(item))
        return values
    if isinstance(value, (list, tuple)):
        values = []
        for item in value:
            values.extend(flatten_numeric_values(item))
        return values
    try:
        if hasattr(value, "tolist"):
            return flatten_numeric_values(value.tolist())
    except Exception:
        pass
    return []


def read_setting(root: Any, dotted_paths: Iterable[str]) -> Any:
    errors: list[str] = []
    for dotted in dotted_paths:
        try:
            current = root
            for part in dotted.split("."):
                current = current[int(part)] if part.isdigit() else getattr(current, part)
            getter = getattr(current, "get_state", None)
            return getter() if callable(getter) else (current() if callable(current) else current)
        except Exception as exc:
            errors.append(f"{dotted}: {type(exc).__name__}: {exc}")
    return {"readback_error": " | ".join(errors)}


def collect_physics_readback(solver: Any, *, inlet_zone: str = "velocity_inlet") -> dict[str, Any]:
    settings = solver.settings
    inlet_root = settings.setup.boundary_conditions.velocity_inlet[inlet_zone]
    air = settings.setup.materials.fluid["air"]
    return {
        "solver_type": read_setting(settings, ["setup.general.solver.type", "setup.general.solver_type"]),
        "time": read_setting(settings, ["setup.general.solver.time"]),
        "energy_enabled": read_setting(settings, ["setup.models.energy.enabled"]),
        "viscous_model": read_setting(
            settings,
            ["setup.models.viscous.k_omega_model", "setup.models.viscous.model"],
        ),
        "density_model": read_setting(air, ["density.option"]),
        "viscosity_model": read_setting(air, ["viscosity.option"]),
        "inlet_turbulence_intensity": read_setting(
            inlet_root,
            ["turbulence.turbulent_intensity.value", "turbulence.turbulent_intensity"],
        ),
        "reference_velocity": read_setting(settings, ["setup.reference_values.velocity"]),
        "reference_density": read_setting(settings, ["setup.reference_values.density"]),
        "reference_area": read_setting(settings, ["setup.reference_values.area"]),
        "reference_length": read_setting(settings, ["setup.reference_values.length"]),
    }


def collect_wall_y_plus(solver: Any, wall_zone: str) -> list[float]:
    field_data = getattr(getattr(solver, "fields", None), "field_data", None) or getattr(solver, "field_data", None)
    if field_data is None:
        raise RuntimeError("PyFluent field data service is unavailable")
    errors: list[str] = []
    for field_name in ("y-plus", "wall-yplus", "wall-y-plus"):
        try:
            response = field_data.get_scalar_field_data(field_name=field_name, surfaces=[wall_zone])
            values = flatten_numeric_values(response)
            if values:
                return values
        except Exception as exc:
            errors.append(f"{field_name}: {type(exc).__name__}: {exc}")
    raise RuntimeError("unable to read wall y+ field: " + " | ".join(errors))


def locate_surface_scalar_hotspots(
    values: Iterable[float],
    vertices: Iterable[Any],
    faces: Iterable[Any],
    *,
    hotspot_count: int = 20,
) -> dict[str, Any]:
    """Map face scalar values to 2-D face centroids and normalized chord positions."""
    clean_values = [float(value) for value in values]
    clean_vertices = [(float(value[0]), float(value[1])) for value in vertices]
    clean_faces = list(faces)
    if not clean_vertices or len(clean_values) != len(clean_faces):
        return {
            "status": "UNVERIFIED",
            "errors": ["wall_scalar_face_count_mismatch"],
            "value_count": len(clean_values),
            "face_count": len(clean_faces),
        }
    xmin = min(point[0] for point in clean_vertices)
    xmax = max(point[0] for point in clean_vertices)
    chord = xmax - xmin
    if not math.isfinite(chord) or chord <= 0.0:
        return {"status": "UNVERIFIED", "errors": ["invalid_surface_chord"]}
    samples: list[dict[str, Any]] = []
    for index, (value, raw_face) in enumerate(zip(clean_values, clean_faces)):
        try:
            a, b = int(raw_face[0]), int(raw_face[1])
            pa, pb = clean_vertices[a], clean_vertices[b]
        except (IndexError, TypeError, ValueError):
            return {"status": "UNVERIFIED", "errors": ["invalid_surface_face_connectivity"]}
        x = 0.5 * (pa[0] + pb[0])
        y = 0.5 * (pa[1] + pb[1])
        samples.append(
            {
                "face_index": index,
                "value": value,
                "x": x,
                "y": y,
                "x_over_chord": (x - xmin) / chord,
                "surface_side": "upper" if y >= 0.0 else "lower",
            }
        )
    ranked = sorted(samples, key=lambda item: item["value"], reverse=True)
    return {
        "status": "PASS",
        "value_count": len(clean_values),
        "face_count": len(clean_faces),
        "surface_xmin": xmin,
        "surface_xmax": xmax,
        "surface_chord": chord,
        "maximum_location": ranked[0],
        "hotspots": ranked[: max(1, int(hotspot_count))],
        "samples": samples,
    }


def collect_wall_y_plus_distribution(solver: Any, wall_zone: str, *, hotspot_count: int = 20) -> dict[str, Any]:
    """Collect wall y+ together with face-centroid locations for hotspot diagnosis."""
    from ansys.fluent.core.services.field_data import SurfaceDataType

    values = collect_wall_y_plus(solver, wall_zone)
    field_data = getattr(getattr(solver, "fields", None), "field_data", None) or getattr(solver, "field_data", None)
    if field_data is None:
        raise RuntimeError("PyFluent field data service is unavailable")
    response = field_data.get_surface_data(
        data_types=[SurfaceDataType.Vertices, SurfaceDataType.FacesConnectivity],
        surfaces=[wall_zone],
    )
    surface_data = response.get(wall_zone) or next(iter(response.values()))
    result = locate_surface_scalar_hotspots(
        values,
        surface_data[SurfaceDataType.Vertices],
        surface_data[SurfaceDataType.FacesConnectivity],
        hotspot_count=hotspot_count,
    )
    result["field_name"] = "y-plus"
    result["surface"] = wall_zone
    return result


def _prefer_named_numeric(value: Any, token: str) -> float | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if token in _normalise_token(key):
                numbers = flatten_numeric_values(item)
                if numbers:
                    return numbers[-1]
        for item in value.values():
            found = _prefer_named_numeric(item, token)
            if found is not None:
                return found
    return None


def collect_boundary_mass_fluxes(solver: Any, boundary_zones: Iterable[str]) -> dict[str, float]:
    report = solver.settings.results.report
    values: dict[str, float] = {}
    for zone in boundary_zones:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", str(zone)):
            raise RuntimeError(f"unsafe boundary zone name for mass-flow query: {zone!r}")
        query_errors: list[str] = []
        numeric: float | None = None

        # PyFluent 0.40.2's results.report.fluxes.get_mass_flow wrapper emits
        # repeated `pm/boundaries` Scheme errors in Fluent 2025 R1 before its
        # fallback succeeds.  Use the stable signed TUI report first and retain
        # the settings surface-integral API only as a quiet compatibility path.
        command = f'(ti-menu-load-string "/report/fluxes/mass-flow no {zone} () no")'
        scheme = getattr(solver, "scheme", None) or getattr(solver, "scheme_eval", None)
        if scheme is not None:
            try:
                scheme_response = scheme.exec((command,))
                matches = re.findall(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?", str(scheme_response))
                numeric = float(matches[-1]) if matches else None
            except Exception as exc:
                query_errors.append(f"scheme_tui: {type(exc).__name__}: {exc}")
        if numeric is not None:
            values[str(zone)] = numeric
            continue

        integrals = report.surface_integrals
        common = {
            "surface_names": [str(zone)],
            "cust_vec_func": "",
            "report_of": "mass-flow-rate",
            "current_domain": "mixture",
        }
        query = getattr(integrals, "get_mass_flow_rate", None)
        if callable(query):
            try:
                response = query(**common, locations={})
            except TypeError:
                response = query(**common, geometry_names=[])
        else:
            try:
                response = integrals.mass_flow_rate(
                    **common, locations={}, write_to_file=False, file_name="", append_data=False
                )
            except TypeError:
                response = integrals.mass_flow_rate(
                    **common, geometry_names=[], write_to_file=False, file_name="", append_data=False
                )
        numeric = _prefer_named_numeric(response, "mass-flow")
        if numeric is None:
            flattened = flatten_numeric_values(response)
            numeric = flattened[-1] if flattened else None
        if numeric is None:
            raise RuntimeError(
                f"mass-flow report returned no numeric value for {zone!r}: {response!r}; "
                + " | ".join(query_errors)
            )
        values[str(zone)] = float(numeric)
    return values


def collect_final_residuals(solver: Any) -> dict[str, float]:
    monitors = getattr(solver, "monitors", None)
    if monitors is None:
        return {}
    result: dict[str, float] = {}
    for monitor_set in monitors.get_monitor_set_names():
        if "residual" not in str(monitor_set).lower():
            continue
        _iterations, series = monitors.get_monitor_set_data(monitor_set)
        for name, values in (series or {}).items():
            numeric = flatten_numeric_values(values)
            if numeric:
                result[str(name)] = numeric[-1]
    return result
